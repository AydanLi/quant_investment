from __future__ import annotations

import sqlite3

import pytest

from scripts.backup_database import backup_database


def test_backup_database_copies_and_verifies_sqlite(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    with sqlite3.connect(source) as database:
        database.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        database.execute("INSERT INTO sample VALUES ('preserved')")

    assert backup_database(source, destination) == destination.resolve()
    with sqlite3.connect(destination) as database:
        assert database.execute("SELECT value FROM sample").fetchone() == ("preserved",)
        assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_backup_database_refuses_overwrite(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "backup.db"
    sqlite3.connect(source).close()
    destination.touch()

    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        backup_database(source, destination)

