from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import func, select

from config.settings import Config
from config.universe import EligibilityRules, INITIAL_ETF_UNIVERSE, UniverseVersion
from execution.models import (
    BrokerEnvironment,
    ExecutionFill,
    OrderIntent,
    Quote,
    Side,
)
from storage.db import create_all, create_db_engine
from storage.repositories.execution import ExecutionRepository
from storage.repositories.experiments import ExperimentRepository
from storage.repositories.governance import GovernanceRepository
from storage.schema import admission_runs, execution_fills, experiment_runs, parameter_trials


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


def _engine():
    engine = create_db_engine("sqlite:///:memory:")
    create_all(engine)
    return engine


def test_universe_and_frozen_strategy_versions_are_immutable():
    engine = _engine()
    repository = GovernanceRepository(engine=engine)
    policy_version = UniverseVersion(
        version="UV-001",
        effective_date="2026-07-17",
        seed_tickers=INITIAL_ETF_UNIVERSE,
        rules=EligibilityRules(),
        approved=True,
        approved_by="operator",
    )
    repository.save_universe_version(policy_version)
    repository.save_universe_version(policy_version)
    with pytest.raises(ValueError, match="immutable"):
        repository.save_universe_version(
            UniverseVersion(
                version="UV-001",
                effective_date="2026-10-01",
                seed_tickers=INITIAL_ETF_UNIVERSE,
                rules=policy_version.rules,
                approved=True,
            )
        )

    repository.save_strategy_version(
        version="SV-001",
        universe_version="UV-001",
        protocol={"hash": "one"},
        dataset_snapshot_id=1,
        code_commit="a" * 40,
        frozen=True,
    )
    with pytest.raises(ValueError, match="Frozen strategy"):
        repository.save_strategy_version(
            version="SV-001",
            universe_version="UV-001",
            protocol={"hash": "changed"},
            dataset_snapshot_id=1,
            code_commit="a" * 40,
            frozen=True,
        )
    assert repository.is_universe_approved("UV-001") is True
    assert repository.is_strategy_frozen("SV-001") is True


def test_experiment_repository_forces_legacy_invalid_and_daily_weekly_exploratory():
    engine = _engine()
    repository = ExperimentRepository(engine=engine)
    summary = pd.Series({"Start Equity": 10_000.0, "End Equity": 10_100.0})
    signal = {"date": "2026-07-17", "regime": "neutral", "weights": {"BIL": 1.0}}
    legacy_id = repository.save_run(
        scenario_name="legacy",
        config=Config(),
        summary=summary,
        latest_signal=signal,
    )
    exploratory_id = repository.save_run(
        scenario_name="weekly",
        config=Config(rebalance_frequency="W", strategy_version="SV-001"),
        summary=summary,
        latest_signal=signal,
        dataset_snapshot_id=1,
        admissible=True,
    )
    with engine.connect() as connection:
        rows = {
            row.id: row
            for row in connection.execute(
                select(experiment_runs).where(
                    experiment_runs.c.id.in_([legacy_id, exploratory_id])
                )
            )
        }
    assert rows[legacy_id].status == "invalid_data_v1"
    assert rows[legacy_id].admissible == 0
    assert rows[exploratory_id].status == "exploratory_only"
    assert rows[exploratory_id].admissible == 0


def test_execution_repository_is_environment_scoped_and_fill_idempotent():
    engine = _engine()
    repository = ExecutionRepository(engine=engine, environment="PAPER")
    quote = Quote("SPY", 99.95, 100.05, NOW)
    intent = OrderIntent(
        client_order_id="client-1",
        environment=BrokerEnvironment.PAPER,
        strategy_version="SV-001",
        signal_session="2026-07-16",
        ticker="SPY",
        side=Side.BUY,
        quantity=1.0,
        limit_price=100.02,
        arrival_quote=quote,
        adv_fraction=0.0001,
        estimated_impact_bps=0.0,
        created_at=NOW,
    )
    intent_id = repository.save_intent(intent)
    fill = ExecutionFill(
        client_order_id="client-1",
        broker_execution_id="exec-1",
        filled_at=NOW,
        quantity=1.0,
        price=100.0,
        commission=0.35,
        implementation_shortfall_bps=-2.0,
    )
    repository.save_fill(intent_id, fill)
    repository.save_fill(intent_id, fill)
    with engine.connect() as connection:
        count = connection.execute(select(func.count()).select_from(execution_fills)).scalar_one()
    assert count == 1

    intent.environment = BrokerEnvironment.LIVE
    with pytest.raises(ValueError, match="environment mismatch"):
        repository.save_intent(intent)

    live_repository = ExecutionRepository(engine=engine, environment="LIVE")
    live_intent_id = live_repository.save_intent(intent)
    live_repository.save_fill(live_intent_id, fill)
    with engine.connect() as connection:
        count = connection.execute(
            select(func.count()).select_from(execution_fills)
        ).scalar_one()
    assert count == 2
    with pytest.raises(ValueError, match="does not belong"):
        repository.save_fill(live_intent_id, fill)


def test_admission_storage_normalizes_timestamps_and_nonfinite_trial_values():
    engine = _engine()
    repository = GovernanceRepository(engine=engine)

    admission_id = repository.save_admission(
        strategy_version="SV-001",
        methodology="nested_expanding_v3",
        status="rejected",
        results={
            "selection_uses_future_holdout": False,
            "freeze_date": pd.Timestamp("2026-07-17"),
            "failed_score": np.nan,
        },
        trials=[
            {
                "label": "candidate-1",
                "parameters": {"target_vol": np.float64(0.10)},
                "folds": [{"validation_start": pd.Timestamp("2020-01-02")}],
                "status": "failed",
                "score": float("-inf"),
            }
        ],
    )

    with engine.connect() as connection:
        run = connection.execute(
            select(admission_runs).where(admission_runs.c.id == admission_id)
        ).mappings().one()
        trial = connection.execute(
            select(parameter_trials).where(
                parameter_trials.c.admission_run_id == admission_id
            )
        ).mappings().one()
    assert run["results_json"]["freeze_date"].startswith("2026-07-17")
    assert run["results_json"]["failed_score"] is None
    assert trial["parameters_json"]["target_vol"] == 0.10
    assert trial["score"] is None
