from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import settings
from app.db import engine
from app.errors import InternalError, LedgerError
from app.logging import configure_logging, log, new_request_id, request_id_var
from app.routers import accounts, transfers

configure_logging()

API_DESCRIPTION = """
A money-movement ledger. Accounts hold balances; clients transfer funds between them
and query balances and history.

It runs as **several stateless instances behind a load balancer** sharing one PostgreSQL
database. Any request can land on any instance and a retry need not reach the same process
as the original, so every guarantee below is enforced in the database, not in memory.

### Guarantees

**Exactly-once money operations.** `POST /transfers` and `POST /accounts` both require an
`Idempotency-Key`. Retrying with the same key returns the original result and neither moves
nor mints additional money, whichever instance handles the retry. Creation is guarded
because an opening balance is how value *enters* the ledger — a retried create breaks
conservation as surely as a retried transfer.

**Money is conserved.** Each transfer writes two signed rows to an append-only ledger that
always nets to zero. Balances can never go negative; a `CHECK` constraint enforces that
even if the application is wrong.

**Correct under concurrency.** Both accounts are row-locked in a globally consistent order
before any balance is read for a decision, so simultaneous transfers can neither overdraw
an account nor deadlock against each other.

### Handling failures

If a request does not return — timeout, reset, `503` — **resend it identically with the
same `Idempotency-Key`**. That is the correct recovery path. Never generate a new key for a
retry, and never reuse a key for a different request.

`Idempotent-Replay` tells a fresh execution (`false`, `201`) from a replay (`true`, `200`).

### Money format

Amounts are `NUMERIC(20,4)` in the database and **strings** on the wire (`"125.50"`). JSON
numbers are IEEE-754 doubles in most parsers, which silently corrupts money. Send strings;
you always receive strings, normalised to four decimal places.

### Errors

Every failure, validation and domain alike, uses one envelope. Switch on `code`; the HTTP
status is coarse guidance and `message` is for humans.

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


@asynccontextmanager
async def lifespan(instance: FastAPI):
    """Size the request thread pool to the connection pool that feeds it.

    Every endpoint here is sync, so Starlette runs it in an AnyIO worker thread and every
    one of those threads needs a connection. AnyIO defaults to 40 threads against a pool
    of `pool_size + max_overflow` (30), so the ten surplus threads queue on *connection
    checkout* rather than on the row they actually contend for.

    The two waits are bounded by different things: a row-lock wait by `lock_timeout` (3s),
    surfacing as a retryable 503; a checkout wait by `pool_timeout` (30s), raising
    `sqlalchemy.exc.TimeoutError` -- a sibling of `OperationalError`, not a subclass, so no
    branch in `execute_once` catches it. The failure `lock_timeout` exists to bound came
    back one layer up, ten times larger and in a worse shape.

    Sizing down rather than raising the pool keeps the binding constraint where it belongs:
    PostgreSQL decides how many concurrent transactions to run, and everything upstream
    lives under that. The invariant is `threads <= pool_size + max_overflow`.
    """
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = settings.db_pool_size + settings.db_max_overflow
    # Exposed for the test that pins the invariant; the limiter itself is only readable
    # from inside the event loop.
    instance.state.request_threads = limiter.total_tokens
    log.info(
        "startup.thread_pool_sized",
        threads=limiter.total_tokens,
        connections=settings.db_pool_size + settings.db_max_overflow,
    )
    yield


app = FastAPI(
    title="Transaction Ledger API",
    version="0.1.0",
    summary="Idempotent money movement that conserves value under concurrency.",
    description=API_DESCRIPTION,
    openapi_tags=TAGS_METADATA,
    lifespan=lifespan,
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
    # Driven by the error class's own `retryable` flag rather than by its status, so
    # "is this safe to resend?" is answered once, next to the error it describes.
    headers = {"Retry-After": "1"} if exc.retryable else {}
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


@app.exception_handler(Exception)
async def unhandled_error_handler(_request: Request, exc: Exception):
    """The catch-all that makes "one envelope" true rather than aspirational.

    The handlers above cover the failures we anticipated; this covers the ones we did not,
    which are exactly the ones a client is least equipped to handle. Without it they leave
    as `text/plain` "Internal Server Error" and code switching on `error.code` gets a parse
    failure on top of an outage. Still reachable today: a pool checkout timeout, a dropped
    connection, a CHECK firing because the application was wrong, an unmapped sqlstate.

    Two bugs of this shape were already fixed one at a time -- a `DataError` from a balance
    overflow, a `22001` from an over-long key. This handles the class instead.

    The exception's own text is logged, never returned: it is the one string here nobody
    has audited for what it might contain. The request id joins the two.
    """
    log.error(
        "request.unhandled_error",
        error_type=type(exc).__name__,
        error=str(exc),
        exc_info=exc,
    )
    error = InternalError("An unexpected error occurred. The request was not applied.")
    return JSONResponse(status_code=error.status_code, content=error.body())


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
