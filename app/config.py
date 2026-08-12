from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://ledger:ledger@localhost:5433/ledger"

    # Cap on how long a transaction waits for a row lock. Without it, a hot account turns
    # into an unbounded queue of requests each holding a connection: the pool drains and
    # the instance stops serving every account, not just the contended one.
    lock_timeout_ms: int = 3000

    # The binding constraint -- how many concurrent transactions PostgreSQL should run.
    # The request thread pool is sized down to match these at startup rather than these
    # being raised to meet it; `lifespan` in app/main.py argues that direction.
    # Both numbers still want a real load profile, which I do not have.
    db_pool_size: int = 20
    db_max_overflow: int = 10

    log_level: str = "info"


settings = Settings()
