"""Re-run current quality rules on an immutable stored dual-source snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from config.settings import Config
from data.models import DataQualityReport
from data.quality import assess_market_data_quality
from storage.db import create_db_engine
from storage.repositories.trusted_data import TrustedMarketDataRepository
from storage.schema import dataset_snapshots


def _database_url(database: str) -> str:
    return database if "://" in database else f"sqlite:///{Path(database).as_posix()}"


def requalify_snapshot(
    repository: TrustedMarketDataRepository,
    source_snapshot_id: int,
    *,
    config: Config,
) -> tuple[int, DataQualityReport]:
    """Create a new source snapshot; never mutate or inherit old decisions."""
    with repository.engine.connect() as connection:
        source = connection.execute(
            select(dataset_snapshots).where(
                dataset_snapshots.c.id == int(source_snapshot_id)
            )
        ).mappings().one_or_none()
    if source is None:
        raise ValueError(f"Unknown dataset snapshot {source_snapshot_id}.")
    source_report = DataQualityReport.from_dict(source["quality_json"])
    if source_report.source_snapshot_id is not None:
        raise ValueError("A derived adjudicated snapshot cannot be requalified as raw.")

    payloads = repository.load_snapshot_sources(source_snapshot_id)
    primary = payloads.get("primary")
    secondary = payloads.get("secondary")
    if primary is None or secondary is None:
        raise ValueError("Requalification requires both immutable source roles.")

    required = sorted(
        set(config.universe + [config.benchmark, config.fear_gauge])
    )
    missing = sorted(set(required) - set(primary.bars))
    if missing:
        raise ValueError(
            "Stored snapshot is missing configured tickers: " + ", ".join(missing)
        )
    report = assess_market_data_quality(
        primary,
        secondary,
        required_tickers=required,
        config=config,
        as_of=pd.Timestamp(source["as_of"]),
    )
    source_by_ticker = {
        ticker: str(primary.metadata.get(ticker, {}).get("source", primary.source))
        for ticker in primary.bars
    }
    snapshot_id = repository.create_snapshot(
        report,
        as_of=str(source["as_of"]),
        start_date=source["start_date"],
        end_date=source["end_date"],
        bars=primary.bars,
        actions=primary.actions,
        source_by_ticker=source_by_ticker,
        secondary_payload=secondary,
    )
    return snapshot_id, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="quant_research.db")
    parser.add_argument("--source-snapshot-id", required=True, type=int)
    args = parser.parse_args()

    engine = create_db_engine(_database_url(args.database))
    repository = TrustedMarketDataRepository(engine=engine)
    with engine.connect() as connection:
        row = connection.execute(
            select(
                dataset_snapshots.c.start_date,
                dataset_snapshots.c.end_date,
            ).where(dataset_snapshots.c.id == args.source_snapshot_id)
        ).one_or_none()
    if row is None:
        raise SystemExit(f"Unknown dataset snapshot {args.source_snapshot_id}.")
    config = Config(start_date=row.start_date or "2006-01-01", end_date=row.end_date)
    snapshot_id, report = requalify_snapshot(
        repository,
        args.source_snapshot_id,
        config=config,
    )
    print(
        json.dumps(
            {
                "source_snapshot_id": args.source_snapshot_id,
                "new_snapshot_id": snapshot_id,
                "status": report.status.value,
                "raw_data_hash": report.raw_data_hash,
                "actionable": report.actionable,
                "decisions_inherited": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report.actionable else 2


if __name__ == "__main__":
    raise SystemExit(main())
