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

    # Lock contention parks a connection, so the pool needs to be wider than the
    # thread pool that feeds it or threads queue on checkout instead of on the row.
    db_pool_size: int = 20
    db_max_overflow: int = 10

    log_level: str = "info"


settings = Settings()
