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
    # What the account was opened with. Immutable. Opening balances are how value *enters*
    # the system; ledger entries are how it *moves* within it, and keeping them separate is
    # what makes both invariants exact:
    #
    #     SUM(ledger_entries.amount) = 0                      (nothing created or lost)
    #     balance = opening_balance + SUM(my ledger entries)  (the cache is honest)
    #
    # Without this column the second is unassertable: a funded account's balance would
    # legitimately differ from its entries, indistinguishable from corruption.
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
    transaction, so reads do not aggregate the whole history.

    The schema enforces this, not just the code that writes it: a deferrable constraint
    trigger (migration c4b8f2e07a13) re-checks at COMMIT that entries sharing a transfer_id
    sum to zero. Deferred, because the first leg legitimately leaves the sum non-zero; a
    trigger, because the rule spans a *set* of rows and no CHECK sees more than one. It
    fires against every writer, including ones that never touch this application.
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
    """A client-supplied key claiming exactly one state change.

    Written inside the same transaction as the effect it authorises -- the ledger
    entries of a transfer, or the row of a newly opened account -- so the key and that
    effect commit or roll back together. There is no window in which one exists without
    the other.

    One table serves every mutating endpoint. Keys share a single namespace, which is
    why `operation` is stored: it makes the row self-describing ("what did this key
    actually do?") without parsing the body, and it is the natural label to break
    replay metrics down by.
    """

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    # Which endpoint consumed the key: "create_transfer" | "create_account".
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    # SHA-256 of the canonicalised operation + request body. Lets us tell "this is a
    # retry" apart from "this is a different request sent under a recycled key".
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # The thing the request created: a transfer_id, or an account id.
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # What the original request answered (201 today for both). Kept for forensics, not for
    # replay -- a replay deliberately answers 200, because that is the signal telling a
    # client "this had already happened". Read onto the `idempotency.replayed` log line.
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Nullable because the row is claimed before the work runs; `execute_once` fills both
    # in before the commit that makes the claim visible, so a *committed* row always has
    # them. That is an invariant of the primitive, not of the column.
    response_body: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
