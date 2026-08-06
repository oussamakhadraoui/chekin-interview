from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

engine = create_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,
    future=True,
)


@event.listens_for(engine, "connect")
def _set_lock_timeout(dbapi_connection, _record):
    """Bound every lock wait at the connection level.

    Set once per physical connection rather than per transaction so there is no way
    to open a transaction that forgot it.
    """
    with dbapi_connection.cursor() as cur:
        cur.execute(f"SET lock_timeout = {settings.lock_timeout_ms}")


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# One session per request, returned to the pool when the request ends. Sessions are
# never shared between requests: two concurrent transfers must be two transactions.
DbSession = Annotated[Session, Depends(get_db)]
