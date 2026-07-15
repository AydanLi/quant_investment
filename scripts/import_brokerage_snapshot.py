"""Import a normalized, read-only brokerage snapshot from JSON.

The input object must contain provider, masked account_ref, account_type, and a
positions array.  Credentials and full brokerage account numbers are rejected.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from storage.db import create_db_engine
from storage.repositories.brokerage_mirror import BrokerageMirrorRepository


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--db-url", default="sqlite:///quant_research.db")
    args = parser.parse_args()
    payload = json.loads(args.snapshot.read_text(encoding="utf-8"))
    account_ref = str(payload["account_ref"])
    if account_ref.isdigit() and len(account_ref) > 4:
        raise ValueError("Use a masked account_ref (last four digits only).")

    repo = BrokerageMirrorRepository(create_db_engine(args.db_url))
    snapshot_id = repo.save_snapshot(
        provider=payload["provider"],
        account_ref=account_ref,
        account_type=payload["account_type"],
        positions=payload["positions"],
    )
    print(f"Imported snapshot {snapshot_id}: {len(payload['positions'])} positions")


if __name__ == "__main__":
    main()
