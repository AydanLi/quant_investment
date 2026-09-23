from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from config.settings import Config
from research.runtime import (
    FrozenRuntimeManifest,
    assert_runtime_matches,
    build_runtime_manifest,
    capture_code_identity,
)


def test_frozen_runtime_roundtrip_rejects_economic_change_and_tampering():
    config = Config(strategy_version="SV-REMEDIATION", top_n=5)
    manifest = build_runtime_manifest(
        config, code_identity=capture_code_identity(),
        research_cutoff="2026-09-01", dataset_snapshot_id=7,
    )
    restored = FrozenRuntimeManifest.from_dict(manifest.to_dict())
    assert restored.runtime_hash == manifest.runtime_hash
    assert restored.to_config(db_url="sqlite:///:memory:").top_n == 5
    assert_runtime_matches(restored.to_config(db_url="sqlite:///:memory:"), restored)
    with pytest.raises(ValueError, match="configuration"):
        assert_runtime_matches(replace(config, top_n=3), restored)
    changed = manifest.to_dict()
    changed["config"]["top_n"] = 3
    with pytest.raises(ValueError, match="hash"):
        FrozenRuntimeManifest.from_dict(changed)


def test_runtime_identity_mismatch_is_not_covered_by_same_strategy_name():
    manifest = build_runtime_manifest(
        Config(strategy_version="SV-REMEDIATION"),
        code_identity={**capture_code_identity(), "source_hash": "0" * 64},
        research_cutoff="2026-09-01", dataset_snapshot_id=7,
    )
    with pytest.raises(ValueError, match="code|identity"):
        assert_runtime_matches(manifest.to_config(), manifest)


def test_research_drawdown_includes_first_loss_from_initial_capital():
    from research.core_evaluator import path_metrics
    from research.model_admission import performance_metrics
    portfolio = pd.DataFrame({"daily_return": [-0.20, 0.10], "benchmark_return": [0.0, 0.0],
        "w_SPY": [1.0, 1.0], "drawdown": [-0.2, -0.12], "stop_triggered": [False, False],
        "turnover": [0.0, 0.0], "est_cost": [0.0, 0.0]})
    assert path_metrics(portfolio, Config()).max_drawdown == pytest.approx(-0.20)
    assert performance_metrics(portfolio).max_drawdown == pytest.approx(-0.20)
    portfolio["unconfirmed_dividend_payments"] = [True, True]
    provisional = path_metrics(portfolio, Config())
    assert provisional.confirmed_cash_flows is False
    assert provisional.selection_score == float("-inf")


def _risk_fixture():
    from research.risk_admission import build_risk_protocol
    parent = build_runtime_manifest(Config(strategy_version="PARENT"),
        code_identity=capture_code_identity(), research_cutoff="2004-12-31", dataset_snapshot_id=1)
    dates = pd.bdate_range("2000-01-03", "2010-12-31")
    data = {"SPY": pd.DataFrame({"Close": 100.0}, index=dates)}
    return parent, data, build_risk_protocol(parent, dataset_snapshot_id=2, holdout_start="2009-01-01")


def test_risk_holdout_mutation_cannot_choose_final_parameters_and_outer_state_is_carried():
    from research.nested_walk_forward import EvaluationMetrics
    from research.risk_admission import run_risk_admission
    parent, data, protocol = _risk_fixture()
    continuations = []

    def evaluator(config, training, validation, *, initial_state=None):
        dates = validation["SPY"].index
        assert training["SPY"].index.max() < dates.min()
        if initial_state is not None:
            assert initial_state == training["SPY"].index.max()
            continuations.append(initial_state)
        dynamic = config.risk_model == "dynamic_factor"
        shock = float(validation["SPY"]["Close"].iloc[0]) < 0
        score = (0.3 + config.ewma_half_life_days / 1000) if dynamic else 0.1
        metric = EvaluationMetrics(excess_sharpe=-score if shock else score,
            net_return=-0.2 if shock else 0.2 if dynamic else 0.1, benchmark_return=0.01,
            max_drawdown=-0.2 if shock else -0.05 if dynamic else -0.1)
        portfolio = pd.DataFrame({"daily_return": 0.001 if dynamic else 0.0001,
            "benchmark_return": 0.0, "w_SPY": 1.0, "stop_triggered": False,
            "drawdown": 0.0}, index=dates)
        return {"metrics": metric, "portfolio": portfolio, "final_state": dates.max()}

    kwargs = {"strategy_version": "CHILD", "evaluator": evaluator,
              "independence_evaluator": lambda *args: {"correlation": 0.1, "observations": 36}}
    original = run_risk_admission(parent, data, protocol, **kwargs)
    changed = {"SPY": data["SPY"].copy()}
    changed["SPY"].loc["2009-01-01":, "Close"] = -100.0
    shocked = run_risk_admission(parent, changed, protocol, **kwargs)
    assert original["admitted"] is True
    assert shocked["admitted"] is False
    assert original["selected_label"] == shocked["selected_label"]
    assert continuations
    final_trials = [row for row in original["trials"] if row["stage"] == "final_selection_summary"]
    assert len(final_trials) == 6
    assert all(len(row["folds"]) >= 3 for row in final_trials)
    assert all(pd.Timestamp(row["fold"]["validation_start"]) > pd.Timestamp(parent.research_cutoff)
               for row in original["outer_folds"])


def test_insufficient_post_core_samples_never_evaluate_or_admit_extension():
    from research.risk_admission import run_risk_admission
    parent, data, protocol = _risk_fixture()
    short = {"SPY": data["SPY"].loc[:"2008-03-01"]}
    def forbidden(*args, **kwargs):
        raise AssertionError("Insufficient evidence must fail before candidate/holdout evaluation.")
    result = run_risk_admission(parent, short, protocol, strategy_version="CHILD", evaluator=forbidden)
    assert result["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["risk_model_default_remains"] == "sample"
    assert result["trials"] == []


def test_factor_ablations_preserve_score_scale_and_do_not_claim_admission():
    from scripts.research_diagnostics import ablation_configs, run_diagnostics
    from research.nested_walk_forward import EvaluationMetrics
    config = Config()
    ablations = ablation_configs(config)
    no_short = ablations["without_weight_mom_20"]
    assert no_short.weight_mom_20 == 0
    assert sum(getattr(no_short, key) for key in ("weight_mom_20", "weight_mom_60", "weight_mom_120", "weight_low_vol")) == pytest.approx(1.0)
    assert ablations["without_ma200_deviation_filter"].max_allowed_drawdown_from_200d == -1.0
    dates = pd.bdate_range("2020-01-01", periods=300)
    frame = pd.DataFrame({"Open": 100.0, "Close": 100.0, "Volume": 10000.0}, index=dates)
    frame.attrs["corporate_actions"] = ()
    metric = EvaluationMetrics(0.1, 0.1, 0.01, -0.1)
    result = run_diagnostics(config, {"SPY": frame, "BIL": frame.copy()},
        evaluation_start=dates[-20], repeats=1, evaluator=lambda *args: {"metrics": metric})
    assert result["admitted"] is False
    assert result["feature_timing_seconds"]["median"] >= 0
    assert result["ablations"]["baseline"]["peak_python_bytes"] >= 0


def _paper_evidence_database():
    from storage.db import create_db_engine, create_all
    from storage.schema import universe_versions, dataset_snapshots, strategy_versions, validation_runs
    engine = create_db_engine("sqlite:///:memory:")
    create_all(engine)
    manifest = build_runtime_manifest(Config(strategy_version="PAPER-TEST"),
        code_identity=capture_code_identity(), research_cutoff="2020-01-01", dataset_snapshot_id=1)
    start = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    with engine.begin() as conn:
        conn.execute(universe_versions.insert().values(version="UV-001", effective_date="2020-01-01",
            status="approved", seed_tickers_json=["SPY", "BIL"], rules_json={}))
        conn.execute(dataset_snapshots.insert().values(id=1, as_of="2020-01-01", start_date="2010-01-01",
            end_date="2020-01-01", primary_source="test", content_hash="a" * 64,
            status="TRUSTED", quality_json={}))
        conn.execute(strategy_versions.insert().values(version="PAPER-TEST", universe_version="UV-001",
            dataset_snapshot_id=1, status="frozen", frozen_at=start - timedelta(days=800),
            approved_by="reviewer", approved_at=start - timedelta(days=800),
            protocol_json={}, runtime_hash=manifest.runtime_hash, runtime_manifest_json=manifest.to_dict()))
        conn.execute(validation_runs.insert().values(id=1, strategy_version="PAPER-TEST",
            runtime_hash=manifest.runtime_hash, environment="PAPER", execution_model="REPLAY_OPEN",
            account_ref="ACCOUNT", started_at=start, status="active"))
    return engine, manifest, start


def test_paper_evidence_uses_actual_stage_start_and_never_admits_replay_as_broker():
    from research.paper_admission import evaluate_persisted_paper_admission
    from storage.repositories.governance import GovernanceRepository
    engine, manifest, start = _paper_evidence_database()
    result = evaluate_persisted_paper_admission(engine, 1)
    assert 1 <= result["elapsed_days"] < 2
    assert result["gates"]["twelve_months"] is False
    assert result["broker_paper_admitted"] is False and result["live_admitted"] is False
    assert GovernanceRepository(engine=engine).start_validation_run("PAPER-TEST", account_ref="ACCOUNT") == 1
    with pytest.raises(ValueError, match="future"):
        evaluate_persisted_paper_admission(engine, 1, evaluated_at=datetime.now(timezone.utc) + timedelta(days=400))


def test_late_full_day_snapshot_does_not_count_as_prospective_open_fill():
    from research.paper_admission import evaluate_persisted_paper_admission
    from storage.schema import signal_decisions, paper_cycles, order_intents, execution_fills
    engine, manifest, start = _paper_evidence_database()
    generated = start + timedelta(hours=1)
    fill_time = start + timedelta(hours=2)
    with engine.begin() as conn:
        conn.execute(signal_decisions.insert().values(id=1, decision_key="key", environment="PAPER",
            strategy_version="PAPER-TEST", universe_version="UV-001", dataset_snapshot_id=1,
            signal_session=str(start.date()), data_as_of=generated.isoformat(), generated_at=generated,
            recorded_at=generated, next_rebalance_session=str(start.date()), status="ACTIONABLE",
            regime="NORMAL", decision_json={}, runtime_hash=manifest.runtime_hash))
        conn.execute(paper_cycles.insert().values(id=1, environment="PAPER", strategy_version="PAPER-TEST",
            signal_decision_id=1, signal_session=str(start.date()), execution_session=str(start.date()),
            approval_deadline=fill_time, status="COMPLETED", created_at=generated,
            execution_payload_json={"REPLAY_OPEN": {"source_snapshot_id": 1,
                "snapshot_as_of": (fill_time + timedelta(hours=6)).isoformat()}}))
        conn.execute(order_intents.insert().values(id=1, client_order_id="order", environment="PAPER",
            strategy_version="PAPER-TEST", signal_decision_id=1, paper_cycle_id=1,
            created_at=generated, approved_at=generated, approved_by="operator", status="FILLED",
            ticker="SPY", side="BUY", quantity=1, limit_price=100, order_type="REPLAY_OPEN",
            metadata_json={"account_before": {"account_ref": "ACCOUNT"}}))
        conn.execute(execution_fills.insert().values(order_intent_id=1, environment="PAPER",
            filled_at=fill_time, recorded_at=fill_time, quantity=1, price=100,
            implementation_shortfall_bps=2, broker_execution_id="fill"))
    result = evaluate_persisted_paper_admission(engine, 1)
    assert result["evidence"]["fill_count"] == 0
    assert result["excluded_retrospective_fills"] == 1


def test_failed_or_running_risk_attempt_reserves_holdout_across_child_names():
    from storage.schema import dataset_snapshots, strategy_versions
    from storage.repositories.governance import GovernanceRepository
    from research.risk_admission import build_risk_protocol
    from research.runtime import canonical_hash
    engine, parent, _ = _paper_evidence_database()
    protocol = build_risk_protocol(parent, dataset_snapshot_id=1, holdout_start="2024-01-01")
    with engine.begin() as conn:
        conn.execute(dataset_snapshots.update().values(end_date="2025-12-31"))
        for child in ("RISK-ONE", "RISK-TWO"):
            conn.execute(strategy_versions.insert().values(version=child, universe_version="UV-001",
                dataset_snapshot_id=1, status="draft", protocol_json=protocol))
    repository = GovernanceRepository(engine=engine)
    kwargs = {"methodology": "nested_risk_extension_v1", "protocol_hash": canonical_hash(protocol),
              "results": {"parent_runtime_hash": parent.runtime_hash, "evidence_role": "post_core_holdout"}}
    run_id = repository.start_admission(strategy_version="RISK-ONE", **kwargs)
    assert repository.start_admission(strategy_version="RISK-ONE", **kwargs) == run_id
    with pytest.raises(ValueError, match="consumed or reserved"):
        repository.start_admission(strategy_version="RISK-TWO", **kwargs)
    repository.finish_admission(run_id, status="failed", results={"error": "interrupted"})
    with pytest.raises(ValueError, match="consumed or reserved"):
        repository.start_admission(strategy_version="RISK-TWO", **kwargs)


def test_mirror_backtest_consumes_raw_open_close_and_liquidity_frames():
    import numpy as np
    from data.calendar import NyseCalendar
    from data.features import FeatureEngineer
    from scripts.optimize_mirrored_portfolio import _run
    config = Config(universe=["SPY", "BIL"], top_n=1)
    dates = NyseCalendar().sessions("2019-01-02", "2020-12-31")
    data = {}
    for ticker, growth in (("SPY", 0.0005), ("BIL", 0.00002), ("^VIX", 0.0)):
        close = 100 * np.exp(growth * np.arange(len(dates))) if ticker != "^VIX" else np.full(len(dates), 15.0)
        frame = pd.DataFrame({"Open": close * 0.999, "Close": close, "High": close * 1.01,
                              "Low": close * 0.99, "Volume": 10_000_000.0}, index=dates)
        frame.attrs["corporate_actions"] = ()
        data[ticker] = frame
    engineer = FeatureEngineer(data, config)
    prices = engineer.make_price_frame()
    features = engineer.compute_features(prices, engineer.make_returns_frame(prices))
    portfolio = _run(config, prices, features, execution_prices=engineer.make_open_frame(),
        raw_close_prices=engineer.make_raw_close_frame(), median_dollar_volume=engineer.make_median_dollar_volume_frame(),
        corporate_actions=engineer.corporate_actions())
    assert not portfolio.empty
    assert np.isfinite(portfolio["equity"]).all()


def test_core_outer_replays_state_instead_of_reusing_scalar_cache():
    from research.nested_walk_forward import EvaluationMetrics, NestedExpandingAdmissionRunner
    from research.protocol import build_protocol
    protocol = build_protocol(protocol_version="state-test", code_commit=capture_code_identity()["code_commit"],
                              dataset_snapshot_id=1, universe_version="UV-001")
    states_seen = []
    class Evaluator:
        def evaluate_path(self, candidate, training, validation, cost_bps, *, initial_state=None):
            states_seen.append(initial_state)
            end = validation["SPY"].index.max()
            return {"final_state": end, "portfolio": pd.DataFrame(index=validation["SPY"].index),
                    "metrics": EvaluationMetrics(0.1, 0.01, 0.0, -0.01)}
    runner = NestedExpandingAdmissionRunner(protocol, Evaluator(), trial_cache={
        ("outer_evaluation", "one", protocol.candidates[0].label): {
            "protocol_hash": protocol.content_hash, "status": "evaluated", "metrics": {"excess_sharpe": 999.0}}})
    data = {"SPY": pd.DataFrame({"Close": 100.0}, index=pd.bdate_range("2020-01-01", periods=4))}
    first = runner._evaluate(stage="outer_evaluation", fold_key="one", candidate=protocol.candidates[0],
        training={"SPY": data["SPY"].iloc[:1]}, validation={"SPY": data["SPY"].iloc[1:2]}, cost_bps=7.0)
    runner._evaluate(stage="outer_evaluation", fold_key="two", candidate=protocol.candidates[1],
        training={"SPY": data["SPY"].iloc[:2]}, validation={"SPY": data["SPY"].iloc[2:3]}, cost_bps=7.0)
    runner._evaluate(stage="replacement_baseline", fold_key="one", candidate=protocol.candidates[0],
        training={"SPY": data["SPY"].iloc[:1]}, validation={"SPY": data["SPY"].iloc[1:2]}, cost_bps=7.0)
    assert first["metrics"]["excess_sharpe"] == 0.1
    assert states_seen == [None, data["SPY"].index[1], None]


def test_validation_restart_retains_history_and_does_not_reset_another_account():
    from sqlalchemy import select
    from storage.schema import validation_runs
    from storage.repositories.governance import GovernanceRepository
    engine, _, original_start = _paper_evidence_database()
    repository = GovernanceRepository(engine=engine)
    other_id = repository.start_validation_run("PAPER-TEST", account_ref="OTHER")
    replacement_id = repository.restart_paper_clock("PAPER-TEST", account_ref="ACCOUNT", reason="restart after operational repair")
    with engine.connect() as conn:
        rows = {row["id"]: row for row in conn.execute(select(validation_runs)).mappings()}
    assert rows[1]["status"] == "restarted" and rows[1]["started_at"] == original_start
    assert rows[1]["ended_at"] == rows[replacement_id]["started_at"]
    assert rows[replacement_id]["started_at"] > original_start
    assert rows[other_id]["status"] == "active"
    assert repository.start_validation_run("PAPER-TEST", account_ref="ACCOUNT") == replacement_id


def test_legacy_frozen_runtime_without_human_approval_is_not_loadable():
    from storage.schema import strategy_versions
    from storage.repositories.governance import GovernanceRepository
    engine, _, _ = _paper_evidence_database()
    with engine.begin() as conn:
        conn.execute(strategy_versions.update().values(approved_by=None, approved_at=None))
    with pytest.raises(ValueError, match="human strategy-version approval"):
        GovernanceRepository(engine=engine).load_frozen_runtime("PAPER-TEST", verify_code=False)


def test_dynamic_admission_cli_persists_research_without_approving(monkeypatch, capsys):
    import json
    import sys
    from scripts import validate_dynamic_factor_model as dynamic_cli
    parent, _, _ = _risk_fixture()
    completed = []
    class Governance:
        def load_frozen_runtime(self, version):
            return parent
        def create_strategy_version(self, **kwargs):
            completed.append("draft")
        def start_admission(self, **kwargs):
            return 7
        def finish_admission(self, *args, **kwargs):
            completed.append(kwargs["status"])
        def freeze_strategy_version(self, *args, **kwargs):
            raise AssertionError("Research may not approve a strategy version.")
    monkeypatch.setattr(sys, "argv", ["validate_dynamic_factor_model", "--strategy-version", "PARENT",
        "--candidate-version", "CHILD", "--snapshot-id", "2", "--holdout-start", "2009-01-01"])
    monkeypatch.setattr(dynamic_cli, "create_db_engine", lambda *args: object())
    monkeypatch.setattr(dynamic_cli, "GovernanceRepository", lambda **kwargs: Governance())
    monkeypatch.setattr("scripts.run_core_admission.load_immutable_snapshot", lambda *args: {})
    monkeypatch.setattr("research.risk_admission.run_risk_admission", lambda *args, **kwargs: {"admitted": True})
    dynamic_cli.main()
    output = json.loads(capsys.readouterr().out)
    assert completed == ["draft", "admitted"]
    assert output["strategy_approval"] == "SEPARATE_MANUAL_STEP"
