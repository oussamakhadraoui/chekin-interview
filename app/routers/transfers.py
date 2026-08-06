from typing import Annotated

from fastapi import APIRouter, Header, Response, status

from app.db import DbSession
from app.errors import (
    AccountNotFound,
    IdempotencyKeyConflict,
    InsufficientFunds,
    LockTimeout,
    MissingIdempotencyKey,
    SameAccountTransfer,
    ValidationFailed,
    openapi_responses,
)
from app.schemas import TransferRequest, TransferResponse
from app.services.transfers import execute_transfer

router = APIRouter(prefix="/transfers", tags=["transfers"])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Transfer money between two accounts",
    response_description="The transfer that was executed.",
    responses={
        201: {
            "model": TransferResponse,
            "description": "Transfer executed. `Idempotent-Replay: false`.",
        },
        200: {
            "model": TransferResponse,
            "description": (
                "This key was already used for this exact transfer. The original "
                "response is replayed byte-for-byte and **no money moved**. "
                "`Idempotent-Replay: true`."
            ),
        },
        **openapi_responses(
            MissingIdempotencyKey,
            SameAccountTransfer,
            AccountNotFound,
            IdempotencyKeyConflict,
            InsufficientFunds,
            ValidationFailed,
            LockTimeout,
        ),
    },
)
def create_transfer(
    payload: TransferRequest,
    response: Response,
    db: DbSession,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            description=(
                "**Required.** A client-generated unique string (a UUID is the usual "
                "choice) identifying this transfer attempt. Retrying with the same key "
                "and the same body returns the original result without moving money "
                "again. Reusing a key with a *different* body returns 409."
            ),
            examples=["a3f1c2d4-5e6b-4789-a0b1-c2d3e4f5a6b7"],
        ),
    ] = None,
):
    """Move money from one account to another, exactly once.

    Atomic: the debit, the credit, the two ledger entries and the idempotency record
    all commit together or not at all. There is no state in which money has left one
    account without arriving in the other.

    Safe to retry. If you do not get a response — timeout, connection reset, a 503 —
    resend the identical request with the **same** `Idempotency-Key`. That is the only
    correct way to recover, and it is guaranteed not to move money twice even if the
    retry lands on a different instance than the original.
    """
    if not idempotency_key or not idempotency_key.strip():
        raise MissingIdempotencyKey("The Idempotency-Key header is required.")

    outcome = execute_transfer(db, idempotency_key.strip(), payload)

    response.status_code = outcome.status_code
    # Lets a client (and anyone reading logs) tell "your transfer went through just
    # now" apart from "your transfer had already gone through".
    response.headers["Idempotent-Replay"] = "true" if outcome.replayed else "false"
    return outcome.body
