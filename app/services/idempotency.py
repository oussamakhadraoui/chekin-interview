"""Exactly-once execution for state-changing endpoints.

Idempotency is not a property of transfers. It is a property of any request that puts
value into the ledger or moves it around. `POST /accounts` opens a *funded* account, so
a retried create conjures money exactly as surely as a retried transfer moves it twice.
Both endpoints therefore go through the primitive in this module, backed by the same
`idempotency_keys` table and the same SHA-256 request fingerprint.

The whole mechanism is one primary-key insert. PostgreSQL blocks a duplicate insert
until the first transaction resolves, so a single unique index is what serialises
retries across every instance behind the load balancer -- no advisory locks, no Redis,
no coordination service. The claim is taken inside the same transaction as the work it
authorises, so the key and its effect commit or roll back together. There is no window
in which one exists without the other.
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
    InvalidIdempotencyKey,
    LedgerError,
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

    Hashing the parsed model rather than the raw bytes means whitespace, key order and
    `"100"` vs `"100.00"` all fingerprint identically -- those are the same request, and
    a client retrying through a proxy that reserialises JSON should not get a spurious
    409. A different amount or a different account pair does change the hash, which is
    the case we actually want to catch.

    Note that the "100" == "100.00" property comes from the *serialisers on the money
    fields*, not from anything here: pydantic renders a bare Decimal with `str()`, which
    preserves whatever scale the client sent. Every money field must normalise, or a
    safe retry becomes a 409. There is a test per endpoint pinning that.

    The operation is deliberately NOT hashed. It is compared as its own column instead
    (see `replay`), which gives the same answer while keeping the hash a pure function
    of the body -- so adding an endpoint to this table never invalidates hashes already
    stored by a running deploy.
    """
    canonical = json.dumps(req.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def replay(record: IdempotencyKey, operation: str, request_hash: str) -> Outcome:
    """Answer from the stored response, or refuse if the key was recycled.

    Keys share a single namespace across endpoints, so "same key, different operation"
    is checked explicitly rather than left to the accident that two endpoints' request
    bodies happen to differ.

    The body is returned *verbatim* rather than re-validated through the response model:
    it is a snapshot of what that client was promised, and it should survive a later
    schema change that the old body would no longer parse under.
    """
    if record.operation != operation or record.request_hash != request_hash:
        raise IdempotencyKeyConflict(
            "This idempotency key was already used for a different request.",
            {"idempotency_key": record.key, "operation": record.operation},
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
            raise LedgerError("Idempotency record vanished during a concurrent retry.") from exc
        log.info("idempotency.replayed_after_race", operation=operation, idempotency_key=key)
        return replay(winner, operation, request_hash)

    except DataError as exc:
        db.rollback()
        # A value that passed schema validation but does not fit once the database applies
        # it -- in practice, a credit that would push a balance past NUMERIC(20,4).
        #
        # This branch exists because DataError is a sibling of IntegrityError, not a
        # subclass: without it, 22003 escapes as a bare 500 with no error envelope. That is
        # the same failure shape as an over-long idempotency key, which is handled at the
        # boundary in `require_key`. The difference is that an out-of-range *result* is only
        # knowable after the arithmetic, so it has to be caught here instead.
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
