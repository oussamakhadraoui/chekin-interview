"""Exactly-once execution for state-changing endpoints.

Idempotency is a property of any request that puts value into the ledger or moves it
around, not of transfers specifically -- `POST /accounts` opens a *funded* account, so a
retried create conjures money as surely as a retried transfer moves it twice. Both go
through this primitive, on one table and one fingerprint.

The whole mechanism is a primary-key insert. PostgreSQL blocks a duplicate until the first
transaction resolves, so one unique index serialises retries across every instance -- no
advisory locks, no Redis, no coordination service. The claim is taken inside the same
transaction as the work it authorises, so there is no window in which one exists without
the other.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from sqlalchemy.exc import DataError, IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.errors import (
    AmountOutOfRange,
    IdempotencyKeyConflict,
    InternalError,
    InvalidIdempotencyKey,
    LockTimeout,
    MissingIdempotencyKey,
)
from app.logging import log
from app.models import IdempotencyKey

# PostgreSQL error codes we handle by name rather than by string matching.
PG_UNIQUE_VIOLATION = "23505"
PG_LOCK_NOT_AVAILABLE = "55P03"
PG_DEADLOCK_DETECTED = "40P01"
PG_NUMERIC_OUT_OF_RANGE = "22003"

# Matches the column width. Enforced here rather than left to the INSERT, because
# PostgreSQL raises 22001 for an over-long value, which is a DataError rather than an
# IntegrityError and would escape every handler below as a 500. A malformed header is a
# client error and has to leave the API through the same envelope as any other.
MAX_KEY_LENGTH = 255


def require_key(raw: str | None) -> str:
    """Validate the `Idempotency-Key` header at the boundary. Shared by every mutating
    endpoint, so the rules cannot drift between them."""
    if not raw or not raw.strip():
        raise MissingIdempotencyKey("The Idempotency-Key header is required.")

    key = raw.strip()
    if len(key) > MAX_KEY_LENGTH:
        raise InvalidIdempotencyKey(
            f"The Idempotency-Key header must be at most {MAX_KEY_LENGTH} characters.",
            {"length": len(key), "max_length": MAX_KEY_LENGTH},
        )
    return key


@dataclass
class Outcome:
    """What the client gets back, and whether it was earned now or replayed."""

    status_code: int
    body: dict[str, Any]
    replayed: bool


def fingerprint(req: BaseModel) -> str:
    """Stable hash of the *meaning* of a request body.

    Hashing the parsed model rather than raw bytes means whitespace, key order and `"100"`
    vs `"100.00"` fingerprint identically -- they are the same request, and a retry through
    a proxy that reserialises JSON must not 409. A different amount or account pair does
    change the hash, which is the case worth catching.

    That `"100" == "100.00"` property comes from the *serialisers on the money fields*, not
    from here: pydantic renders a bare Decimal with `str()`, preserving whatever scale the
    client sent. Every money field must normalise or a safe retry becomes a 409; there is a
    test per endpoint pinning it.

    The operation is deliberately NOT hashed -- it is compared as its own column (see
    `replay`), which keeps the hash a pure function of the body, so adding an endpoint
    never invalidates hashes already stored by a running deploy.
    """
    canonical = json.dumps(req.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def replay(
    record: IdempotencyKey, operation: str, request_hash: str, *, raced: bool = False
) -> Outcome:
    """Answer from the stored response, or refuse if the key was recycled.

    Keys share one namespace across endpoints, so "same key, different operation" is
    checked explicitly rather than left to the accident that two endpoints' bodies differ.

    The body is returned *verbatim*, not re-validated through the response model: it is a
    snapshot of what that client was promised and should survive a schema change it would
    no longer parse under.

    A replay answers 200 rather than the stored `response_status` deliberately -- the
    200-vs-201 distinction is what tells a client "this had already happened", and it is
    the signal that survives a proxy stripping `Idempotent-Replay`. The original status
    goes on the log line, where it is useful for reconciling reports against what we sent.
    """
    if record.operation != operation or record.request_hash != request_hash:
        raise IdempotencyKeyConflict(
            "This idempotency key was already used for a different request.",
            {"idempotency_key": record.key, "operation": record.operation},
        )
    log.info(
        "idempotency.replayed",
        operation=operation,
        idempotency_key=record.key,
        resource_id=str(record.resource_id) if record.resource_id else None,
        original_status=record.response_status,
        # True when this request lost the race for the key rather than arriving after
        # the winner had already committed. A rising ratio means clients are retrying
        # faster than requests complete.
        raced=raced,
    )
    return Outcome(status_code=200, body=record.response_body, replayed=True)


def execute_once(
    db: Session,
    key: str,
    operation: str,
    req: BaseModel,
    perform: Callable[[IdempotencyKey], Outcome],
) -> Outcome:
    """Run `perform` at most once for this key, ever, across all instances.

    `perform` receives the claimed key row and must (a) do its work and (b) record its
    response on that row. It must not commit -- this function owns the transaction, and
    that is the point: the work and the claim are one atomic unit.
    """
    request_hash = fingerprint(req)

    # --- Fast path -------------------------------------------------------------
    # A retry of an already-completed request is the common case, and it needs no locks
    # at all. This is purely an optimisation: correctness for the racing case is handled
    # by the unique-violation branch below, which is the same check done atomically.
    try:
        existing = db.get(IdempotencyKey, key)
        if existing is not None:
            return replay(existing, operation, request_hash)
    finally:
        # Close the read transaction before the write one opens. In `finally` so that a
        # 409 raised out of replay() does not leave the session sitting on an idle
        # transaction, and *after* replay() rather than before, because rollback expires
        # loaded instances -- reading the record's fields afterwards would silently
        # issue a second SELECT.
        db.rollback()

    try:
        # Claim the key BEFORE doing any work.
        #
        # Ordering matters. A concurrent duplicate now blocks on the primary-key index
        # instead of on whatever rows the work touches, which buys three things:
        #   - the loser does no wasted work;
        #   - the loser never holds domain locks, so it cannot delay unrelated requests
        #     that merely share a row with it;
        #   - if this transaction rolls back (insufficient funds, say), the loser's
        #     insert succeeds and it proceeds on its own merits, rather than replaying
        #     a failure.
        record = IdempotencyKey(key=key, operation=operation, request_hash=request_hash)
        db.add(record)
        db.flush()

        outcome = perform(record)
        db.commit()
        log.info(
            "idempotency.committed",
            operation=operation,
            idempotency_key=key,
            resource_id=str(record.resource_id) if record.resource_id else None,
        )
        return outcome

    except IntegrityError as exc:
        db.rollback()
        if sqlstate(exc) != PG_UNIQUE_VIOLATION:
            raise

        # We lost the race to claim this key. Reaching here proves the winner
        # *committed*: had it rolled back, our insert would have succeeded rather than
        # conflicting. So the completed record is readable now.
        winner = db.get(IdempotencyKey, key)
        if winner is None:  # pragma: no cover - would mean the key was pruned mid-flight
            # Not a client error: our own insert conflicted with a row that is no longer
            # there, which means something deleted it between the violation and this
            # read. `InternalError` rather than the 400-status `LedgerError` base, so an
            # anomaly on our side is not reported to the client as their mistake.
            log.error("idempotency.record_vanished", operation=operation, idempotency_key=key)
            raise InternalError("Idempotency record vanished during a concurrent retry.") from exc
        return replay(winner, operation, request_hash, raced=True)

    except DataError as exc:
        db.rollback()
        # A value that passed schema validation but does not fit once applied -- a credit
        # pushing a balance past NUMERIC(20,4). DataError is a *sibling* of IntegrityError,
        # so without this branch 22003 escapes as a bare 500 with no envelope.
        if sqlstate(exc) != PG_NUMERIC_OUT_OF_RANGE:
            raise
        log.warning("ledger.value_out_of_range", operation=operation, idempotency_key=key)
        raise AmountOutOfRange(
            "The resulting balance would exceed the maximum the ledger can represent."
        ) from exc

    except OperationalError as exc:
        db.rollback()
        state = sqlstate(exc)
        if state == PG_LOCK_NOT_AVAILABLE:
            # Covers the wait on the key's unique index as well as any row locks the
            # work took, so a retry storm behind one slow request is bounded too.
            log.warning("idempotency.lock_timeout", operation=operation, idempotency_key=key)
            raise LockTimeout("Resource is busy, retry with the same idempotency key.") from exc
        if state == PG_DEADLOCK_DETECTED:
            # Should be unreachable: every lock is taken in a globally consistent order.
            # If this ever fires, that invariant has been broken somewhere and it needs
            # a human, so it is logged at error level rather than quietly retried.
            log.error("idempotency.deadlock", operation=operation, idempotency_key=key)
            raise LockTimeout("Transient contention, retry with the same key.") from exc
        raise

    except Exception:
        db.rollback()
        raise


def sqlstate(exc: Exception) -> str | None:
    return getattr(getattr(exc, "orig", None), "sqlstate", None)
