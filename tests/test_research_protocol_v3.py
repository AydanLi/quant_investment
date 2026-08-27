from __future__ import annotations

import pandas as pd
import pytest

from research.nested_walk_forward import (
    EvaluationMetrics,
    NestedExpandingAdmissionRunner,
    build_nested_folds,
    historical_admission_gates,
)
from research.core_evaluator import CoreStrategyEvaluator
from research.protocol import build_protocol, core_candidate_grid
from config.settings import Config


def _protocol():
    return build_protocol(
        protocol_version="test-v1",
        code_commit="a" * 40,
        dataset_snapshot_id=1,
        universe_version="UV-001",
    )


def test_core_grid_contains_exactly_135_unique_preregistered_candidates():
    candidates = core_candidate_grid()

    assert len(candidates) == 135
    assert len({candidate.label for candidate in candidates}) == 135
    assert {candidate.top_n for candidate in candidates} == {3, 4, 5}
    assert {candidate.target_annual_vol for candidate in candidates} == {0.08, 0.10, 0.12}


def test_protocol_is_write_once(tmp_path):
    protocol = _protocol()
    target = tmp_path / "protocol.json"

    protocol.write_once(target)
    protocol.write_once(target)
    changed = build_protocol(
        protocol_version="test-v2",
        code_commit="b" * 40,
        dataset_snapshot_id=1,
        universe_version="UV-001",
    )

    with pytest.raises(FileExistsError):
        changed.write_once(target)


def test_nested_runner_never_exposes_validation_or_future_rows_to_training():
    index = pd.bdate_range("2010-01-01", "2018-12-31")
    data = {"prices": pd.DataFrame({"value": range(len(index))}, index=index)}
    observed: list[tuple[pd.Timestamp, pd.Timestamp]] = []

    def evaluator(candidate, training, validation, cost_bps):
        train_end = training["prices"].index.max()
        validation_start = validation["prices"].index.min()
        assert train_end < validation_start
        observed.append((train_end, validation_start))
        return EvaluationMetrics(
            excess_sharpe=float(candidate.top_n) / 10.0,
            net_return=0.1,
            benchmark_return=0.05,
            max_drawdown=-0.1,
        )

    result = NestedExpandingAdmissionRunner(_protocol(), evaluator).run(data)

    assert observed
    assert result["selection_uses_future_holdout"] is False
    assert len(result["trials"]) == 135 * len(result["outer_folds"])
    assert all(item["selected_label"] for item in result["outer_folds"])
    first_final = result["final_selection_trials"][0]["folds"][0]["fold"]
    assert pd.Timestamp(first_final["validation_start"]) >= pd.Timestamp(
        first_final["training_start"]
    ) + pd.DateOffset(years=5)
    for item in result["robustness"]["start_date_results"]:
        for evaluation in item["evaluations"]:
            assert pd.Timestamp(evaluation["validation_start"]) >= pd.Timestamp(
                evaluation["training_start"]
            ) + pd.DateOffset(years=5)


def test_outer_folds_use_five_year_training_and_twelve_month_tests():
    index = pd.bdate_range("2010-01-01", "2017-12-31")
    folds = build_nested_folds(index)

    first = folds[0].outer
    assert first.training_end < first.validation_start
    assert first.validation_start >= pd.Timestamp("2015-01-01")
    assert first.validation_end < first.validation_start + pd.DateOffset(months=12)


def test_historical_gates_include_stress_robustness_and_replacement_hurdles():
    def metrics(net_return, benchmark_return, excess_sharpe):
        return {
            "excess_sharpe": excess_sharpe,
            "net_return": net_return,
            "benchmark_return": benchmark_return,
            "stop_count": 0,
            "maximum_stop_overshoot": 0.01,
            "degenerate_all_cash": False,
            "evaluation_status": "evaluated",
        }

    outer = [
        {
            "cost_scenarios": {
                "7.0": metrics(0.10, 0.05, 0.2),
                "20.0": metrics(0.08, 0.05, 0.1),
            }
        }
        for _ in range(3)
    ]

    gates = historical_admission_gates(
        outer,
        _protocol(),
        aggregate_net_return=0.5,
        aggregate_bil_return=0.2,
        neighbor_pass_rate=0.7,
        start_date_pass_rate=0.7,
        elapsed_years=10.0,
        replacement_excess_sharpe_improvement=0.05,
        replacement_drawdown_improvement=0.10,
    )

    assert all(gates.values())


def test_historical_gates_use_return_windows_and_aggregate_twenty_bp_excess():
    def metrics(net_return, benchmark_return, excess_sharpe):
        return {
            "excess_sharpe": excess_sharpe,
            "net_return": net_return,
            "benchmark_return": benchmark_return,
            "stop_count": 0,
            "maximum_stop_overshoot": 0.0,
            "degenerate_all_cash": False,
            "evaluation_status": "evaluated",
        }

    outer = [
        {
            "cost_scenarios": {
                "7.0": metrics(0.20, 0.10, 0.2),
                "20.0": metrics(0.50, 0.10, 0.2),
            }
        },
        {
            "cost_scenarios": {
                "7.0": metrics(0.12, 0.10, -0.1),
                "20.0": metrics(-0.10, 0.10, -0.1),
            }
        },
        {
            "cost_scenarios": {
                "7.0": metrics(0.05, 0.10, 0.2),
                "20.0": metrics(0.05, 0.10, 0.2),
            }
        },
    ]
    gates = historical_admission_gates(
        outer,
        _protocol(),
        aggregate_net_return=0.4,
        aggregate_bil_return=0.2,
        neighbor_pass_rate=0.7,
        start_date_pass_rate=0.7,
        elapsed_years=3.0,
        replacement_excess_sharpe_improvement=0.05,
        replacement_drawdown_improvement=0.10,
    )

    assert gates["positive_outer_windows"] is True
    assert gates["stress_cost_positive"] is True
    outer[0]["cost_scenarios"]["7.0"]["degenerate_all_cash"] = True
    assert historical_admission_gates(
        outer,
        _protocol(),
        aggregate_net_return=0.4,
        aggregate_bil_return=0.2,
        neighbor_pass_rate=0.7,
        start_date_pass_rate=0.7,
        elapsed_years=3.0,
        replacement_excess_sharpe_improvement=0.05,
        replacement_drawdown_improvement=0.10,
    )["non_degenerate"] is False


def _ohlc(index, value):
    return pd.DataFrame(
        {"Open": value, "Close": value, "Volume": 1_000_000.0},
        index=index,
    )


def test_core_evaluator_blocks_missing_bil_benchmark(monkeypatch):
    index = pd.bdate_range("2010-01-01", periods=400)
    training_index = index[:300]
    validation_index = index[300:]

    class FakeBacktester:
        def __init__(self, **kwargs):
            pass

        def run(self):
            portfolio = pd.DataFrame(
                {
                    "daily_return": 0.0,
                    "drawdown": 0.0,
                    "stop_triggered": False,
                    "w_SPY": 0.0,
                },
                index=validation_index,
            )
            return {"portfolio": portfolio}

    monkeypatch.setattr("research.core_evaluator.Backtester", FakeBacktester)

    with pytest.raises(ValueError, match="BIL benchmark"):
        CoreStrategyEvaluator(Config(universe=["SPY", "BIL"]))(
            core_candidate_grid()[0],
            {"SPY": _ohlc(training_index, 100.0)},
            {"SPY": _ohlc(validation_index, 101.0)},
            7.0,
        )


def test_core_evaluator_marks_all_cash_validation_as_degenerate(monkeypatch):
    index = pd.bdate_range("2010-01-01", periods=400)
    training_index = index[:300]
    validation_index = index[300:]

    class FakeBacktester:
        def __init__(self, **kwargs):
            pass

        def run(self):
            portfolio = pd.DataFrame(
                {
                    "daily_return": 0.0001,
                    "drawdown": 0.0,
                    "stop_triggered": False,
                    "w_SPY": 0.0,
                    "w_BIL": 1.0,
                },
                index=validation_index,
            )
            return {"portfolio": portfolio}

    monkeypatch.setattr("research.core_evaluator.Backtester", FakeBacktester)
    training = {
        "SPY": _ohlc(training_index, 100.0),
        "BIL": _ohlc(training_index, 90.0),
    }
    validation = {
        "SPY": _ohlc(validation_index, 101.0),
        "BIL": _ohlc(validation_index, 91.0),
    }

    metrics = CoreStrategyEvaluator(Config(universe=["SPY", "BIL"]))(
        core_candidate_grid()[0],
        training,
        validation,
        7.0,
    )

    assert metrics.degenerate_all_cash is True
    assert metrics.selection_score == float("-inf")


def test_nested_runner_reuses_persisted_stage_fold_trial_without_recomputing():
    protocol = _protocol()
    candidate = protocol.candidates[0]
    cached = {
        ("inner_selection", "outer-001/inner-001", candidate.label): {
            "status": "evaluated",
            "metrics": {
                "excess_sharpe": 0.2,
                "net_return": 0.1,
                "benchmark_return": 0.05,
                "max_drawdown": -0.1,
                "stop_count": 0,
                "maximum_stop_overshoot": 0.0,
                "degenerate_all_cash": False,
            },
        }
    }

    def should_not_run(*args):
        raise AssertionError("cached trial was recomputed")

    runner = NestedExpandingAdmissionRunner(
        protocol,
        should_not_run,
        trial_cache=cached,
    )
    frame = pd.DataFrame({"value": [1.0]}, index=[pd.Timestamp("2020-01-02")])

    record = runner._evaluate(
        stage="inner_selection",
        fold_key="outer-001/inner-001",
        candidate=candidate,
        training={"SPY": frame},
        validation={"SPY": frame},
        cost_bps=7.0,
    )

    assert record["metrics"]["excess_sharpe"] == 0.2
