from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.db import engine
from app.errors import LedgerError
from app.logging import configure_logging, log, new_request_id, request_id_var
from app.routers import accounts, transfers

configure_logging()

app = FastAPI(
    title="Transaction Ledger API",
    version="0.1.0",
    description="Idempotent money movement that conserves value under concurrency.",
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


@app.get("/health", tags=["ops"])
def health():
    """Liveness: is this process running?

    Deliberately does not touch the database. If Postgres blips and liveness depends on
    it, every instance fails its probe at once and the orchestrator restarts the entire
    fleet -- turning a recoverable database incident into a total outage.
    """
    return {"status": "ok"}


@app.get("/ready", tags=["ops"])
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
