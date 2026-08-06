from typing import Annotated

from fastapi import APIRouter, Header, Response

from app.db import DbSession
from app.errors import LedgerError
from app.schemas import TransferRequest
from app.services.transfers import execute_transfer

router = APIRouter(prefix="/transfers", tags=["transfers"])


class MissingIdempotencyKey(LedgerError):
    status_code = 400
    code = "IDEMPOTENCY_KEY_REQUIRED"


@router.post("")
def create_transfer(
    payload: TransferRequest,
    response: Response,
    db: DbSession,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    """Move money between two accounts.

    The `Idempotency-Key` header is mandatory rather than optional. Making it optional
    would mean the safe path is opt-in, and every client that forgets it gets
    at-least-once money movement by default. A required header turns "I forgot" into a
    400 at integration time instead of a duplicated transfer at 3am.

    The key belongs in a header, not the body: it describes the delivery of the
    request, not the transfer itself, and keeping it out of the body is what lets the
    body be hashed as the transfer's identity.
    """
    if not idempotency_key or not idempotency_key.strip():
        raise MissingIdempotencyKey("The Idempotency-Key header is required.")

    outcome = execute_transfer(db, idempotency_key.strip(), payload)

    response.status_code = outcome.status_code
    # Lets a client (and anyone reading logs) tell "your transfer went through just
    # now" apart from "your transfer had already gone through".
    response.headers["Idempotent-Replay"] = "true" if outcome.replayed else "false"
    return outcome.body
