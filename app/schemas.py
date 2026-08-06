import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

# Money crosses the wire as a decimal *string*, always with 4 places. JSON numbers are
# IEEE-754 doubles in most parsers, so "0.1" round-trips through a client as
# 0.1000000000000000055... Serialising as a string means the value a client reads is
# byte-for-byte the value the ledger holds.
Money = Annotated[
    Decimal,
    PlainSerializer(lambda v: f"{v:.4f}", return_type=str, when_used="json"),
]

# Amounts are accepted as string or number; strings are recommended for the reason
# above. gt=0 rules out zero and negative transfers -- a negative transfer would be a
# reverse transfer that bypasses the source account's balance check.
PositiveMoney = Annotated[
    Decimal,
    Field(gt=0, max_digits=20, decimal_places=4),
    PlainSerializer(lambda v: f"{v:.4f}", return_type=str, when_used="json"),
]


class CreateAccountRequest(BaseModel):
    # Opening a non-zero account is money appearing from nowhere. It is allowed here
    # because the exercise needs a way to get money into the system and there is no
    # external funding rail; see README for how this would work in production.
    initial_balance: Annotated[
        Decimal, Field(ge=0, max_digits=20, decimal_places=4)
    ] = Decimal("0")


class AccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    balance: Money
    created_at: datetime


class BalanceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_id: uuid.UUID
    balance: Money


class TransferRequest(BaseModel):
    from_account_id: uuid.UUID
    to_account_id: uuid.UUID
    amount: PositiveMoney


class TransferResponse(BaseModel):
    transfer_id: uuid.UUID
    from_account_id: uuid.UUID
    to_account_id: uuid.UUID
    amount: Money
    created_at: datetime


class TransactionItem(BaseModel):
    """One account's view of a transfer.

    Signed from the perspective of the account being queried: negative means money
    left it. `counterparty_account_id` is the other side of the same transfer.
    """

    transfer_id: uuid.UUID
    amount: Money
    direction: str  # "debit" | "credit"
    counterparty_account_id: uuid.UUID
    created_at: datetime


class TransactionListResponse(BaseModel):
    account_id: uuid.UUID
    items: list[TransactionItem]
