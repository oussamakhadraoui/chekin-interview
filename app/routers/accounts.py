import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Response, status

from app.db import DbSession
from app.errors import (
    AccountNotFound,
    IdempotencyKeyConflict,
    InvalidIdempotencyKey,
    LockTimeout,
    MissingIdempotencyKey,
    ValidationFailed,
    openapi_responses,
)
from app.schemas import (
    AccountResponse,
    BalanceResponse,
    CreateAccountRequest,
    TransactionListResponse,
)
from app.services import accounts as service
from app.services.idempotency import require_key

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Open an account",
    response_description="The newly created account.",
    responses={
        201: {
            "model": AccountResponse,
            "description": "Account opened. `Idempotent-Replay: false`.",
        },
        200: {
            "model": AccountResponse,
            "description": (
                "This key already opened an account with this exact body. The original "
                "response is replayed and **no second account exists**. "
                "`Idempotent-Replay: true`."
            ),
        },
        **openapi_responses(
            MissingIdempotencyKey,
            InvalidIdempotencyKey,
            IdempotencyKeyConflict,
            ValidationFailed,
            LockTimeout,
        ),
    },
)
def create_account(
    payload: CreateAccountRequest,
    response: Response,
    db: DbSession,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            description=(
                "**Required.** A client-generated unique string identifying this "
                "attempt to open an account. Retrying with the same key and body "
                "returns the original account without opening a second one. Reusing "
                "a key with a *different* body returns 409."
            ),
            examples=["a3f1c2d4-5e6b-4789-a0b1-c2d3e4f5a6b7"],
        ),
    ] = None,
):
    """Open an account, optionally funded, exactly once.

    The opening balance is how money *enters* the system — there is no deposit rail here,
    so this is the only place value is created. It is stored immutably, which is what makes
    the ledger reconcilable: `balance == opening_balance + sum(entries)`, for every account,
    forever.

    Because this endpoint mints value it carries the same **required** `Idempotency-Key` as
    a transfer, and for the same reason that guarantee survives a retry landing on a
    different instance: the claim lives in the shared database, not in a process.
    """
    outcome = service.create_account(db, require_key(idempotency_key), payload)

    response.status_code = outcome.status_code
    response.headers["Idempotent-Replay"] = "true" if outcome.replayed else "false"
    return outcome.body


@router.get(
    "/{account_id}/balance",
    response_model=BalanceResponse,
    summary="Get an account's current balance",
    responses=openapi_responses(AccountNotFound, ValidationFailed),
)
def get_balance(account_id: uuid.UUID, db: DbSession):
    """Read the current balance.

    Served from the `accounts.balance` cache rather than by summing the ledger, so it
    is O(1) regardless of history length. The cache is written in the same transaction
    as the entries it summarises, so it is never stale — but it is a cache, and the
    reconciliation query in the README is what proves it still agrees with the log.

    Not part of any transfer's consistency guarantee: a balance read concurrently with
    a transfer may reflect either side of it. Do not read-then-transfer and expect the
    balance to still hold; send the transfer and let it fail with `INSUFFICIENT_FUNDS`.
    """
    account = service.get_account(db, account_id)
    return BalanceResponse(account_id=account.id, balance=account.balance)


@router.get(
    "/{account_id}/transactions",
    response_model=TransactionListResponse,
    summary="List an account's transactions",
    responses=openapi_responses(AccountNotFound, ValidationFailed),
)
def list_transactions(account_id: uuid.UUID, db: DbSession):
    """Every transfer touching this account, newest first, capped at 100.

    Amounts are signed from this account's perspective: negative left it, positive
    arrived. `counterparty_account_id` is the other side of the same transfer.

    Pagination is deliberately not implemented (see README); the cap keeps the worst
    case bounded until it is. Only successful transfers appear — a rejected transfer
    writes nothing.
    """
    items = service.list_transactions(db, account_id)
    return TransactionListResponse(account_id=account_id, items=items)
