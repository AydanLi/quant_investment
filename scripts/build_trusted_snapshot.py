from __future__ import annotations

import argparse
from collections import Counter
from getpass import getpass
import json
import os
from pathlib import Path

import pandas as pd

from config.settings import Config
from data.providers import (
    ProviderError,
    ProviderRateLimitError,
    TiingoMarketDataProvider,
    YahooMarketDataProvider,
)
from data.trusted_loader import TrustedMarketDataLoader
from storage.repositories.trusted_data import TrustedMarketDataRepository


def _database_url(database: str) -> str:
    return (
        database
        if "://" in database
        else f"sqlite:///{Path(database).as_posix()}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build and persist a full immutable Tiingo/Yahoo/CBOE snapshot."
    )
    parser.add_argument("--database", default="quant_research.db")
    parser.add_argument("--start", default="2006-01-01")
    parser.add_argument("--end")
    parser.add_argument("--as-of")
    args = parser.parse_args()

    token = os.getenv("TIINGO_API_TOKEN") or getpass("Tiingo API token: ")
    if not token:
        raise SystemExit("A Tiingo token is required.")
    config = Config(
        start_date=args.start,
        end_date=args.end,
        db_url=_database_url(args.database),
    )
    repository = TrustedMarketDataRepository(db_url=config.db_url)
    loader = TrustedMarketDataLoader(
        config,
        primary_provider=TiingoMarketDataProvider(token=token),
        secondary_provider=YahooMarketDataProvider(),
        repository=repository,
        as_of=None if args.as_of is None else pd.Timestamp(args.as_of),
        persist=True,
    )
    try:
        data = loader.load()
    except ProviderError as exc:
        output = {
            "quality_status": "BLOCKED",
            "actionable": False,
            "error_code": (
                "provider_rate_limit"
                if isinstance(exc, ProviderRateLimitError)
                else "provider_error"
            ),
            "provider": getattr(exc, "provider", None),
            "message": str(exc),
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        raise SystemExit(3) from None
    report = loader.quality_report
    if report is None or loader.dataset_snapshot_id is None:
        raise SystemExit("Snapshot creation did not produce an auditable result.")
    issue_counts = Counter(issue.code for issue in report.issues)
    output = {
        "dataset_snapshot_id": loader.dataset_snapshot_id,
        "quality_status": report.status.value,
        "actionable": report.actionable,
        "latest_session": report.latest_session,
        "stale_sessions": report.stale_sessions,
        "ticker_count": len(data),
        "universe_version": config.universe_version,
        "universe_version_recorded": loader.universe_version_recorded,
        "historical_universe_integrity": config.historical_universe_integrity,
        "issue_counts": dict(sorted(issue_counts.items())),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report.actionable else 2)


if __name__ == "__main__":
    main()
