"""Record one snapshot-bound, evidence-backed data-quality decision."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from sqlalchemy import select

from data.models import (
    DataQualityDecision,
    DataQualityDisposition,
    DataQualityReport,
)
from storage.db import create_db_engine
from storage.repositories.trusted_data import TrustedMarketDataRepository
from storage.schema import dataset_snapshots


def _database_url(database: str) -> str:
    return database if "://" in database else f"sqlite:///{Path(database).as_posix()}"


def _json_object(raw: str, name: str) -> dict[str, object]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object.")
    return value


def record_decision(
    repository: TrustedMarketDataRepository,
    *,
    snapshot_id: int,
    issue_fingerprint: str,
    issue_code: str,
    ticker: str,
    start_date: str,
    end_date: str,
    normalization: dict[str, object],
    evidence: dict[str, object],
    reason: str,
    decided_by: str,
) -> int:
    with repository.engine.connect() as connection:
        quality = connection.execute(
            select(dataset_snapshots.c.quality_json).where(
                dataset_snapshots.c.id == int(snapshot_id)
            )
        ).scalar_one_or_none()
    if quality is None:
        raise ValueError(f"Unknown dataset snapshot {snapshot_id}.")
    report = DataQualityReport.from_dict(quality)
    if not report.raw_data_hash:
        raise ValueError(
            "Snapshot has no raw_data_hash; requalify or rebuild it before adjudication."
        )
    decision = DataQualityDecision(
        source_snapshot_id=snapshot_id,
        issue_fingerprint=issue_fingerprint,
        issue_code=issue_code,
        ticker=ticker,
        start_date=start_date,
        end_date=end_date,
        raw_data_hash=report.raw_data_hash,
        disposition=DataQualityDisposition.ACCEPTED_EXCEPTION,
        normalization=normalization,
        evidence=evidence,
        reason=reason,
        decided_by=decided_by,
        decided_at=datetime.now(timezone.utc),
    )
    return repository.save_quality_decision(decision)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="quant_research.db")
    parser.add_argument("--snapshot-id", required=True, type=int)
    parser.add_argument("--issue-fingerprint", required=True)
    parser.add_argument("--issue-code", required=True)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--normalization-json", required=True)
    parser.add_argument("--evidence-json", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--decided-by", required=True)
    args = parser.parse_args()

    repository = TrustedMarketDataRepository(
        engine=create_db_engine(_database_url(args.database))
    )
    decision_id = record_decision(
        repository,
        snapshot_id=args.snapshot_id,
        issue_fingerprint=args.issue_fingerprint,
        issue_code=args.issue_code,
        ticker=args.ticker,
        start_date=args.start_date,
        end_date=args.end_date,
        normalization=_json_object(args.normalization_json, "normalization-json"),
        evidence=_json_object(args.evidence_json, "evidence-json"),
        reason=args.reason,
        decided_by=args.decided_by,
    )
    print(f"Recorded immutable data-quality decision {decision_id}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

