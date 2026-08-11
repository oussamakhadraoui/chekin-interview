from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://ledger:ledger@localhost:5433/ledger"

    # Cap on how long a transaction will wait for a row lock. Without this, a hot
    # account under heavy contention turns into an unbounded queue of requests each
    # holding a connection -- the pool drains and the instance stops serving anything,
    # including requests for unrelated accounts. Failing fast keeps the blast radius
    # on the contended account.
    lock_timeout_ms: int = 3000

    # Lock contention parks a connection, so the connection budget should be at least as
    # wide as the thread pool that feeds it -- otherwise threads queue on pool *checkout*
    # rather than on the row, and that wait is governed by SQLAlchemy's `pool_timeout`
    # (30s by default), not by `lock_timeout` above.
    #
    # These numbers do NOT satisfy that yet, and it is deliberate that the comment says so
    # rather than describing an intention the code does not meet. The endpoints are sync,
    # so Starlette runs them in AnyIO's default 40-thread pool, against 20 + 10 = 30
    # connections. Ten threads can therefore be waiting on checkout with a bound an order
    # of magnitude larger than the one the lock timeout enforces.
    #
    # Left as-is because the right fix is to size both against a real load profile -- the
    # binding constraint should be how many concurrent transactions PostgreSQL should be
    # running, and the thread pool sized under that, not both numbers raised until the
    # symptom goes away. The invariant to restore is `threads <= pool_size + max_overflow`.
    db_pool_size: int = 20
    db_max_overflow: int = 10

    log_level: str = "info"


settings = Settings()
