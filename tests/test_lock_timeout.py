"""The lock timeout is a correctness component, so it gets a test.

`lock_timeout` is what keeps a single stuck transaction from turning into an outage: a
transfer that cannot get its row lock fails fast with a retryable 503 instead of parking
a pooled connection indefinitely. With enough contention, unbounded waits exhaust the
pool and the instance stops serving *every* account, not just the contended one.

That makes it worth two tests rather than a config line taken on trust. The first pins
the setting itself; the second pins the behaviour a client actually sees.
"""

import uuid
from decimal import Decimal

from sqlalchemy import text

from app.config import settings
from app.db import SessionLocal, engine
from tests.conftest import assert_ledger_invariants, balance_of, make_account


def _lock_timeout() -> str:
    with SessionLocal() as db:
        return db.execute(text("SHOW lock_timeout")).scalar_one()


def test_lock_timeout_survives_connection_reuse():
    """The regression test for a bug that made the timeout dead code.

    It was originally applied with `SET lock_timeout = ...` from a "connect" event
    handler. `SET` is transactional in PostgreSQL, so it landed inside the implicit
    transaction psycopg opened for it, and SQLAlchemy's pool rolled that transaction back
    when the connection was returned -- reverting the setting.

    The effect was silent and worse than having no timeout at all: the *first* request on
    each physical connection was bounded, so a naive check passed, while every subsequent
    request on that connection waited forever. Under contention the pool would drain and
    the 503 path would never fire.

    Checking a second checkout is the whole point of this test. Checking only the first
    is what let the bug through.
    """
    expected = f"{settings.lock_timeout_ms}ms"

    first = _lock_timeout()
    second = _lock_timeout()  # same physical connection, returned to the pool once
    third = _lock_timeout()

    def normalise(value: str) -> int:
        # PostgreSQL renders 3000ms as "3s"; compare in milliseconds, not text.
        return int(value[:-2]) if value.endswith("ms") else int(float(value[:-1]) * 1000)

    assert normalise(first) == settings.lock_timeout_ms, f"never applied: {first}"
    assert normalise(second) == settings.lock_timeout_ms, (
        f"lock_timeout was lost when the connection was reused: {second} (expected {expected}). "
        "Every request after the first would wait for a row lock indefinitely."
    )
    assert normalise(third) == settings.lock_timeout_ms


def test_a_transfer_blocked_on_a_held_lock_returns_a_retryable_503(client):
    """The client-visible half: a contended row fails fast and safely.

    A row lock is held on the source account from outside the API for longer than
    `lock_timeout`, and a transfer is fired at it. The transfer must give up and return
    `LOCK_TIMEOUT` with `Retry-After` — not hang, and not corrupt anything.

    The important assertion is the last one: the request timed out *before* touching any
    money, so the idempotency key is not consumed and the very same key succeeds on
    retry. That is what makes the documented recovery path ("resend with the same key")
    correct for a 503 rather than a way to lose a transfer.
    """
    src = make_account(client, "100")
    dst = make_account(client, "0")
    key = uuid.uuid4().hex

    holder = SessionLocal()
    try:
        holder.execute(text("SELECT id FROM accounts WHERE id = :i FOR UPDATE"), {"i": src})

        blocked = client.post(
            "/transfers",
            json={"from_account_id": src, "to_account_id": dst, "amount": "40"},
            headers={"Idempotency-Key": key},
        )

        assert blocked.status_code == 503, blocked.text
        assert blocked.json()["error"]["code"] == "LOCK_TIMEOUT"
        assert blocked.headers["Retry-After"] == "1"
    finally:
        holder.rollback()
        holder.close()

    # No money moved, and the key was not burned: the same key still works.
    assert balance_of(client, src) == Decimal("100")

    retry = client.post(
        "/transfers",
        json={"from_account_id": src, "to_account_id": dst, "amount": "40"},
        headers={"Idempotency-Key": key},
    )
    assert retry.status_code == 201, retry.text

    assert balance_of(client, src) == Decimal("60")
    assert balance_of(client, dst) == Decimal("40")
    assert_ledger_invariants(expected_total=Decimal("100"))


def test_lock_timeout_survives_a_rollback():
    """Names the exact mechanism that broke, so a regression is self-explaining.

    Rollback is the specific thing that reverted the old `SET`, and the pool issues one
    on every connection it reclaims. Asserting directly across a rollback states the
    requirement in one line — the setting must not be transactional — where the reuse
    test above only observes its consequence.
    """
    with engine.connect() as conn:
        before = conn.execute(text("SHOW lock_timeout")).scalar_one()
        conn.rollback()
        after = conn.execute(text("SHOW lock_timeout")).scalar_one()

    assert before == after, (
        f"lock_timeout is transactional: {before} before rollback, {after} after. "
        "It must be applied as a connection startup option, not via SET."
    )
