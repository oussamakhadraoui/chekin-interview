import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

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

# Same, but zero is a legitimate opening balance.
#
# The serialiser is load-bearing beyond presentation: request fingerprints are taken
# over `model_dump(mode="json")`, and pydantic's default for a bare Decimal is `str()`,
# which preserves whatever scale the client happened to send. Without normalising here,
# "500" and "500.00" would hash differently and a proxy that reserialises JSON could
# turn a safe retry into a spurious 409 -- precisely the failure PositiveMoney avoids.
NonNegativeMoney = Annotated[
    Decimal,
    Field(ge=0, max_digits=20, decimal_places=4),
    PlainSerializer(lambda v: f"{v:.4f}", return_type=str, when_used="json"),
]


class ErrorDetail(BaseModel):
    code: str = Field(
        description="Stable machine-readable code. Switch on this, not the HTTP status."
    )
    message: str = Field(description="Human-readable explanation. May change; do not parse.")
    details: dict[str, Any] | None = Field(
        default=None, description="Structured context, e.g. the offending account id."
    )


class ErrorResponse(BaseModel):
    """The single envelope every failure uses, domain and validation alike."""

    error: ErrorDetail

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "error": {
                        "code": "INSUFFICIENT_FUNDS",
                        "message": "Source account has insufficient funds.",
                        "details": {
                            "account_id": "3f6c1e4a-...",
                            "balance": "10.0000",
                            "requested": "50.0000",
                        },
                    }
                }
            ]
        }
    )


class CreateAccountRequest(BaseModel):
    # Opening a non-zero account is money appearing from nowhere. It is allowed here
    # because the exercise needs a way to get money into the system and there is no
    # external funding rail; see README for how this would work in production.
    initial_balance: NonNegativeMoney = Field(
        default=Decimal("0"),
        description=(
            "Opening balance. Send as a string to avoid float rounding. "
            "Recorded immutably so the ledger can be reconciled against it. "
            'Equivalent encodings ("500", "500.00", 500) are treated as the same '
            "request when retried."
        ),
    )

    # `extra="forbid"` is load-bearing for idempotency, not just tidiness. The request
    # fingerprint is taken over the *parsed* model, so anything pydantic drops is
    # invisible to the hash: with the default `extra="ignore"`, two requests that differ
    # only in an unmodelled field fingerprint identically and the second is answered as
    # a replay of the first. Rejecting the field is the only way "same fingerprint" can
    # keep meaning "same request".
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"initial_balance": "500.00"}]},
    )


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
    from_account_id: uuid.UUID = Field(
        description="Account to debit. Must differ from the destination."
    )
    to_account_id: uuid.UUID = Field(description="Account to credit.")
    amount: PositiveMoney = Field(
        description=(
            "Strictly positive. Send as a string. Equivalent encodings "
            '("25", "25.00", 25) are treated as the same transfer when retried.'
        )
    )

    # See CreateAccountRequest: an unmodelled field that pydantic silently drops would
    # be invisible to the request fingerprint, so two different requests would replay
    # each other. A money API should also never accept a field it does not honour --
    # a client sending `"currency": "EUR"` deserves a 422, not a silent USD transfer.
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "from_account_id": "3f6c1e4a-9a1b-4c2d-8e3f-0a1b2c3d4e5f",
                    "to_account_id": "7b2d8c5e-1f4a-4b6c-9d8e-2f3a4b5c6d7e",
                    "amount": "125.50",
                }
            ]
        },
    )


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
