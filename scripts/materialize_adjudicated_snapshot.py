"""Materialize snapshot-bound quality decisions without refetching providers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sqlalchemy import select

from config.settings import Config
from data.models import DataQualityReport, QualitySeverity
from storage.db import create_db_engine
from storage.repositories.trusted_data import TrustedMarketDataRepository
from storage.schema import dataset_snapshots


def _database_url(database: str) -> str:
    return database if "://" in database else f"sqlite:///{Path(database).as_posix()}"


def materialize_adjudicated_snapshot(
    repository: TrustedMarketDataRepository,
    source_snapshot_id: int,
    *,
    config: Config,
) -> tuple[int, DataQualityReport]:
    """Create a derived immutable snapshot from exact stored decisions and raw rows."""
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
        raise ValueError("Only a raw source snapshot may be adjudicated.")
    decisions = repository.quality_decisions(source_snapshot_id)
    if not decisions:
        raise ValueError("The source snapshot has no data-quality decisions.")

    payloads = repository.load_snapshot_sources(source_snapshot_id)
    primary = payloads.get("primary")
    secondary = payloads.get("secondary")
    if primary is None:
        raise ValueError("The source snapshot has no immutable primary payload.")

    report = repository.adjudicated_report(source_snapshot_id, config=config)
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
        source = connection.execute(
            select(
                dataset_snapshots.c.start_date,
                dataset_snapshots.c.end_date,
            ).where(dataset_snapshots.c.id == args.source_snapshot_id)
        ).one_or_none()
    if source is None:
        raise SystemExit(f"Unknown dataset snapshot {args.source_snapshot_id}.")

    snapshot_id, report = materialize_adjudicated_snapshot(
        repository,
        args.source_snapshot_id,
        config=Config(start_date=source.start_date or "2006-01-01", end_date=source.end_date),
    )
    unresolved = sum(
        issue.severity == QualitySeverity.BLOCK
        and issue.fingerprint not in set(report.adjudicated_issue_fingerprints)
        for issue in report.issues
    )
    print(
        json.dumps(
            {
                "source_snapshot_id": args.source_snapshot_id,
                "new_snapshot_id": snapshot_id,
                "status": report.status.value,
                "actionable": report.actionable,
                "decision_set_hash": report.decision_set_hash,
                "adjudicated_blocking_issues": len(
                    report.adjudicated_issue_fingerprints
                ),
                "unresolved_blocking_issues": unresolved,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if report.actionable else 2


if __name__ == "__main__":
    raise SystemExit(main())
