"""Shared repository plumbing: engine handling and a portable upsert."""
from __future__ import annotations

from typing import Iterable, Mapping, Optional, Sequence

from sqlalchemy import Engine, Table
from sqlalchemy.engine import Connection

from storage.db import DEFAULT_DB_URL, get_engine


class BaseRepository:
    """Base class holding the SQLAlchemy engine.

    Pass an explicit ``engine`` (tests use an in-memory one); otherwise the
    process-wide engine for ``db_url`` is used so the whole app shares a pool.
    """

    def __init__(self, engine: Optional[Engine] = None, db_url: str = DEFAULT_DB_URL):
        self.engine = engine if engine is not None else get_engine(db_url)


def upsert(
    conn: Connection,
    table: Table,
    rows: Sequence[Mapping],
    index_elements: Iterable[str],
    update_columns: Iterable[str],
) -> None:
    """Insert ``rows``, updating ``update_columns`` on conflict of the keys.

    Dispatches to each backend's native upsert (SQLite / Postgres / MySQL) and
    falls back to delete-then-insert for any other dialect, so callers stay
    database-agnostic. ``rows`` must be a list of column->value mappings.
    """
    if not rows:
        return

    dialect = conn.engine.dialect.name
    update_columns = list(update_columns)

    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = sqlite_insert(table).values(list(rows))
        stmt = stmt.on_conflict_do_update(
            index_elements=list(index_elements),
            set_={c: getattr(stmt.excluded, c) for c in update_columns},
        )
        conn.execute(stmt)
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(table).values(list(rows))
        stmt = stmt.on_conflict_do_update(
            index_elements=list(index_elements),
            set_={c: getattr(stmt.excluded, c) for c in update_columns},
        )
        conn.execute(stmt)
    elif dialect == "mysql":
        from sqlalchemy.dialects.mysql import insert as mysql_insert

        stmt = mysql_insert(table).values(list(rows))
        stmt = stmt.on_duplicate_key_update(
            **{c: getattr(stmt.inserted, c) for c in update_columns}
        )
        conn.execute(stmt)
    else:  # pragma: no cover - generic portable fallback
        from sqlalchemy import and_, delete

        keys = list(index_elements)
        for row in rows:
            conn.execute(
                delete(table).where(
                    and_(*(table.c[k] == row[k] for k in keys))
                )
            )
        conn.execute(table.insert(), list(rows))
