"""Schema-level guards for governed research and local paper replay."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
import pytest
from sqlalchemy import insert, inspect, text
from sqlalchemy.exc import IntegrityError

from storage.db import create_all, create_db_engine
from storage.schema import (
    admission_runs,
    dataset_snapshots,
    strategy_versions,
    universe_versions,
)


def _engine():
    engine = create_db_engine("sqlite:///:memory:")
    create_all(engine)
    return engine


def test_v4_runtime_tables_are_created():
    engine = _engine()
    names = set(inspect(engine).get_table_names())

    assert {
        "data_quality_decisions",
        "signal_decisions",
        "paper_cycles",
        "paper_accounts",
        "paper_cash_movements",
    } <= names


def test_strategy_and_admission_references_fail_closed():
    engine = _engine()

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                insert(strategy_versions).values(
                    version="SV-MISSING-REFS",
                    universe_version="UV-MISSING",
                    dataset_snapshot_id=999,
                    protocol_json={},
                )
            )

    with engine.begin() as connection:
        snapshot_id = connection.execute(
            insert(dataset_snapshots).values(
                as_of="2026-08-13T20:30:00-04:00",
                primary_source="tiingo",
                secondary_source="yahoo",
                content_hash="a" * 64,
                status="TRUSTED",
                quality_json={},
            )
        ).inserted_primary_key[0]
        connection.execute(
            insert(universe_versions).values(
                version="UV-TEST",
                effective_date="2026-08-13",
                status="approved",
                seed_tickers_json=["SPY", "BIL"],
                rules_json={},
                approved_by="test-operator",
            )
        )
        connection.execute(
            insert(strategy_versions).values(
                version="SV-TEST",
                universe_version="UV-TEST",
                dataset_snapshot_id=snapshot_id,
                protocol_json={},
            )
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                insert(admission_runs).values(
                    strategy_version="SV-UNKNOWN",
                    methodology="nested_expanding_v4",
                    status="RUNNING",
                    results_json={},
                )
            )


def test_migration_resets_legacy_loader_universe_approval(tmp_path, monkeypatch):
    project_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "legacy-auto-universe.db"
    db_url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("QUANT_DB_URL", db_url)
    alembic_config = AlembicConfig(str(project_root / "alembic.ini"))

    command.upgrade(alembic_config, "a14f0c9d7e62")
    engine = create_db_engine(db_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO universe_versions
                    (version, effective_date, status, seed_tickers_json,
                     rules_json, approved_at, approved_by)
                VALUES
                    ('UV-AUTO', '2026-07-17', 'approved', '["SPY", "BIL"]',
                     '{}', CURRENT_TIMESTAMP, 'implementation_plan_2026-07-17')
                """
            )
        )
    engine.dispose()

    command.upgrade(alembic_config, "head")
    upgraded = create_db_engine(db_url)
    with upgraded.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT status, approved_by
                FROM universe_versions
                WHERE version = 'UV-AUTO'
                """
            )
        ).mappings().one()
    assert row["status"] == "draft"
    assert row["approved_by"] == "implementation_plan_2026-07-17"
    upgraded.dispose()
