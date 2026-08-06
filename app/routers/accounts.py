import uuid

from fastapi import APIRouter, status

from app.db import DbSession
from app.schemas import (
    AccountResponse,
    BalanceResponse,
    CreateAccountRequest,
    TransactionListResponse,
)
from app.services import accounts as service

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.post("", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
def create_account(payload: CreateAccountRequest, db: DbSession):
    return service.create_account(db, payload.initial_balance)


@router.get("/{account_id}/balance", response_model=BalanceResponse)
def get_balance(account_id: uuid.UUID, db: DbSession):
    account = service.get_account(db, account_id)
    return BalanceResponse(account_id=account.id, balance=account.balance)


@router.get("/{account_id}/transactions", response_model=TransactionListResponse)
def list_transactions(account_id: uuid.UUID, db: DbSession):
    items = service.list_transactions(db, account_id)
    return TransactionListResponse(account_id=account_id, items=items)
