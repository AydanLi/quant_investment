"""Database engine factory and connection helpers.

The rest of the application talks to the database only through the engine
created here, using the schema in :mod:`storage.schema`. Swapping backends
(SQLite -> Postgres/MySQL) is a matter of changing the URL passed in and
installing the matching driver; nothing else in the codebase needs to change.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Engine, create_engine, event

from storage.schema import metadata

DEFAULT_DB_URL = "sqlite:///quant_research.db"


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    """SQLite ignores ``ON DELETE CASCADE`` unless PRAGMA foreign_keys is ON.

    Postgres/MySQL enforce foreign keys natively, so this hook is a no-op there.
    """

    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_connection, _connection_record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_db_engine(db_url: str = DEFAULT_DB_URL, echo: bool = False) -> Engine:
    """Create a SQLAlchemy engine for the given URL.

    For SQLite we also enable WAL journaling (better read/write concurrency for
    a dashboard reading while a backtest writes) and per-connection foreign-key
    enforcement.
    """
    engine = create_engine(db_url, echo=echo, future=True)

    if engine.dialect.name == "sqlite":
        _enable_sqlite_foreign_keys(engine)

        @event.listens_for(engine, "connect")
        def _set_wal(dbapi_connection, _connection_record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def create_all(engine: Engine) -> None:
    """Create any missing tables defined in the schema.

    Convenience for tests / fresh databases. In normal operation Alembic owns
    schema changes; prefer ``alembic upgrade head`` for managed environments.
    """
    metadata.create_all(engine)


_default_engine: Optional[Engine] = None


def get_engine(db_url: str = DEFAULT_DB_URL) -> Engine:
    """Return a process-wide singleton engine for the default URL.

    Repositories use this so the whole app shares one connection pool. Tests
    that need isolation should call :func:`create_db_engine` directly instead.
    """
    global _default_engine
    if _default_engine is None or str(_default_engine.url) != db_url:
        _default_engine = create_db_engine(db_url)
    return _default_engine
