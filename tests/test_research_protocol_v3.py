from __future__ import annotations

import pandas as pd
import pytest

from research.nested_walk_forward import (
    EvaluationMetrics,
    NestedExpandingAdmissionRunner,
    build_nested_folds,
    historical_admission_gates,
)
from research.protocol import build_protocol, core_candidate_grid


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


def test_outer_folds_use_five_year_training_and_twelve_month_tests():
    index = pd.bdate_range("2010-01-01", "2017-12-31")
    folds = build_nested_folds(index)

    first = folds[0].outer
    assert first.training_end < first.validation_start
    assert first.validation_start >= pd.Timestamp("2015-01-01")
    assert first.validation_end < first.validation_start + pd.DateOffset(months=12)


def test_historical_gates_include_stress_robustness_and_replacement_hurdles():
    outer = [
        {
            "cost_scenarios": {
                "7.0": {"excess_sharpe": 0.2, "stop_count": 0, "maximum_stop_overshoot": 0.01},
                "20.0": {"excess_sharpe": 0.1, "stop_count": 0, "maximum_stop_overshoot": 0.01},
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
