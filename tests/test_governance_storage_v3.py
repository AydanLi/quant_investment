from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import func, select

from config.settings import Config
from data.models import DATA_QUALITY_MODEL_VERSION
from research.runtime import build_runtime_manifest, capture_code_identity, canonical_hash, config_payload
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
from storage.schema import (
    admission_runs,
    dataset_snapshots,
    execution_fills,
    experiment_runs,
    parameter_trials,
    strategy_versions,
)


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


def _engine():
    engine = create_db_engine("sqlite:///:memory:")
    create_all(engine)
    return engine


def _strategy_draft(engine):
    repository = GovernanceRepository(engine=engine)
    repository.create_universe_draft(
        UniverseVersion(
            version="UV-001",
            effective_date="2026-07-17",
            seed_tickers=INITIAL_ETF_UNIVERSE,
            rules=EligibilityRules(),
        )
    )
    repository.approve_universe_version("UV-001", approved_by="operator")
    with engine.begin() as connection:
        snapshot_id = int(
            connection.execute(
                dataset_snapshots.insert().values(
                    as_of="2026-07-17T21:00:00+00:00",
                    start_date="2026-07-16",
                    end_date="2026-07-17",
                    primary_source="fixture",
                    secondary_source="check",
                    content_hash="a" * 64,
                    status="TRUSTED",
                    decision_set_hash=None,
                    quality_json={
                        "quality_model_version": DATA_QUALITY_MODEL_VERSION,
                        "status": "TRUSTED",
                        "primary_source": "fixture",
                        "secondary_source": "check",
                        "expected_session": "2026-07-17",
                        "latest_session": "2026-07-17",
                        "stale_sessions": 0,
                        "content_hash": "a" * 64,
                        "raw_data_hash": "b" * 64,
                    },
                )
            ).inserted_primary_key[0]
        )
    repository.create_strategy_version(
        version="SV-001",
        universe_version="UV-001",
        protocol={"hash": "one", "base_config": config_payload(Config(strategy_version="SV-001"))},
        dataset_snapshot_id=snapshot_id,
        code_commit=capture_code_identity()["code_commit"],
    )
    return repository, snapshot_id


def _admit_and_freeze(repository):
    with repository.engine.connect() as connection:
        strategy = connection.execute(select(strategy_versions).where(
            strategy_versions.c.version == "SV-001")).mappings().one()
    manifest = build_runtime_manifest(
        Config(strategy_version="SV-001"), code_identity=capture_code_identity(),
        research_cutoff="2026-07-17", dataset_snapshot_id=int(strategy["dataset_snapshot_id"]),
        protocol_hash=canonical_hash(strategy["protocol_json"]),
    )
    admission_id = repository.start_admission(
        strategy_version="SV-001",
        methodology="nested_expanding_v3",
    )
    for index in range(135):
        repository.save_admission_trial(
            admission_id,
            stage="final_selection_summary",
            fold_key="aggregate",
            label=f"candidate-{index:03d}",
            parameters={"index": index},
            folds=[],
            status="evaluated",
            score=float(index),
        )
    repository.finish_admission(
        admission_id,
        status="admitted",
        results={
            "selection_uses_future_holdout": False,
            "admitted": True,
            "gates": {"historical": True},
            "runtime_manifest": manifest.to_dict(),
            "runtime_hash": manifest.runtime_hash,
        },
    )
    repository.freeze_strategy_version("SV-001", admission_run_id=admission_id, approved_by="reviewer")
    return admission_id


def test_config_cannot_claim_point_in_time_universe_without_approved_evidence():
    repository, snapshot_id = _strategy_draft(_engine())
    with pytest.raises(ValueError, match="Historical universe integrity"):
        repository.create_strategy_version(
            version="SV-UNVERIFIED-PIT", universe_version="UV-001",
            protocol={"base_config": config_payload(Config(historical_universe_integrity=True))},
            dataset_snapshot_id=snapshot_id, code_commit=capture_code_identity()["code_commit"],
        )


def test_universe_strategy_and_admission_lifecycle_is_fail_closed():
    engine = _engine()
    repository = GovernanceRepository(engine=engine)
    policy_version = UniverseVersion(
        version="UV-001",
        effective_date="2026-07-17",
        seed_tickers=INITIAL_ETF_UNIVERSE,
        rules=EligibilityRules(),
    )
    repository.create_universe_draft(policy_version)
    assert repository.is_universe_approved("UV-001") is False
    repository.approve_universe_version("UV-001", approved_by="operator")
    repository.create_universe_draft(policy_version)
    with pytest.raises(ValueError, match="immutable"):
        repository.create_universe_draft(
            UniverseVersion(
                version="UV-001",
                effective_date="2026-10-01",
                seed_tickers=INITIAL_ETF_UNIVERSE,
                rules=policy_version.rules,
            )
        )
    with engine.begin() as connection:
        snapshot_id = int(
            connection.execute(
                dataset_snapshots.insert().values(
                    as_of="2026-07-17T21:00:00+00:00",
                    start_date="2026-07-16",
                    end_date="2026-07-17",
                    primary_source="fixture",
                    secondary_source="check",
                    content_hash="b" * 64,
                    status="TRUSTED",
                    decision_set_hash=None,
                    quality_json={
                        "quality_model_version": DATA_QUALITY_MODEL_VERSION,
                        "status": "TRUSTED",
                        "primary_source": "fixture",
                        "secondary_source": "check",
                        "expected_session": "2026-07-17",
                        "latest_session": "2026-07-17",
                        "stale_sessions": 0,
                        "content_hash": "b" * 64,
                        "raw_data_hash": "b" * 64,
                    },
                )
            ).inserted_primary_key[0]
        )
    repository.create_strategy_version(
        version="SV-001",
        universe_version="UV-001",
        protocol={"hash": "one", "base_config": config_payload(Config(strategy_version="SV-001"))},
        dataset_snapshot_id=snapshot_id,
        code_commit=capture_code_identity()["code_commit"],
    )
    with pytest.raises(ValueError, match="AdmissionRun"):
        repository.freeze_strategy_version("SV-001", approved_by="reviewer")
    admission_id = _admit_and_freeze(repository)
    with pytest.raises(ValueError, match="immutable"):
        repository.create_strategy_version(
            version="SV-001",
            universe_version="UV-001",
            protocol={"hash": "changed"},
            dataset_snapshot_id=snapshot_id,
            code_commit=capture_code_identity()["code_commit"],
        )
    assert repository.is_universe_approved("UV-001") is True
    assert repository.is_strategy_frozen("SV-001") is True
    repository.start_local_sim_clock("SV-001")
    with engine.connect() as connection:
        strategy = connection.execute(
            select(strategy_versions).where(strategy_versions.c.version == "SV-001")
        ).mappings().one()
    assert strategy["local_sim_start"] is not None
    with pytest.raises(TypeError):
        repository.start_paper_clock("SV-001", started_at=NOW)
    assert admission_id > 0


def test_admitted_status_requires_complete_final_candidate_trials():
    engine = _engine()
    repository, _ = _strategy_draft(engine)
    admission_id = repository.start_admission(
        strategy_version="SV-001",
        methodology="nested_expanding_v3",
    )
    repository.save_admission_trial(
        admission_id,
        stage="final_selection_summary",
        fold_key="aggregate",
        label="candidate-000",
        parameters={},
        folds=[],
        status="evaluated",
    )

    with pytest.raises(ValueError, match="complete evaluated final candidate"):
        repository.finish_admission(
            admission_id,
            status="admitted",
            results={"admitted": True, "gates": {"historical": True}},
        )

    repository.finish_admission(
        admission_id,
        status="rejected",
        results={"admitted": False, "gates": {"historical": False}},
    )


def test_strategy_creation_rejects_unapproved_universe_and_blocked_snapshot():
    engine = _engine()
    repository = GovernanceRepository(engine=engine)
    repository.create_universe_draft(
        UniverseVersion(
            version="UV-001",
            effective_date="2026-07-17",
            seed_tickers=INITIAL_ETF_UNIVERSE,
            rules=EligibilityRules(),
        )
    )
    with engine.begin() as connection:
        blocked_snapshot_id = int(
            connection.execute(
                dataset_snapshots.insert().values(
                    as_of="2026-07-17T21:00:00+00:00",
                    primary_source="fixture",
                    secondary_source="check",
                    content_hash="c" * 64,
                    status="BLOCKED",
                    quality_json={"status": "BLOCKED", "stale_sessions": 0},
                )
            ).inserted_primary_key[0]
        )

    with pytest.raises(ValueError, match="approved universe"):
        repository.create_strategy_version(
            version="SV-001",
            universe_version="UV-001",
            protocol={},
            dataset_snapshot_id=blocked_snapshot_id,
        )
    repository.approve_universe_version("UV-001", approved_by="operator")
    with pytest.raises(ValueError, match="actionable dataset"):
        repository.create_strategy_version(
            version="SV-001",
            universe_version="UV-001",
            protocol={},
            dataset_snapshot_id=blocked_snapshot_id,
        )
    with pytest.raises(ValueError, match="Unknown strategy"):
        repository.start_admission(
            strategy_version="SV-MISSING",
            methodology="nested_expanding_v3",
        )


def test_experiment_admissibility_is_derived_from_governed_references():
    engine = _engine()
    governance, snapshot_id = _strategy_draft(engine)
    _admit_and_freeze(governance)
    repository = ExperimentRepository(engine=engine)

    run_id = repository.save_run(
        scenario_name="governed",
        config=Config(strategy_version="SV-001"),
        summary=pd.Series({"Start Equity": 10_000.0, "End Equity": 10_100.0}),
        latest_signal={"date": "2026-07-17", "regime": "neutral"},
        dataset_snapshot_id=snapshot_id,
        universe_version="UV-001",
        strategy_version="SV-001",
        admissible=False,
    )

    with engine.connect() as connection:
        row = connection.execute(
            select(experiment_runs).where(experiment_runs.c.id == run_id)
        ).one()
    assert row.admissible == 1
    assert row.status == "complete"


def test_experiment_cannot_forge_admissibility_with_invalid_references():
    engine = _engine()
    repository = ExperimentRepository(engine=engine)

    run_id = repository.save_run(
        scenario_name="forged",
        config=Config(strategy_version="SV-MISSING"),
        summary=pd.Series({"Start Equity": 10_000.0, "End Equity": 20_000.0}),
        latest_signal={"date": "2026-07-17", "regime": "neutral"},
        dataset_snapshot_id=999,
        universe_version="UV-MISSING",
        strategy_version="SV-MISSING",
        admissible=True,
    )

    with engine.connect() as connection:
        row = connection.execute(
            select(experiment_runs).where(experiment_runs.c.id == run_id)
        ).one()
    assert row.admissible == 0
    assert row.status == "blocked_data"
    assert row.dataset_snapshot_id is None
    assert row.universe_version is None
    assert row.strategy_version is None


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
    assert rows[legacy_id].universe_version is None
    assert rows[legacy_id].strategy_version is None
    assert rows[exploratory_id].status == "exploratory_only"
    assert rows[exploratory_id].admissible == 0
    assert rows[exploratory_id].dataset_snapshot_id is None


def test_execution_repository_is_environment_scoped_and_fill_idempotent():
    engine = _engine()
    governance, _ = _strategy_draft(engine)
    _admit_and_freeze(governance)
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
    repository, _ = _strategy_draft(engine)

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
