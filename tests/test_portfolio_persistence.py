from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
import pandas as pd
import pytest
from sqlalchemy import inspect, text

from storage.db import create_all, create_db_engine
from storage.repositories.portfolio import PortfolioRepository
from storage.schema import experiment_runs


def _create_run(engine) -> int:
    with engine.begin() as conn:
        result = conn.execute(experiment_runs.insert().values(config_json={}))
        return int(result.inserted_primary_key[0])


def test_portfolio_repository_persists_cost_components_without_fabricating_old_data():
    engine = create_db_engine("sqlite:///:memory:")
    create_all(engine)
    repository = PortfolioRepository(engine=engine)

    current_run_id = _create_run(engine)
    current = pd.DataFrame(
        {
            "equity": [1007.98],
            "gross_return": [0.01],
            "daily_return": [0.00798],
            "regime": ["neutral"],
            "turnover": [2.0],
            "est_trading_cost": [0.001],
            "est_slippage": [0.0004],
            "est_cost": [0.0014],
            "w_SPY": [1.0],
        },
        index=pd.to_datetime(["2026-07-16"]),
    )
    repository.save(current_run_id, current)

    stored = repository.get_daily(current_run_id).iloc[0]
    assert stored["gross_return"] == pytest.approx(0.01)
    assert stored["daily_return"] == pytest.approx(0.00798)
    assert stored["est_trading_cost"] == pytest.approx(0.001)
    assert stored["est_slippage"] == pytest.approx(0.0004)
    assert stored["est_cost"] == pytest.approx(0.0014)

    legacy_run_id = _create_run(engine)
    legacy = current.drop(
        columns=["gross_return", "est_trading_cost", "est_slippage"]
    )
    repository.save(legacy_run_id, legacy)

    legacy_stored = repository.get_daily(legacy_run_id).iloc[0]
    assert pd.isna(legacy_stored["gross_return"])
    assert pd.isna(legacy_stored["est_trading_cost"])
    assert pd.isna(legacy_stored["est_slippage"])
    assert legacy_stored["est_cost"] == pytest.approx(0.0014)


def test_migration_preserves_legacy_rows_as_null_and_is_reversible(
    tmp_path,
    monkeypatch,
):
    project_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "migration.db"
    db_url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("QUANT_DB_URL", db_url)
    alembic_config = AlembicConfig(str(project_root / "alembic.ini"))

    command.upgrade(alembic_config, "b91e2f08c4a1")
    engine = create_db_engine(db_url)
    with engine.begin() as conn:
        run_id = conn.execute(
            text("INSERT INTO experiment_runs (config_json) VALUES ('{}')")
        ).lastrowid
        conn.execute(
            text(
                """
                INSERT INTO portfolio_daily
                    (run_id, date, equity, daily_return, regime, turnover, est_cost)
                VALUES
                    (:run_id, '2026-07-15', 1000.0, 0.01, 'neutral', 1.5, 0.0007)
                """
            ),
            {"run_id": run_id},
        )
    engine.dispose()

    command.upgrade(alembic_config, "head")
    upgraded_engine = create_db_engine(db_url)
    upgraded_columns = {
        column["name"] for column in inspect(upgraded_engine).get_columns(
            "portfolio_daily"
        )
    }
    assert {
        "gross_return",
        "est_trading_cost",
        "est_slippage",
    }.issubset(upgraded_columns)
    with upgraded_engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT gross_return, est_trading_cost, est_slippage, est_cost
                FROM portfolio_daily
                """
            )
        ).mappings().one()
    assert row["gross_return"] is None
    assert row["est_trading_cost"] is None
    assert row["est_slippage"] is None
    assert row["est_cost"] == pytest.approx(0.0007)
    upgraded_engine.dispose()

    command.downgrade(alembic_config, "b91e2f08c4a1")
    downgraded_engine = create_db_engine(db_url)
    downgraded_columns = {
        column["name"] for column in inspect(downgraded_engine).get_columns(
            "portfolio_daily"
        )
    }
    assert "gross_return" not in downgraded_columns
    assert "est_trading_cost" not in downgraded_columns
    assert "est_slippage" not in downgraded_columns
    with downgraded_engine.connect() as conn:
        preserved_cost = conn.execute(
            text("SELECT est_cost FROM portfolio_daily")
        ).scalar_one()
    assert preserved_cost == pytest.approx(0.0007)
    downgraded_engine.dispose()
