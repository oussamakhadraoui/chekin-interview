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


class InsufficientFunds(LedgerError):
    status_code = 422
    code = "INSUFFICIENT_FUNDS"


class SameAccountTransfer(LedgerError):
    """Source == destination.

    Rejected rather than treated as a no-op: it is always a client bug, and allowing it
    would mean reading and writing one row twice inside a single transaction, which is
    exactly the shape that produces lost updates.
    """

    status_code = 400
    code = "SAME_ACCOUNT_TRANSFER"


class IdempotencyKeyConflict(LedgerError):
    """Same key, different request body.

    The client has recycled a key for a genuinely different transfer. Replaying the
    stored response would silently swallow the new transfer; executing it would break
    the promise the key makes. Both are worse than refusing.
    """

    status_code = 409
    code = "IDEMPOTENCY_KEY_CONFLICT"


class LockTimeout(LedgerError):
    """Could not acquire the account locks within lock_timeout.

    Retryable: the request never started moving money. 503 + Retry-After tells clients
    and load balancers that this is congestion, not a bad request.
    """

    status_code = 503
    code = "LOCK_TIMEOUT"
