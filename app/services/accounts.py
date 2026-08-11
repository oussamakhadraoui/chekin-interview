import uuid

from sqlalchemy import and_, select
from sqlalchemy.orm import Session, aliased

from app.errors import AccountNotFound
from app.models import Account, IdempotencyKey, LedgerEntry
from app.schemas import AccountResponse, CreateAccountRequest, TransactionItem
from app.services.idempotency import Outcome, execute_once

# Pagination is out of scope (see README), but "no pagination" must not mean "stream
# the entire history of a hot account into one response". A fixed cap keeps the
# endpoint's worst case bounded; the README says what replaces it.
MAX_TRANSACTIONS = 100

OPERATION = "create_account"


def create_account(db: Session, idempotency_key: str, req: CreateAccountRequest) -> Outcome:
    """Open an account, at most once per idempotency key.

    Account creation is the only place value *enters* this system, which makes a
    retried create the mirror image of a retried transfer: instead of moving money
    twice it mints it twice, out of nothing. Guarding transfers but not this would
    leave an asymmetry where the cheapest way to break conservation is a network
    timeout on the funding call. Same table, same fingerprint, same claim.

    No row locks are needed -- there is nothing to read before deciding -- so the key's
    unique index is the only serialisation point, and it is sufficient: whichever
    instance wins the insert opens the account, and every loser replays its response.
    """
    return execute_once(
        db,
        key=idempotency_key,
        operation=OPERATION,
        req=req,
        perform=lambda record: _open_account(db, record, req),
    )


def _open_account(db: Session, record: IdempotencyKey, req: CreateAccountRequest) -> Outcome:
    """The transactional body. `execute_once` owns commit/rollback."""
    account = Account(balance=req.initial_balance, opening_balance=req.initial_balance)
    db.add(account)
    db.flush()
    # created_at comes from a server default, so it only exists after the INSERT.
    db.refresh(account)

    body = AccountResponse.model_validate(account).model_dump(mode="json")

    record.resource_id = account.id
    record.response_status = 201
    record.response_body = body
    db.flush()

    return Outcome(status_code=201, body=body, replayed=False)


def get_account(db: Session, account_id: uuid.UUID) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise AccountNotFound("Account does not exist.", {"account_id": str(account_id)})
    return account


def list_transactions(db: Session, account_id: uuid.UUID) -> list[TransactionItem]:
    """Every ledger entry touching this account, newest first.

    Self-joins the ledger on transfer_id to recover the other side of each transfer.
    That join is the reason there is no separate `transfers` header table: the two
    entries already carry the full truth of a transfer, and a header row would
    duplicate the amount in a second place that could drift from the entries.
    """
    get_account(db, account_id)  # 404 rather than an empty list for an unknown account

    mine = aliased(LedgerEntry)
    theirs = aliased(LedgerEntry)

    rows = db.execute(
        select(mine.transfer_id, mine.amount, mine.created_at, theirs.account_id)
        .join(theirs, and_(theirs.transfer_id == mine.transfer_id, theirs.id != mine.id))
        .where(mine.account_id == account_id)
        # created_at alone is not a total order -- two transfers can share a
        # transaction timestamp. id breaks the tie so paging over this later is stable.
        .order_by(mine.created_at.desc(), mine.id.desc())
        .limit(MAX_TRANSACTIONS)
    ).all()

    return [
        TransactionItem(
            transfer_id=transfer_id,
            amount=amount,
            direction="debit" if amount < 0 else "credit",
            counterparty_account_id=counterparty_id,
            created_at=created_at,
        )
        for transfer_id, amount, created_at, counterparty_id in rows
    ]
