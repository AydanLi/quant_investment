"""Incremental migration preserves unknown legacy evidence and refuses unsafe rollback."""
from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config as AlembicConfig
import pytest
from sqlalchemy import inspect

from storage.db import create_db_engine


def test_incremental_migration_keeps_legacy_evidence_unsigned(tmp_path, monkeypatch):
    database = tmp_path / "migration.db"
    monkeypatch.setenv("QUANT_DB_URL", f"sqlite:///{database.as_posix()}")
    config = AlembicConfig(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "5f74c1a9d2b0")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO experiment_runs(scenario_name, config_json, config_hash) VALUES ('legacy', '{}', 'old-hash')")
        connection.execute("INSERT INTO corporate_actions(ticker, ex_date, action_type, source) VALUES ('SPY', '2024-01-02', 'dividend', 'old-source')")
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT config_hash,summary_json,runtime_hash FROM experiment_runs").fetchone() == ("old-hash", None, None)
        assert connection.execute("SELECT payment_date,payment_source FROM corporate_actions").fetchone() == (None, None)
        assert connection.execute("SELECT COUNT(*) FROM paper_account_closes").fetchone()[0] == 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    command.downgrade(config, "5f74c1a9d2b0")
    engine = create_db_engine(f"sqlite:///{database.as_posix()}")
    assert "summary_json" not in {item["name"] for item in inspect(engine).get_columns("experiment_runs")}
    engine.dispose()
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO validation_runs(strategy_version,runtime_hash,environment,execution_model,account_ref,started_at,status) VALUES ('fixture','hash','PAPER','LOCAL_REPLAY','account',CURRENT_TIMESTAMP,'RUNNING')")
    with pytest.raises(RuntimeError, match="Cannot discard"):
        command.downgrade(config, "5f74c1a9d2b0")
