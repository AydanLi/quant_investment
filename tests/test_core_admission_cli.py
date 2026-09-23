from __future__ import annotations

import json
import pandas as pd
import pytest
from sqlalchemy import create_engine

from data.models import DATA_QUALITY_MODEL_VERSION, DataQualityReport, DataQualityStatus, ProviderPayload
from data.quality import raw_market_data_hash
from config.universe import EligibilityRules, INITIAL_ETF_UNIVERSE, UniverseVersion
from research.protocol import build_protocol
from research.runtime import capture_code_identity
from scripts import run_core_admission as cli
from storage.db import create_all
from storage.repositories.trusted_data import TrustedMarketDataRepository
from storage.repositories.governance import GovernanceRepository
from storage.schema import admission_runs, dataset_snapshot_bars, strategy_versions


def _protocol():
    return build_protocol(
        protocol_version="cli-test-v1",
        code_commit=capture_code_identity()["code_commit"],
        dataset_snapshot_id=7,
        universe_version="UV-001",
    )


def _metric(
    net=0.10,
    benchmark=0.03,
    *,
    excess_sharpe=0.2,
    max_drawdown=-0.08,
    degenerate=False,
):
    return {
        "excess_sharpe": excess_sharpe,
        "net_return": net,
        "benchmark_return": benchmark,
        "max_drawdown": max_drawdown,
        "stop_count": 0,
        "maximum_stop_overshoot": 0.0,
        "degenerate_all_cash": degenerate,
        "evaluation_status": (
            "DEGENERATE_ALL_CASH" if degenerate else "evaluated"
        ),
    }


def _result(
    protocol,
    *,
    degenerate=False,
    baseline_sharpe=0.10,
    baseline_drawdown=-0.10,
):
    final_trials = [
        {
            "label": candidate.label,
            "parameters": candidate.to_dict(),
            "folds": [],
            "status": "evaluated",
            "score": 0.2,
        }
        for candidate in protocol.candidates
    ]
    fold = {
        "training_start": pd.Timestamp("2010-01-04"),
        "training_end": pd.Timestamp("2014-12-31"),
        "validation_start": pd.Timestamp("2015-01-02"),
        "validation_end": pd.Timestamp("2015-12-31"),
    }
    baseline = cli.fixed_current_baseline_candidate(protocol)
    return {
        "protocol_hash": protocol.content_hash,
        "continuous_outer_account": True,
        "continuous_outer_metrics": {
            "outer_evaluation/7.0": {**_metric(), "confirmed_cash_flows": True},
            "replacement_baseline/7.0": {**_metric(excess_sharpe=baseline_sharpe,
                max_drawdown=baseline_drawdown), "confirmed_cash_flows": True},
        },
        "selection_uses_future_holdout": False,
        "outer_folds": [
            {
                "outer_fold": 1,
                "fold": fold,
                "selected_label": final_trials[0]["label"],
                "cost_scenarios": {
                    "2.0": _metric(),
                    "7.0": _metric(degenerate=degenerate),
                    "20.0": _metric(0.08, 0.03, degenerate=degenerate),
                },
            }
        ],
        "replacement_baseline_folds": [
            {
                "outer_fold": 1,
                "fold": fold,
                "baseline_label": baseline.label,
                "cost_scenarios": {
                    str(cost): _metric(
                        excess_sharpe=baseline_sharpe,
                        max_drawdown=baseline_drawdown,
                    )
                    for cost in protocol.cost_scenarios_bps
                },
            }
        ],
        "replacement_baseline_label": baseline.label,
        "trials": [],
        "final_selection_trials": final_trials,
        "final_selected_label": final_trials[0]["label"],
        "robustness": {
            "neighbor_pass_rate": 0.70,
            "start_date_pass_rate": 0.70,
            "start_date_results": [],
        },
    }


class FakeGovernance:
    def __init__(self, engine=None):
        self.calls = []
        self.trials = {}
        if engine is not None:
            self.engine = engine

    def create_strategy_version(self, **kwargs):
        self.calls.append(("create_strategy_version", kwargs))

    def start_admission(
        self,
        *,
        strategy_version,
        methodology,
        protocol_hash=None,
        results=None,
    ):
        kwargs = {
            "strategy_version": strategy_version,
            "methodology": methodology,
            "protocol_hash": protocol_hash,
            "results": results,
        }
        self.calls.append(("start_admission", kwargs))
        return 11

    def save_admission_trial(self, **kwargs):
        key = (kwargs["stage"], kwargs["fold_key"], kwargs["label"])
        self.trials[key] = kwargs
        self.calls.append(("save_admission_trial", kwargs))

    def finish_admission(
        self,
        admission_run_id,
        *,
        status,
        results,
        error_message=None,
    ):
        kwargs = {
            "admission_run_id": admission_run_id,
            "status": status,
            "results": results,
            "error_message": error_message,
        }
        self.calls.append(("finish_admission", kwargs))

    def freeze_strategy_version(self, **kwargs):
        self.calls.append(("freeze_strategy_version", kwargs))

    def start_local_sim_clock(self, **kwargs):
        self.calls.append(("start_local_sim_clock", kwargs))


def _runner(result, *, fail=False):
    class FakeRunner:
        def __init__(self, protocol, evaluator, trial_callback, trial_cache=None):
            self.protocol = protocol
            self.trial_callback = trial_callback
            self.trial_cache = trial_cache or {}

        def run(self, data):
            self.trial_callback(
                {
                    "stage": "inner_selection",
                    "fold_key": "outer-001/inner-001",
                    "label": self.protocol.candidates[0].label,
                    "parameters": self.protocol.candidates[0].to_dict(),
                    "cost_bps": 7.0,
                    "status": "evaluated",
                    "metrics": {"excess_sharpe": 0.2},
                    "score": 0.2,
                }
            )
            if fail:
                raise RuntimeError("interrupted")
            for baseline_fold in result["replacement_baseline_folds"]:
                for cost, metrics in baseline_fold["cost_scenarios"].items():
                    self.trial_callback(
                        {
                            "stage": "replacement_baseline",
                            "fold_key": (
                                f"outer-{baseline_fold['outer_fold']:03d}/"
                                f"cost-{float(cost):.1f}"
                            ),
                            "label": baseline_fold["baseline_label"],
                            "parameters": cli.fixed_current_baseline_candidate(
                                self.protocol
                            ).to_dict(),
                            "cost_bps": float(cost),
                            "status": metrics["evaluation_status"],
                            "metrics": metrics,
                            "score": metrics["excess_sharpe"],
                        }
                    )
            return result

    return FakeRunner


def test_cli_admission_never_approves_a_strategy_or_starts_its_clock(monkeypatch):
    protocol = _protocol()
    repository = FakeGovernance()
    monkeypatch.setattr(
        cli,
        "NestedExpandingAdmissionRunner",
        _runner(_result(protocol)),
    )

    output = cli.execute_core_admission(
        protocol=protocol,
        strategy_version="SV-TEST",
        data={"SPY": pd.DataFrame()},
        repository=repository,
        evaluator=lambda *args: None,
    )

    methods = [name for name, _ in repository.calls]
    assert output["status"] == "ADMITTED"
    assert methods[:2] == ["create_strategy_version", "start_admission"]
    assert methods[-1] == "finish_admission"
    assert "freeze_strategy_version" not in methods
    assert "start_local_sim_clock" not in methods
    summaries = [key for key in repository.trials if key[0] == "final_selection_summary"]
    assert len(summaries) == 135
    baselines = [key for key in repository.trials if key[0] == "replacement_baseline"]
    assert len(baselines) == 3


def test_cli_rejects_when_absolute_gates_pass_but_fixed_baseline_hurdle_fails(
    monkeypatch,
):
    protocol = _protocol()
    repository = FakeGovernance()
    result = _result(
        protocol,
        baseline_sharpe=0.18,
        baseline_drawdown=-0.085,
    )
    monkeypatch.setattr(cli, "NestedExpandingAdmissionRunner", _runner(result))

    output = cli.execute_core_admission(
        protocol=protocol,
        strategy_version="SV-BASELINE-REJECT",
        data={"SPY": pd.DataFrame()},
        repository=repository,
        evaluator=lambda *args: None,
    )

    replacement_gates = {"replacement_sharpe", "replacement_drawdown"}
    assert all(
        value for name, value in output["gates"].items() if name not in replacement_gates
    )
    assert output["gates"]["replacement_sharpe"] is False
    assert output["gates"]["replacement_drawdown"] is False
    assert output["status"] == "REJECTED"
    assert "freeze_strategy_version" not in [name for name, _ in repository.calls]


def test_cli_rejects_degenerate_run_without_freezing(monkeypatch):
    protocol = _protocol()
    repository = FakeGovernance()
    monkeypatch.setattr(
        cli,
        "NestedExpandingAdmissionRunner",
        _runner(_result(protocol, degenerate=True)),
    )

    output = cli.execute_core_admission(
        protocol=protocol,
        strategy_version="SV-REJECT",
        data={"SPY": pd.DataFrame()},
        repository=repository,
        evaluator=lambda *args: None,
    )

    methods = [name for name, _ in repository.calls]
    assert output["status"] == "REJECTED"
    assert "freeze_strategy_version" not in methods
    assert "start_local_sim_clock" not in methods


def test_cli_interruption_is_failed_and_stage_fold_upserts_can_resume(monkeypatch):
    protocol = _protocol()
    repository = FakeGovernance()
    monkeypatch.setattr(
        cli,
        "NestedExpandingAdmissionRunner",
        _runner(_result(protocol), fail=True),
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        cli.execute_core_admission(
            protocol=protocol,
            strategy_version="SV-RESUME",
            data={"SPY": pd.DataFrame()},
            repository=repository,
            evaluator=lambda *args: None,
        )

    monkeypatch.setattr(
        cli,
        "NestedExpandingAdmissionRunner",
        _runner(_result(protocol)),
    )
    output = cli.execute_core_admission(
        protocol=protocol,
        strategy_version="SV-RESUME",
        data={"SPY": pd.DataFrame()},
        repository=repository,
        evaluator=lambda *args: None,
    )

    finishes = [kwargs["status"] for name, kwargs in repository.calls if name == "finish_admission"]
    inner = [key for key in repository.trials if key[0] == "inner_selection"]
    assert finishes == ["FAILED", "ADMITTED"]
    assert len(inner) == 1
    assert output["status"] == "ADMITTED"


def test_protocol_loader_rejects_mutation(tmp_path):
    protocol = _protocol()
    path = protocol.write_once(tmp_path / "protocol.json")

    assert cli.load_protocol(path).content_hash == protocol.content_hash
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["maximum_weight"] = 0.40
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="content or hash"):
        cli.load_protocol(path)


def _snapshot(tmp_path, *, stale=0, status=DataQualityStatus.TRUSTED, decision_hash=None):
    path = tmp_path / f"snapshot-{stale}-{status.value}.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}", future=True)
    create_all(engine)
    index = pd.date_range("2026-07-16", periods=2)
    bars = {
        "SPY": pd.DataFrame(
            {
                "Open": [100.0, 101.0],
                "High": [101.0, 102.0],
                "Low": [99.0, 100.0],
                "Close": [100.5, 101.5],
                "Volume": [1_000_000.0, 1_100_000.0],
            },
            index=index,
        )
    }
    provider = ProviderPayload(
        bars=bars,
        actions=(),
        metadata={"SPY": {"source": "fixture"}},
        source="fixture",
    )
    raw_hash = raw_market_data_hash(provider, None)
    report = DataQualityReport(
        quality_model_version=DATA_QUALITY_MODEL_VERSION,
        status=status,
        primary_source="fixture",
        secondary_source=None,
        expected_session="2026-07-17",
        latest_session="2026-07-17",
        stale_sessions=stale,
        content_hash="c" * 64,
        raw_data_hash=raw_hash,
        decision_set_hash=decision_hash,
    )
    snapshot_id = TrustedMarketDataRepository(engine=engine).create_snapshot(
        report,
        as_of="2026-07-17T21:00:00Z",
        start_date="2026-07-16",
        end_date="2026-07-17",
        bars=bars,
        actions=(),
        source_by_ticker={"SPY": "fixture"},
    )
    return path, engine, snapshot_id


def test_snapshot_loader_requires_fresh_decided_and_hash_verified_data(tmp_path):
    stale_path, _, stale_id = _snapshot(tmp_path, stale=1)
    with pytest.raises(ValueError, match="zero-staleness"):
        cli.load_immutable_snapshot(str(stale_path), stale_id)

    exception_path, _, exception_id = _snapshot(
        tmp_path,
        status=DataQualityStatus.TRUSTED_WITH_EXCEPTIONS,
    )
    with pytest.raises(ValueError, match="decision_set_hash"):
        cli.load_immutable_snapshot(str(exception_path), exception_id)

    valid_path, engine, valid_id = _snapshot(tmp_path)
    assert "SPY" in cli.load_immutable_snapshot(str(valid_path), valid_id)
    with engine.begin() as connection:
        connection.execute(
            dataset_snapshot_bars.update()
            .where(dataset_snapshot_bars.c.snapshot_id == valid_id)
            .values(open=999.0)
        )
    with pytest.raises(ValueError, match="raw_data_hash"):
        cli.load_immutable_snapshot(str(valid_path), valid_id)


def test_legacy_snapshot_quality_is_not_silently_upgraded_for_admission(tmp_path):
    from storage.schema import dataset_snapshots
    path, engine, snapshot_id = _snapshot(tmp_path)
    with engine.begin() as connection:
        row = connection.execute(dataset_snapshots.select().where(dataset_snapshots.c.id == snapshot_id)).mappings().one()
        old_quality = dict(row["quality_json"])
        old_quality.pop("quality_model_version")
        connection.execute(dataset_snapshots.update().where(dataset_snapshots.c.id == snapshot_id).values(quality_json=old_quality))
    with pytest.raises(ValueError, match="newly audited current-quality"):
        cli.load_immutable_snapshot(str(path), snapshot_id)


@pytest.mark.parametrize("terminal_status", ["rejected", "admitted"])
def test_terminal_admission_is_reused_without_finishing_or_approving(monkeypatch, terminal_status):
    protocol = _protocol()
    engine = create_engine("sqlite://", future=True)
    create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            admission_runs.insert().values(
                strategy_version="SV-TERMINAL",
                methodology=cli.METHODOLOGY,
                status=terminal_status,
                selection_uses_future_holdout=0,
                results_json={
                    "protocol_hash": protocol.content_hash,
                    "gates": {"replacement_sharpe": True, "replacement_drawdown": True},
                    "replacement_comparison": {"baseline_label": cli.fixed_current_baseline_candidate(protocol).label},
                },
            )
        )
    repository = FakeGovernance(engine)
    monkeypatch.setattr(
        cli,
        "NestedExpandingAdmissionRunner",
        _runner(_result(protocol)),
    )

    output = cli.execute_core_admission(
        protocol=protocol,
        strategy_version="SV-TERMINAL",
        data={"SPY": pd.DataFrame()},
        repository=repository,
        evaluator=lambda *args: None,
    )

    assert output["status"] == terminal_status.upper()
    assert output["reused_terminal_run"] is True
    assert "finish_admission" not in [name for name, _ in repository.calls]
    assert "freeze_strategy_version" not in [name for name, _ in repository.calls]
    assert "start_local_sim_clock" not in [name for name, _ in repository.calls]


def test_cli_integrates_with_locked_governance_repository(tmp_path, monkeypatch):
    _, engine, snapshot_id = _snapshot(tmp_path)
    repository = GovernanceRepository(engine=engine)
    repository.create_universe_draft(
        UniverseVersion(
            version="UV-001",
            effective_date="2026-07-17",
            seed_tickers=INITIAL_ETF_UNIVERSE,
            rules=EligibilityRules(),
        )
    )
    repository.approve_universe_version("UV-001", approved_by="test-operator")
    protocol = build_protocol(
        protocol_version="cli-integration-v1",
        code_commit=capture_code_identity()["code_commit"],
        dataset_snapshot_id=snapshot_id,
        universe_version="UV-001",
    )
    monkeypatch.setattr(
        cli,
        "NestedExpandingAdmissionRunner",
        _runner(_result(protocol)),
    )

    output = cli.execute_core_admission(
        protocol=protocol,
        strategy_version="SV-INTEGRATION",
        data={"SPY": pd.DataFrame()},
        repository=repository,
        evaluator=lambda *args: None,
    )

    with engine.connect() as connection:
        strategy = connection.execute(
            strategy_versions.select().where(
                strategy_versions.c.version == "SV-INTEGRATION"
            )
        ).mappings().one()
    assert output["status"] == "ADMITTED"
    assert strategy["status"] == "draft"
    assert strategy["local_sim_start"] is None
    with pytest.raises(ValueError, match="frozen runtime"):
        repository.load_frozen_runtime("SV-INTEGRATION")
    with pytest.raises(ValueError, match="approved_by"):
        repository.freeze_strategy_version("SV-INTEGRATION", admission_run_id=output["admission_run_id"], approved_by="  ")
    repository.freeze_strategy_version("SV-INTEGRATION", admission_run_id=output["admission_run_id"], approved_by="reviewer")
    assert repository.load_frozen_runtime("SV-INTEGRATION").config["strategy_version"] == "SV-INTEGRATION"
    with engine.connect() as connection:
        approved = connection.execute(strategy_versions.select().where(strategy_versions.c.version == "SV-INTEGRATION")).mappings().one()
    assert approved["approved_by"] == "reviewer"
    assert approved["approved_at"] is not None
    assert approved["local_sim_start"] is None
