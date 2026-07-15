"""Import a normalized, read-only brokerage snapshot from JSON.

The input object must contain provider, masked account_ref, account_type, and a
positions array.  Credentials and full brokerage account numbers are rejected.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Mapping

from storage.db import create_db_engine
from storage.repositories.brokerage_mirror import BrokerageMirrorRepository


_SENSITIVE_FIELDS = {
    "account",
    "account_id",
    "account_number",
    "api_key",
    "authorization",
    "auth",
    "authentication",
    "client_secret",
    "cookie",
    "cookies",
    "credentials",
    "full_account_number",
    "login",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "session",
    "session_id",
    "token",
    "username",
}


def _normalized_key(value: Any) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(value).strip())
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _reject_sensitive_fields(value: Any, path: str = "snapshot") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = _normalized_key(key)
            if (
                normalized in _SENSITIVE_FIELDS
                or normalized.endswith("_password")
                or normalized.endswith("_secret")
                or normalized.endswith("_token")
            ):
                raise ValueError(
                    f"Sensitive field {path}.{key} is not allowed in a snapshot."
                )
            _reject_sensitive_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_fields(child, f"{path}[{index}]")


def validate_snapshot_payload(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("Brokerage snapshot JSON must be an object.")
    _reject_sensitive_fields(payload)
    required = {"provider", "account_ref", "account_type", "positions"}
    missing = sorted(required.difference(payload))
    if missing:
        raise ValueError(f"Brokerage snapshot is missing fields: {missing}")
    if not isinstance(payload["positions"], list):
        raise ValueError("Brokerage snapshot positions must be an array.")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--db-url", default="sqlite:///quant_research.db")
    args = parser.parse_args()
    payload = validate_snapshot_payload(
        json.loads(args.snapshot.read_text(encoding="utf-8"))
    )

    repo = BrokerageMirrorRepository(create_db_engine(args.db_url))
    snapshot_id = repo.save_snapshot(
        provider=payload["provider"],
        account_ref=payload["account_ref"],
        account_type=payload["account_type"],
        positions=payload["positions"],
    )
    print(f"Imported snapshot {snapshot_id}: {len(payload['positions'])} positions")


if __name__ == "__main__":
    main()
