from __future__ import annotations

import argparse
from collections import Counter
from getpass import getpass
import json
import os

import pandas as pd

from config.settings import Config
from data.providers import (
    ProviderError,
    ProviderRateLimitError,
    TiingoMarketDataProvider,
    YahooMarketDataProvider,
)
from data.trusted_loader import TrustedMarketDataLoader


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only Tiingo/Yahoo/CBOE entitlement and quality smoke test."
    )
    parser.add_argument("--tickers", nargs="+", default=["SPY", "BIL"])
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end")
    parser.add_argument("--as-of")
    args = parser.parse_args()

    token = os.getenv("TIINGO_API_TOKEN") or getpass("Tiingo API token: ")
    if not token:
        raise SystemExit("A Tiingo token is required.")
    config = Config(
        start_date=args.start,
        end_date=args.end,
        universe=list(dict.fromkeys(ticker.upper() for ticker in args.tickers)),
    )
    loader = TrustedMarketDataLoader(
        config,
        primary_provider=TiingoMarketDataProvider(token=token),
        secondary_provider=YahooMarketDataProvider(),
        persist=False,
        as_of=None if args.as_of is None else pd.Timestamp(args.as_of),
    )
    try:
        data = loader.load()
    except ProviderError as exc:
        result = {
            "credential_storage": "memory_only",
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
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(3) from None
    report = loader.quality_report
    if report is None:
        raise SystemExit("No quality report was produced.")
    issue_counts = Counter(issue.code for issue in report.issues)
    result = {
        "credential_storage": "memory_only",
        "providers": {
            "primary": report.primary_source,
            "secondary": report.secondary_source,
        },
        "tickers_returned": sorted(data),
        "metadata_fields": {
            ticker: sorted(loader.primary_payload.metadata.get(ticker, {}))
            for ticker in config.universe
        },
        "corporate_action_count": len(loader.primary_payload.actions),
        "quality_status": report.status.value,
        "actionable": report.actionable,
        "expected_session": report.expected_session,
        "latest_session": report.latest_session,
        "stale_sessions": report.stale_sessions,
        "issue_counts": dict(sorted(issue_counts.items())),
        "blocking_issues": [
            {
                "code": issue.code,
                "ticker": issue.ticker,
                "session": issue.session,
                "value": issue.value,
            }
            for issue in report.issues
            if issue.severity.value == "BLOCK"
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report.actionable else 2)


if __name__ == "__main__":
    main()
