import uuid
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.orm import Session, aliased

from app.errors import AccountNotFound
from app.models import Account, LedgerEntry
from app.schemas import TransactionItem

# Pagination is out of scope (see README), but "no pagination" must not mean "stream
# the entire history of a hot account into one response". A fixed cap keeps the
# endpoint's worst case bounded; the README says what replaces it.
MAX_TRANSACTIONS = 100


def create_account(db: Session, initial_balance: Decimal) -> Account:
    account = Account(balance=initial_balance, opening_balance=initial_balance)
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


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
