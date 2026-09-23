"""Inspect persisted readiness evidence without writing or granting admission.

This installation intentionally has no live broker path. Local replay never
counts as broker execution evidence, even after its numerical gates pass.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import pandas as pd

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from data.models import DATA_QUALITY_MODEL_VERSION, DataQualityReport
from data.calendar import NyseCalendar
from research.runtime import FrozenRuntimeManifest, assert_code_identity


def build_readiness_report(database: Path) -> dict[str, object]:
    database = Path(database).resolve(strict=True)
    blockers = []
    facts = {}
    now = datetime.now(timezone.utc)
    config = AlembicConfig(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    head = ScriptDirectory.from_config(config).get_current_head()
    # Do not use the application engine here: it initializes WAL and some
    # callers create tables. This inspection also supports pre-migration DBs.
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        revision = (connection.execute("SELECT version_num FROM alembic_version").fetchone()
                    if "alembic_version" in tables else None)
        facts["schema_revision"] = None if revision is None else revision[0]
        if facts["schema_revision"] != head:
            blockers.append("SCHEMA_MIGRATION_REQUIRED")
        snapshots = (connection.execute("SELECT id,status,quality_json FROM dataset_snapshots ORDER BY id").fetchall()
                     if "dataset_snapshots" in tables else [])
        decisions = (connection.execute("SELECT source_snapshot_id,issue_fingerprint FROM data_quality_decisions").fetchall()
                     if "data_quality_decisions" in tables else [])
        adjudicated = {(row[0], row[1]) for row in decisions}
        snapshot_facts = []
        for snapshot in snapshots:
            quality = DataQualityReport.from_dict(json.loads(snapshot["quality_json"]))
            applied = set(quality.adjudicated_issue_fingerprints)
            pending = sum(issue.severity.value == "BLOCK" and issue.fingerprint not in applied
                          and (snapshot["id"], issue.fingerprint) not in adjudicated
                          for issue in quality.issues)
            snapshot_facts.append({"id": snapshot["id"], "status": snapshot["status"],
                                   "unadjudicated_blocking_issues": pending,
                                   "quality_model_version": quality.quality_model_version,
                                   "latest_session": quality.latest_session})
        facts["snapshots"] = snapshot_facts
        latest = snapshots[-1] if snapshots else None
        if latest is None or latest["status"] not in {"TRUSTED", "WARNING", "TRUSTED_WITH_EXCEPTIONS"}:
            blockers.append("ACTIONABLE_REQUALIFIED_SNAPSHOT_REQUIRED")
        if not snapshot_facts or snapshot_facts[-1]["quality_model_version"] != DATA_QUALITY_MODEL_VERSION:
            blockers.append("CURRENT_QUALITY_MODEL_REQUALIFICATION_REQUIRED")
        # Recompute freshness today instead of trusting an old zero-staleness flag.
        expected_session = str(NyseCalendar().latest_completed_session(pd.Timestamp(now)).date())
        facts["expected_session"] = expected_session
        if not snapshot_facts or snapshot_facts[-1]["latest_session"] != expected_session:
            blockers.append("CURRENT_SESSION_DATA_RECHECK_REQUIRED")
        universes = (connection.execute("SELECT version,status,historical_universe_integrity FROM universe_versions").fetchall()
                     if "universe_versions" in tables else [])
        facts["universes"] = [dict(row) for row in universes]
        if not any(row["status"] == "approved" for row in universes):
            blockers.append("APPROVED_UNIVERSE_REQUIRED")
        if not any(row["status"] == "approved" and row["historical_universe_integrity"] for row in universes):
            blockers.append("HISTORICAL_UNIVERSE_EVIDENCE_REQUIRED")
        strategies = (connection.execute("SELECT * FROM strategy_versions WHERE status='frozen'").fetchall()
                      if "strategy_versions" in tables else [])
        verified = []
        for strategy in strategies:
            record = dict(strategy)
            if not str(record.get("approved_by") or "").strip() or not record.get("approved_at"):
                continue
            try:
                manifest = FrozenRuntimeManifest.from_dict(json.loads(record.get("runtime_manifest_json") or "{}"))
                assert_code_identity(manifest.code_identity)
                if manifest.runtime_hash == record.get("runtime_hash"):
                    verified.append(record["version"])
            except (TypeError, ValueError):
                continue
        facts["verified_frozen_versions"] = verified
        if not verified:
            blockers.append("NEW_RESEARCH_AND_FROZEN_RUNTIME_REQUIRED")
        for table in ("admission_runs", "paper_cycles", "execution_fills", "validation_runs"):
            facts[table + "_count"] = (connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                                        if table in tables else 0)
        admitted = (connection.execute("SELECT COUNT(*) FROM admission_runs WHERE status='admitted' AND completed_at IS NOT NULL").fetchone()[0]
                    if "admission_runs" in tables else 0)
        if not admitted:
            blockers.append("HISTORICAL_ADMISSION_REQUIRED")
        facts["validation_runs"] = ([dict(row) for row in connection.execute("SELECT * FROM validation_runs ORDER BY id")]
                                     if "validation_runs" in tables else [])
    blockers.extend(["UNTOUCHED_FORWARD_SAMPLE_REQUIRED", "BROKER_EVIDENCE_REQUIRED", "LIVE_CONNECTIVITY_DISABLED"])
    if facts["schema_revision"] == head and facts["validation_runs"]:
        from research.paper_admission import evaluate_persisted_paper_admission
        engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(database.as_uri() + "?mode=ro", uri=True))
        try:
            facts["forward_evidence"] = [evaluate_persisted_paper_admission(engine, row["id"])
                                         for row in facts["validation_runs"]]
        finally:
            engine.dispose()
    return {"as_of": now.isoformat(), "database": str(database),
            "ready_for_live": False, "blockers": blockers, "facts": facts,
            "interpretation": "A read-only prerequisite report, not an admission decision. Local replay is not broker evidence."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("quant_research.db"))
    args = parser.parse_args()
    report = build_readiness_report(args.database)
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
