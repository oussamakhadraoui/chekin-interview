from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

engine = create_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_pre_ping=True,
    future=True,
    # Bound every lock wait, for the life of the physical connection.
    #
    # Passed as a libpq startup option rather than issued as a `SET` on the "connect"
    # event, because `SET` is transactional. A `SET` from an event handler lands inside
    # the implicit transaction psycopg opens for it, and SQLAlchemy's pool rolls that
    # transaction back when the connection is returned (`reset_on_return="rollback"`) --
    # which reverts the setting. Only the very first request on each connection would
    # have been bounded; every later one would wait indefinitely, park a connection, and
    # drain the pool. That is exactly the failure mode the timeout exists to prevent, so
    # it has to survive a rollback. Startup options are applied during the connection
    # handshake and are not part of any transaction.
    #
    # `tests/test_lock_timeout.py` pins this: it asserts the setting is still in force on
    # a *reused* pooled connection, not just a fresh one.
    connect_args={"options": f"-c lock_timeout={settings.lock_timeout_ms}"},
)


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
