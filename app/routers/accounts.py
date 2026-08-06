import uuid

from fastapi import APIRouter, status

from app.db import DbSession
from app.errors import AccountNotFound, ValidationFailed, openapi_responses
from app.schemas import (
    AccountResponse,
    BalanceResponse,
    CreateAccountRequest,
    TransactionListResponse,
)
from app.services import accounts as service

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.post(
    "",
    response_model=AccountResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Open an account",
    response_description="The newly created account.",
    responses=openapi_responses(ValidationFailed),
)
def create_account(payload: CreateAccountRequest, db: DbSession):
    """Open an account, optionally funded.

    The opening balance is how money *enters* the system — there is no deposit rail in
    this service, so account creation is the only place value is created. It is stored
    immutably on the account, which is what makes the ledger reconcilable:
    `balance == opening_balance + sum(entries)` must hold for every account, forever.
    """
    return service.create_account(db, payload.initial_balance)


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
