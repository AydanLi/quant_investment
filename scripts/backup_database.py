"""Create and verify a consistent SQLite backup before schema migration."""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def backup_database(source: Path, destination: Path) -> Path:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Database does not exist: {source}")
    if source == destination:
        raise ValueError("Backup destination must differ from source database")

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite backup: {destination}")

    source_uri = f"file:{source.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_db:
        with sqlite3.connect(destination) as backup_db:
            source_db.backup(backup_db)

    with sqlite3.connect(destination) as backup_db:
        integrity = backup_db.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"Backup integrity check failed: {integrity}")
    return destination


def _default_destination() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(".runtime") / f"quant_research_before_upgrade_{stamp}.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("quant_research.db"))
    parser.add_argument("--destination", type=Path, default=_default_destination())
    args = parser.parse_args()
    result = backup_database(args.source, args.destination)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

