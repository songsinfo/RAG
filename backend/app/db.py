from contextlib import contextmanager
from typing import Generator
from sqlalchemy import create_engine, Connection
from app.config import settings

engine = create_engine(
    settings.sqlalchemy_database_uri,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    connect_args={"charset": "utf8mb4"}
)


@contextmanager
def get_db_connection() -> Generator[Connection, None, None]:
    with engine.connect() as connection:
        yield connection


def get_db() -> Generator[Connection, None, None]:
    with engine.connect() as connection:
        yield connection
