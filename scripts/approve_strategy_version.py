"""Approve one reviewed, admitted strategy version; never start an observation clock."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from storage.db import create_db_engine
from storage.repositories.governance import GovernanceRepository


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="quant_research.db")
    parser.add_argument("--version", required=True)
    parser.add_argument("--admission-run-id", required=True, type=int)
    parser.add_argument("--approved-by", required=True)
    args = parser.parse_args()
    database_url = args.database if "://" in args.database else f"sqlite:///{Path(args.database).as_posix()}"
    repository = GovernanceRepository(engine=create_db_engine(database_url))
    repository.freeze_strategy_version(args.version, admission_run_id=args.admission_run_id,
                                       approved_by=args.approved_by)
    manifest = repository.load_frozen_runtime(args.version)
    print(json.dumps({"strategy_version": args.version, "admission_run_id": args.admission_run_id,
                      "runtime_hash": manifest.runtime_hash, "approved_by": args.approved_by.strip(),
                      "observation_clock_started": False}, indent=2))


if __name__ == "__main__":
    main()
