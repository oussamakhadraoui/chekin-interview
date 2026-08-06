"""Test harness.

Everything here runs against a real PostgreSQL instance, never SQLite. The behaviour
under test *is* PostgreSQL behaviour -- SELECT ... FOR UPDATE, unique-index blocking,
lock_timeout -- and SQLite implements none of it. A suite that passed on SQLite would
be testing nothing that matters here.
"""

import os

# Must precede any `app.*` import: app.config reads the environment at import time and
# app.db builds the engine from it. Pointing the whole app at a throwaway database is
# the only way to keep the suite from truncating the dev one.
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://ledger:ledger@localhost:5433/ledger_test"
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

import threading  # noqa: E402
import time  # noqa: E402
from decimal import Decimal  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
import sqlalchemy  # noqa: E402
import uvicorn  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _create_test_database() -> None:
    admin_url = TEST_DATABASE_URL.rsplit("/", 1)[0] + "/ledger"
    admin = sqlalchemy.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    db_name = TEST_DATABASE_URL.rsplit("/", 1)[1]
    with admin.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": db_name}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    admin.dispose()


@pytest.fixture(scope="session", autouse=True)
def database():
    _create_test_database()
    # Migrate rather than metadata.create_all, so the suite exercises the same DDL
    # path a deploy does. A migration that drifts from the models fails here.
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(cfg, "head")
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_tables(database):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE accounts, ledger_entries, idempotency_keys CASCADE"))
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def live_server(database):
    """A real uvicorn process-in-a-thread, for the concurrency tests.

    The concurrency tests deliberately go over HTTP rather than calling the service
    directly, so the request path under test includes the connection pool and the
    per-request session lifecycle -- both of which are part of how the guarantees hold.
    """
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 20
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("live server failed to start")
        time.sleep(0.02)

    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def http(live_server):
    limits = httpx.Limits(max_connections=64, max_keepalive_connections=64)
    with httpx.Client(base_url=live_server, limits=limits, timeout=30.0) as c:
        yield c


# --- helpers ------------------------------------------------------------------


def make_account(client_or_http, balance: str) -> str:
    resp = client_or_http.post("/accounts", json={"initial_balance": balance})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def balance_of(client_or_http, account_id: str) -> Decimal:
    resp = client_or_http.get(f"/accounts/{account_id}/balance")
    assert resp.status_code == 200, resp.text
    return Decimal(resp.json()["balance"])


def assert_ledger_invariants(expected_total: Decimal) -> None:
    """The three properties that must hold after *any* sequence of operations.

    These are asserted against the database directly rather than through the API,
    because the API is what is being doubted.
    """
    with SessionLocal() as db:
        # 1. Money is neither created nor destroyed: every transfer contributed a
        #    matched pair of signed entries, so the whole table nets to exactly zero.
        entry_sum = db.execute(
            text("SELECT COALESCE(SUM(amount), 0) FROM ledger_entries")
        ).scalar_one()
        assert entry_sum == Decimal("0"), f"ledger does not net to zero: {entry_sum}"

        # 2. Total value in the system is unchanged from what was deposited.
        total = db.execute(text("SELECT COALESCE(SUM(balance), 0) FROM accounts")).scalar_one()
        assert total == expected_total, f"total balance drifted: {total} != {expected_total}"

        # 3. No balance went negative.
        negatives = db.execute(
            text("SELECT COUNT(*) FROM accounts WHERE balance < 0")
        ).scalar_one()
        assert negatives == 0, "an account went negative"

        # 4. The balance cache agrees with the entries it summarises:
        #        balance == opening_balance + SUM(entries for that account)
        #    This is the one that catches a transfer touching a balance without
        #    writing entries, or writing entries without touching the balance -- the
        #    two halves drifting apart. Recomputing from the append-only log is
        #    exactly what a real reconciliation job would do.
        drift = db.execute(
            text(
                """
                SELECT a.id, a.balance, a.opening_balance + COALESCE(SUM(e.amount), 0)
                FROM accounts a
                LEFT JOIN ledger_entries e ON e.account_id = a.id
                GROUP BY a.id, a.balance, a.opening_balance
                HAVING a.balance <> a.opening_balance + COALESCE(SUM(e.amount), 0)
                """
            )
        ).all()
        assert not drift, f"balance cache disagrees with the ledger: {drift}"
