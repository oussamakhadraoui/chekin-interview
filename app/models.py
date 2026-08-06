import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Money is NUMERIC, never float. Four decimal places leaves room for sub-cent
# instruments without committing to a currency (see README: multi-currency is
# deliberately out of scope).
MONEY = Numeric(20, 4)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    balance: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # How much money the account was opened with. Immutable after creation.
    #
    # Opening balances are how value *enters* the system; ledger entries are how it
    # *moves* within it. Keeping them separate is what lets both invariants be exact:
    #
    #     SUM(ledger_entries.amount) = 0                      (nothing created or lost)
    #     balance = opening_balance + SUM(my ledger entries)  (the cache is honest)
    #
    # Without this column the second one is unassertable, because a funded account's
    # balance would legitimately differ from the sum of its entries and there would be
    # no way to tell that apart from corruption.
    opening_balance: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Defence in depth. The application never intends to write a negative
        # balance; this makes it impossible for a bug to. A violation surfaces as a
        # failed transaction, not as silently broken money.
        CheckConstraint("balance >= 0", name="ck_accounts_balance_non_negative"),
        CheckConstraint("opening_balance >= 0", name="ck_accounts_opening_non_negative"),
    )


class LedgerEntry(Base):
    """One side of a transfer.

    Every transfer writes exactly two rows sharing a transfer_id: a negative entry on
    the source and a positive entry on the destination. The rows are append-only --
    nothing ever updates or deletes them -- which makes conservation of money a
    property you can assert directly against the table:

        SELECT SUM(amount) FROM ledger_entries  -->  always exactly 0

    accounts.balance is a maintained cache of these entries, updated in the same
    transaction, so reads do not have to aggregate the whole history.
    """

    __tablename__ = "ledger_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transfer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False
    )
    # Signed: negative debits the account, positive credits it.
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("amount <> 0", name="ck_ledger_entries_amount_non_zero"),
        Index("ix_ledger_entries_account_id_created_at", "account_id", "created_at", "id"),
        Index("ix_ledger_entries_transfer_id", "transfer_id"),
    )


class IdempotencyKey(Base):
    """A client-supplied key claiming exactly one transfer.

    Written inside the same transaction as the ledger entries, so the key and the
    money it authorised commit or roll back together. There is no window in which one
    exists without the other.
    """

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    # SHA-256 of the canonicalised request body. Lets us tell "this is a retry" apart
    # from "this is a different transfer sent under a recycled key".
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    transfer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=True)
    response_status: Mapped[int] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
