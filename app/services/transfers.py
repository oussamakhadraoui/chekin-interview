"""Money movement.

This module is the whole exercise. The three hard requirements map onto three
mechanisms:

  1. Idempotency          -> a primary-key claim on `idempotency_keys`, taken inside
                             the same transaction as the money it authorises. That part
                             is generic, so it lives in `idempotency.execute_once`.
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

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AccountNotFound, InsufficientFunds, SameAccountTransfer
from app.logging import log
from app.models import Account, IdempotencyKey, LedgerEntry
from app.schemas import TransferRequest, TransferResponse
from app.services.idempotency import Outcome, execute_once

OPERATION = "create_transfer"


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


def execute_transfer(db: Session, idempotency_key: str, req: TransferRequest) -> Outcome:
    # Reject a structurally impossible request before the idempotency layer sees it. A
    # same-account transfer can never commit, so it can never be a retry of anything --
    # letting it reach the key lookup would only mean answering "you reused a key" (409)
    # to a request that is malformed whatever key it carries. Validate the request, then
    # ask whether it has already been performed.
    if req.from_account_id == req.to_account_id:
        raise SameAccountTransfer("Cannot transfer to the same account.")

    outcome = execute_once(
        db,
        key=idempotency_key,
        operation=OPERATION,
        req=req,
        perform=lambda record: _move_money(db, record, req),
    )

    if not outcome.replayed:
        log.info(
            "transfer.committed",
            transfer_id=outcome.body["transfer_id"],
            idempotency_key=idempotency_key,
            from_account_id=str(req.from_account_id),
            to_account_id=str(req.to_account_id),
            amount=str(req.amount),
        )
    return outcome


def _move_money(db: Session, record: IdempotencyKey, req: TransferRequest) -> Outcome:
    """The transactional body. `execute_once` owns commit/rollback and has already
    claimed `record`, so a concurrent duplicate is blocked on the key's index and is
    not competing for the account locks taken below."""

    # 1. Lock both accounts, in a deterministic order.
    accounts = _lock_accounts(db, [req.from_account_id, req.to_account_id])
    source = accounts[req.from_account_id]
    destination = accounts[req.to_account_id]

    # 2. Check funds *after* the lock. Checking before would read a balance that another
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

    # 3. Move the money. Two signed rows that sum to zero, and a balance cache updated
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
        created_at=now(db),
    )
    body = response.model_dump(mode="json")

    # 4. Store the response on the key record, in this same transaction. A retry that
    #    arrives after a crash-before-response gets back the identical body, because
    #    the body was durable at the same instant the money moved.
    record.resource_id = transfer_id
    record.response_status = 201
    record.response_body = body
    db.flush()

    return Outcome(status_code=201, body=body, replayed=False)


def now(db: Session):
    """Transaction timestamp from the database, not the app.

    N instances have N slightly different clocks. Taking the time from the shared
    datastore means ledger ordering does not depend on whose clock served the request.
    """
    from sqlalchemy import func

    return db.execute(select(func.now())).scalar_one()
