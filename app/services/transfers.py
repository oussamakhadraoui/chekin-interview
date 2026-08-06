"""Money movement.

This module is the whole exercise. The three hard requirements map onto three
mechanisms, all of which live in `execute_transfer` below:

  1. Idempotency          -> a primary-key claim on `idempotency_keys`, taken inside
                             the same transaction as the money it authorises.
  2. Conservation         -> two signed ledger rows summing to zero, plus a balance
                             check performed *after* the row locks are held, plus a
                             CHECK constraint as a backstop.
  3. Concurrency          -> SELECT ... FOR UPDATE on both accounts, acquired in a
                             globally consistent order so instances cannot deadlock.

Everything here is deliberately in the database rather than in the process. The
service is assumed to run as N stateless instances behind a load balancer, so any
in-process construct -- a `threading.Lock`, an `asyncio.Lock`, an LRU of seen keys --
would protect one instance while the other N-1 happily corrupted the ledger. The
datastore is the only thing all instances share, so it is the only place a guarantee
can live.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.errors import (
    AccountNotFound,
    IdempotencyKeyConflict,
    InsufficientFunds,
    LedgerError,
    LockTimeout,
    SameAccountTransfer,
)
from app.logging import log
from app.models import Account, IdempotencyKey, LedgerEntry
from app.schemas import TransferRequest, TransferResponse

# PostgreSQL error codes we handle by name rather than by string matching.
PG_UNIQUE_VIOLATION = "23505"
PG_LOCK_NOT_AVAILABLE = "55P03"
PG_DEADLOCK_DETECTED = "40P01"


@dataclass
class TransferOutcome:
    status_code: int
    body: dict[str, Any]
    replayed: bool


def fingerprint(req: TransferRequest) -> str:
    """Stable hash of the *meaning* of a transfer request.

    Hashing the parsed model rather than the raw bytes means whitespace, key order and
    `"100"` vs `"100.00"` all fingerprint identically -- those are the same transfer,
    and a client retrying through a proxy that reserialises JSON should not get a
    spurious 409. A different amount or a different account pair does change the hash,
    which is the case we actually want to catch.
    """
    canonical = json.dumps(req.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _replay(record: IdempotencyKey, request_hash: str) -> TransferOutcome:
    if record.request_hash != request_hash:
        raise IdempotencyKeyConflict(
            "This idempotency key was already used for a different transfer.",
            {"idempotency_key": record.key},
        )
    return TransferOutcome(status_code=200, body=record.response_body, replayed=True)


def _lock_accounts(db: Session, account_ids: list[uuid.UUID]) -> dict[uuid.UUID, Account]:
    """Take row locks on the given accounts in a globally consistent order.

    Deadlock avoidance. Two concurrent transfers A->B and B->A will each want both
    rows. If one instance locks A then B while the other locks B then A, they wait on
    each other and Postgres kills one after `deadlock_timeout`. Sorting the ids first
    means *every* instance requests locks in the same sequence, so the second one
    simply queues behind the first. Any total order works as long as all instances
    agree on it; sorted-by-uuid happens to also match Postgres's own uuid ordering.

    Locks are taken one statement at a time, on purpose. The tempting one-liner is

        select(Account).where(Account.id.in_(ids)).order_by(Account.id).with_for_update()

    but ORDER BY constrains the order rows are *returned*, not the order they are
    *locked*. For a simple index scan those coincide; under a bitmap heap scan or a
    parallel plan the locking node can sit below the sort and acquire in heap order,
    which is arbitrary. That failure mode is planner-dependent, so it would pass tests
    on a small table and start deadlocking once the table grew enough to change the
    plan. Two round trips is a cheap price for an ordering that does not depend on the
    query planner.

    populate_existing=True forces the returned objects to be refreshed from the locked
    read. Without it SQLAlchemy would hand back whatever version of the row is already
    in the session's identity map -- a value read *before* the lock was held, which is
    exactly the stale read the lock exists to prevent.
    """
    locked: dict[uuid.UUID, Account] = {}
    for account_id in sorted(account_ids):
        account = db.execute(
            select(Account)
            .where(Account.id == account_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if account is None:
            raise AccountNotFound("Account does not exist.", {"account_id": str(account_id)})
        locked[account_id] = account
    return locked


def execute_transfer(db: Session, idempotency_key: str, req: TransferRequest) -> TransferOutcome:
    request_hash = fingerprint(req)

    # --- Fast path -------------------------------------------------------------
    # A retry of an already-completed transfer is the common case, and it needs no
    # locks at all. This is purely an optimisation: correctness for the racing case is
    # handled by the unique-violation branch at the bottom, which is the same check
    # done atomically.
    try:
        existing = db.get(IdempotencyKey, idempotency_key)
        if existing is not None:
            return _replay(existing, request_hash)
    finally:
        # Close the read transaction before the write one opens. In `finally` so that a
        # 409 raised out of _replay does not leave the session sitting on an idle
        # transaction, and *after* _replay rather than before, because rollback expires
        # loaded instances -- reading the record's fields afterwards would silently
        # issue a second SELECT.
        db.rollback()

    if req.from_account_id == req.to_account_id:
        raise SameAccountTransfer("Cannot transfer to the same account.")

    try:
        outcome = _perform(db, idempotency_key, request_hash, req)
        db.commit()
        log.info(
            "transfer.committed",
            transfer_id=outcome.body["transfer_id"],
            idempotency_key=idempotency_key,
            from_account_id=str(req.from_account_id),
            to_account_id=str(req.to_account_id),
            amount=str(req.amount),
        )
        return outcome

    except IntegrityError as exc:
        db.rollback()
        if _sqlstate(exc) != PG_UNIQUE_VIOLATION:
            raise

        # We lost the race to claim this key. Reaching here proves the winner
        # *committed*: had it rolled back, our insert would have succeeded rather than
        # conflicting. So the completed record is readable now.
        winner = db.get(IdempotencyKey, idempotency_key)
        if winner is None:  # pragma: no cover - would mean the key was pruned mid-flight
            raise LedgerError("Idempotency record vanished during a concurrent retry.") from exc
        log.info("transfer.replayed_after_race", idempotency_key=idempotency_key)
        return _replay(winner, request_hash)

    except OperationalError as exc:
        db.rollback()
        state = _sqlstate(exc)
        if state == PG_LOCK_NOT_AVAILABLE:
            log.warning("transfer.lock_timeout", idempotency_key=idempotency_key)
            raise LockTimeout("Account is busy, retry with the same idempotency key.") from exc
        if state == PG_DEADLOCK_DETECTED:
            # Should be unreachable: _lock_accounts imposes a global lock order. If
            # this ever fires, that invariant has been broken somewhere and it needs a
            # human, so it is logged at error level rather than quietly retried.
            log.error("transfer.deadlock", idempotency_key=idempotency_key)
            raise LockTimeout("Transient contention, retry with the same key.") from exc
        raise

    except Exception:
        db.rollback()
        raise


def _perform(
    db: Session, idempotency_key: str, request_hash: str, req: TransferRequest
) -> TransferOutcome:
    """The transactional body. Caller owns commit/rollback."""

    # 1. Claim the key BEFORE touching any account.
    #
    # Ordering matters here. A concurrent duplicate of this request now blocks on the
    # primary-key index instead of on the account rows, which buys three things:
    #   - the loser does no wasted balance arithmetic;
    #   - the loser never holds account locks, so it cannot delay unrelated transfers
    #     that merely share an account with it;
    #   - if this transaction rolls back (insufficient funds, say), the loser's insert
    #     succeeds and it proceeds on its own merits, rather than replaying a failure.
    #
    # Postgres blocks a duplicate insert until the first transaction resolves, so this
    # single index is what serialises retries across instances. No advisory lock, no
    # cache, no coordination service.
    record = IdempotencyKey(key=idempotency_key, request_hash=request_hash)
    db.add(record)
    db.flush()

    # 2. Lock both accounts, in a deterministic order.
    accounts = _lock_accounts(db, [req.from_account_id, req.to_account_id])
    source = accounts[req.from_account_id]
    destination = accounts[req.to_account_id]

    # 3. Check funds *after* the lock. Checking before would read a balance that another
    #    transaction can invalidate before we write -- the classic TOCTOU overdraft.
    if source.balance < req.amount:
        raise InsufficientFunds(
            "Source account has insufficient funds.",
            {
                "account_id": str(source.id),
                "balance": f"{source.balance:.4f}",
                "requested": f"{req.amount:.4f}",
            },
        )

    # 4. Move the money. Two signed rows that sum to zero, and a balance cache updated
    #    to match. Both legs are the same Decimal, so conservation is structural rather
    #    than something the code has to remember to get right.
    transfer_id = uuid.uuid4()
    amount: Decimal = req.amount

    source.balance -= amount
    destination.balance += amount

    db.add(LedgerEntry(transfer_id=transfer_id, account_id=source.id, amount=-amount))
    db.add(LedgerEntry(transfer_id=transfer_id, account_id=destination.id, amount=amount))

    response = TransferResponse(
        transfer_id=transfer_id,
        from_account_id=source.id,
        to_account_id=destination.id,
        amount=amount,
        created_at=_now(db),
    )
    body = response.model_dump(mode="json")

    # 5. Store the response on the key record, in this same transaction. A retry that
    #    arrives after a crash-before-response gets back the identical body, because
    #    the body was durable at the same instant the money moved.
    record.transfer_id = transfer_id
    record.response_status = 201
    record.response_body = body
    db.flush()

    return TransferOutcome(status_code=201, body=body, replayed=False)


def _now(db: Session):
    """Transaction timestamp from the database, not the app.

    N instances have N slightly different clocks. Taking the time from the shared
    datastore means ledger ordering does not depend on whose clock served the request.
    """
    from sqlalchemy import func

    return db.execute(select(func.now())).scalar_one()


def _sqlstate(exc: Exception) -> str | None:
    return getattr(getattr(exc, "orig", None), "sqlstate", None)
