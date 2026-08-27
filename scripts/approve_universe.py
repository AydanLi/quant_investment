"""Apply an explicit operator approval to an existing draft universe version."""

from __future__ import annotations

import argparse
from pathlib import Path

from storage.db import create_db_engine
from storage.repositories.governance import GovernanceRepository


def _database_url(database: str) -> str:
    return database if "://" in database else f"sqlite:///{Path(database).as_posix()}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="quant_research.db")
    parser.add_argument("--version", required=True)
    parser.add_argument("--approved-by", required=True)
    args = parser.parse_args()

    repository = GovernanceRepository(
        engine=create_db_engine(_database_url(args.database))
    )
    repository.approve_universe_version(
        args.version,
        approved_by=args.approved_by,
    )
    print(f"Approved universe {args.version} by {args.approved_by}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

