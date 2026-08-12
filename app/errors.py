"""Domain errors and their wire representation.

Every failure leaves the API as::

    {"error": {"code": "INSUFFICIENT_FUNDS", "message": "...", "details": {...}}}

The HTTP status is deliberately coarse -- it tells a proxy or a generic client how to
behave. The `code` is the stable contract: it is what application code switches on, and
it is what we promise not to change without a version bump. Reusing 422 for two
different domain failures is fine when the codes distinguish them.
"""

from typing import Any


class LedgerError(Exception):
    status_code: int = 400
    code: str = "LEDGER_ERROR"
    #: Shown in the OpenAPI schema so the docs list every failure an endpoint can
    #: return, not just the happy path.
    summary: str = "Request could not be processed."
    retryable: bool = False

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def body(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return {"error": payload}


class AccountNotFound(LedgerError):
    status_code = 404
    code = "ACCOUNT_NOT_FOUND"
    summary = "No account exists with the given id."


class InsufficientFunds(LedgerError):
    status_code = 422
    code = "INSUFFICIENT_FUNDS"
    summary = (
        "The source account's balance is lower than the requested amount. "
        "Nothing was moved and the idempotency key was not consumed, so the same "
        "request may be retried once the account is funded."
    )


class SameAccountTransfer(LedgerError):
    """Source == destination.

    Rejected rather than treated as a no-op: it is always a client bug, and allowing it
    would mean reading and writing one row twice inside a single transaction, which is
    exactly the shape that produces lost updates.
    """

    status_code = 400
    code = "SAME_ACCOUNT_TRANSFER"
    summary = "Source and destination are the same account."


class IdempotencyKeyConflict(LedgerError):
    """Same key, different request body.

    The client has recycled a key for a genuinely different request. Replaying the
    stored response would silently swallow the new request; executing it would break
    the promise the key makes. Both are worse than refusing.
    """

    status_code = 409
    code = "IDEMPOTENCY_KEY_CONFLICT"
    summary = (
        "This Idempotency-Key was already used for a request with a different body. "
        "Use a fresh key for a different request."
    )


class LockTimeout(LedgerError):
    """Could not acquire the needed locks within lock_timeout.

    Retryable: the request never started moving money. 503 + Retry-After tells clients
    and load balancers that this is congestion, not a bad request.
    """

    status_code = 503
    code = "LOCK_TIMEOUT"
    summary = (
        "The accounts were locked by other transfers for longer than the configured "
        "lock timeout. No money moved. Safe to retry with the SAME idempotency key."
    )
    retryable = True


class MissingIdempotencyKey(LedgerError):
    """No Idempotency-Key header on a request that creates or moves money.

    Required rather than optional: an opt-in safety mechanism means the default
    behaviour is at-least-once money movement.
    """

    status_code = 400
    code = "IDEMPOTENCY_KEY_REQUIRED"
    summary = "The Idempotency-Key header is missing or blank."


class InvalidIdempotencyKey(LedgerError):
    """The header is present but unusable -- currently, longer than the column.

    Checked at the boundary rather than left to the INSERT, where PostgreSQL's 22001
    arrives as a `DataError` and would escape as a 500. A malformed header is the
    client's problem and has to be reported as one.
    """

    status_code = 400
    code = "IDEMPOTENCY_KEY_INVALID"
    summary = "The Idempotency-Key header is longer than 255 characters."


class AmountOutOfRange(LedgerError):
    """A value that is individually valid but does not fit once applied.

    Pydantic rejects an over-large *amount* at the boundary, so this is unreachable from a
    single oversized field. It exists for the accumulating case: a destination balance that
    would exceed NUMERIC(20,4) once the credit lands, which PostgreSQL reports as 22003.

    Same class of bug as `InvalidIdempotencyKey`, caught in a different place: an
    out-of-range *result* is only knowable after the database has done the arithmetic.
    """

    status_code = 422
    code = "AMOUNT_OUT_OF_RANGE"
    summary = (
        "The resulting balance would exceed the maximum the ledger can represent. "
        "Nothing was moved and the idempotency key was not consumed."
    )


class InternalError(LedgerError):
    """Anything that reached the client as a bare 500 before this existed.

    "Every failure uses one envelope" is worth only as much as its weakest path. Handlers
    for known exception types cover what we anticipated; this covers what we did not -- a
    dropped connection, an unmapped sqlstate, a pool checkout timeout
    (`sqlalchemy.exc.TimeoutError`, a *sibling* of `OperationalError`, so no branch in
    `execute_once` sees it). Without it those leave as `text/plain`.

    The message is deliberately generic: an unexpected exception's text is the one string
    nobody has audited for secrets. It goes to the log, joined by the request id.
    """

    status_code = 500
    code = "INTERNAL_ERROR"
    summary = "An unexpected internal error occurred. The request was not applied."
    retryable = True


class ValidationFailed(LedgerError):
    """Documentation-only. Never raised by hand.

    FastAPI raises `RequestValidationError` for a malformed body or an unparseable path
    parameter, and the handler in `main` renders it into the same envelope as every
    other failure. Declaring it here is what keeps the OpenAPI schema honest: without
    it the spec advertises FastAPI's default `HTTPValidationError` shape, which is not
    what this API actually returns.
    """

    status_code = 422
    code = "VALIDATION_ERROR"
    summary = "The request body or a path parameter failed schema validation."


def openapi_responses(*error_types: type[LedgerError]) -> dict[int, dict[str, Any]]:
    """Build the OpenAPI `responses` block for a set of domain errors.

    Derived from the error classes themselves so the docs cannot drift from the code:
    adding a new failure mode to an endpoint is one entry in its decorator, and the
    status, code, description and example all come from the class.
    """
    from app.schemas import ErrorResponse

    grouped: dict[int, list[type[LedgerError]]] = {}
    for error_type in error_types:
        grouped.setdefault(error_type.status_code, []).append(error_type)

    responses: dict[int, dict[str, Any]] = {}
    for status_code, types in grouped.items():
        responses[status_code] = {
            "model": ErrorResponse,
            "description": " · ".join(f"**{t.code}** — {t.summary}" for t in types),
            "content": {
                "application/json": {
                    "examples": {
                        t.code: {
                            "summary": t.code,
                            "value": {"error": {"code": t.code, "message": t.summary}},
                        }
                        for t in types
                    }
                }
            },
        }
    return responses
