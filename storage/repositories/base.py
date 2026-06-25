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


# Conservative cap on bind parameters per statement. SQLite's historical limit
# is 999 (newer builds allow far more); staying under 999 is bulletproof across
# versions and well within Postgres/MySQL limits too.
_MAX_BIND_PARAMS = 900


def _chunk(rows: Sequence[Mapping], columns_per_row: int):
    """Yield row batches small enough to stay under the bind-parameter cap."""
    batch_size = max(1, _MAX_BIND_PARAMS // max(1, columns_per_row))
    for start in range(0, len(rows), batch_size):
        yield list(rows[start : start + batch_size])


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

    Rows are inserted in batches so a large upsert never exceeds the database's
    bind-parameter limit (a single multi-row INSERT of thousands of rows would
    otherwise fail with "too many SQL variables" on SQLite).
    """
    if not rows:
        return

    dialect = conn.engine.dialect.name
    index_elements = list(index_elements)
    update_columns = list(update_columns)
    columns_per_row = len(rows[0])

    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        for batch in _chunk(rows, columns_per_row):
            stmt = sqlite_insert(table).values(batch)
            stmt = stmt.on_conflict_do_update(
                index_elements=index_elements,
                set_={c: getattr(stmt.excluded, c) for c in update_columns},
            )
            conn.execute(stmt)
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        for batch in _chunk(rows, columns_per_row):
            stmt = pg_insert(table).values(batch)
            stmt = stmt.on_conflict_do_update(
                index_elements=index_elements,
                set_={c: getattr(stmt.excluded, c) for c in update_columns},
            )
            conn.execute(stmt)
    elif dialect == "mysql":
        from sqlalchemy.dialects.mysql import insert as mysql_insert

        for batch in _chunk(rows, columns_per_row):
            stmt = mysql_insert(table).values(batch)
            stmt = stmt.on_duplicate_key_update(
                **{c: getattr(stmt.inserted, c) for c in update_columns}
            )
            conn.execute(stmt)
    else:  # pragma: no cover - generic portable fallback
        from sqlalchemy import and_, delete

        for row in rows:
            conn.execute(
                delete(table).where(
                    and_(*(table.c[k] == row[k] for k in index_elements))
                )
            )
        conn.execute(table.insert(), list(rows))
