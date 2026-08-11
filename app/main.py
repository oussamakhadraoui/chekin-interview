from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.db import engine
from app.errors import LedgerError
from app.logging import configure_logging, log, new_request_id, request_id_var
from app.routers import accounts, transfers

configure_logging()

API_DESCRIPTION = """
A money-movement ledger. Accounts hold balances; clients transfer funds between them
and query balances and history.

The service is designed to run as **several stateless instances behind a load
balancer**, sharing one PostgreSQL database. Any request can land on any instance, and
a retry need not reach the same process as the original. Every guarantee below is
therefore enforced in the database, not in application memory.

### Guarantees

**Exactly-once money operations.** Every `POST /transfers` and every `POST /accounts`
carries a required `Idempotency-Key`. Retrying with the same key returns the original
result and neither moves nor mints additional money — whichever instance handles the
retry. Account creation is guarded too because an opening balance is how value *enters*
the ledger, so a retried create is as capable of breaking conservation as a retried
transfer.

**Money is conserved.** Each transfer writes two signed rows to an append-only ledger
that always nets to zero. Balances can never go negative; a database `CHECK` constraint
enforces that even if the application is wrong.

**Correct under concurrency.** Both accounts are row-locked in a globally consistent
order before any balance is read for a decision, so simultaneous transfers can neither
overdraw an account nor deadlock against each other.

### Handling failures

If a transfer does not return — timeout, reset, `503` — **resend the identical request
with the same `Idempotency-Key`**. That is the correct and safe recovery path. Do not
generate a new key for a retry, and do not reuse a key for a different transfer.

Check the `Idempotent-Replay` response header to tell a fresh execution (`false`, `201`)
from a replay of an earlier one (`true`, `200`).

### Money format

Amounts are `NUMERIC(20,4)` in the database and **strings** on the wire (`"125.50"`).
JSON numbers are IEEE-754 doubles in most parsers, which silently corrupts money. Send
strings; you will always receive strings, normalised to four decimal places.

### Errors

Every failure — validation and domain alike — uses one envelope. Switch on `code`; the
HTTP status is coarse guidance and `message` is for humans.

```json
{"error": {"code": "INSUFFICIENT_FUNDS", "message": "...", "details": {}}}
```
"""

TAGS_METADATA = [
    {"name": "accounts", "description": "Open accounts, read balances and history."},
    {
        "name": "transfers",
        "description": "Move money. The only endpoint that mutates balances.",
    },
    {
        "name": "ops",
        "description": "Liveness and readiness probes. Not part of the client contract.",
    },
]

app = FastAPI(
    title="Transaction Ledger API",
    version="0.1.0",
    summary="Idempotent money movement that conserves value under concurrency.",
    description=API_DESCRIPTION,
    openapi_tags=TAGS_METADATA,
)

app.include_router(accounts.router)
app.include_router(transfers.router)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or new_request_id()
    token = request_id_var.set(request_id)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        request_id_var.reset(token)


@app.exception_handler(LedgerError)
async def ledger_error_handler(_request: Request, exc: LedgerError):
    headers = {}
    if exc.status_code == 503:
        headers["Retry-After"] = "1"
    return JSONResponse(status_code=exc.status_code, content=exc.body(), headers=headers)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError):
    """Render schema failures in the same envelope as domain failures.

    FastAPI's default shape is a bare list under `detail`, which means clients need two
    parsers for errors. One envelope, one `code` to switch on.
    """
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Request body failed validation.",
                "details": {"fields": _summarise(exc)},
            }
        },
    )


def _summarise(exc: RequestValidationError) -> list[dict[str, str]]:
    return [
        {"field": ".".join(str(p) for p in err["loc"][1:]) or "body", "reason": err["msg"]}
        for err in exc.errors()
    ]


@app.get("/health", tags=["ops"], summary="Liveness probe")
def health():
    """Liveness: is this process running?

    Deliberately does not touch the database. If Postgres blips and liveness depends on
    it, every instance fails its probe at once and the orchestrator restarts the entire
    fleet -- turning a recoverable database incident into a total outage.
    """
    return {"status": "ok"}


@app.get(
    "/ready",
    tags=["ops"],
    summary="Readiness probe",
    responses={503: {"description": "Cannot reach the database; pull from rotation."}},
)
def ready():
    """Readiness: should the load balancer send this instance traffic?

    This one *does* check the database, because an instance that cannot reach Postgres
    cannot serve a transfer and should be pulled from rotation without being killed.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        log.error("readiness.failed", error=str(exc))
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return {"status": "ready"}
