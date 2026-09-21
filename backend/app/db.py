import threading
from contextlib import contextmanager
from typing import Generator
from sqlalchemy import create_engine, Connection
from app.config import settings

_sqlite_connection_lock = threading.RLock()

engine = create_engine(
    settings.sqlalchemy_database_uri,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    connect_args={"charset": "utf8mb4"}
)


@contextmanager
def get_db_connection() -> Generator[Connection, None, None]:
    # Test fixtures use SQLite StaticPool, which shares one DB-API connection
    # across LangGraph checkpoint worker threads and application code. Serialize
    # that test-only connection while leaving MySQL fully concurrent.
    lock = _sqlite_connection_lock if engine.dialect.name == "sqlite" else None
    if lock is None:
        with engine.connect() as connection:
            yield connection
        return
    with lock:
        with engine.connect() as connection:
            yield connection


def get_db() -> Generator[Connection, None, None]:
    lock = _sqlite_connection_lock if engine.dialect.name == "sqlite" else None
    if lock is None:
        with engine.connect() as connection:
            yield connection
        return
    with lock:
        with engine.connect() as connection:
            yield connection
