import json

import numpy as np
import pandas as pd
import pytest

from research.mirror_walk_forward import (
    build_walk_forward_folds,
    evaluate_mirror_walk_forward,
)
from research.model_admission import compare_period


def _portfolio(gross_returns: pd.Series, turnover_scale: float = 1.0) -> pd.DataFrame:
    index = gross_returns.index
    turnover = pd.Series(0.0, index=index)
    month_ends = index.to_series().groupby(index.to_period("M")).tail(1).index
    turnover.loc[month_ends] = turnover_scale
    trading_cost = turnover * 0.0005
    slippage = turnover * 0.0002
    regimes = np.resize(
        ["bull_trend", "neutral", "bear_high_vol", "risk_off"],
        len(index),
    )
    return pd.DataFrame(
        {
            "daily_return": gross_returns - trading_cost - slippage,
            "turnover": turnover,
            "est_trading_cost": trading_cost,
            "est_slippage": slippage,
            "est_cost": trading_cost + slippage,
            "regime": regimes,
        },
        index=index,
    )


def _evaluation_inputs():
    index = pd.bdate_range("2019-01-02", "2024-12-31")
    cycle = np.resize([0.006, -0.005, 0.003, -0.004, 0.004], len(index))
    baseline_gross = pd.Series(cycle, index=index)
    candidate_a_gross = baseline_gross * 0.65 + 0.00035
    neighbor_gross = baseline_gross * 0.68 + 0.00032
    candidate_b_gross = baseline_gross * 1.15 - 0.00010
    holdout = index >= pd.Timestamp("2024-01-01")
    candidate_b_gross.loc[holdout] = baseline_gross.loc[holdout] * 0.2 + 0.002

    baseline = _portfolio(baseline_gross)
    candidates = {
        "stable": _portfolio(candidate_a_gross, 0.8),
        "stable_neighbor": _portfolio(neighbor_gross, 0.85),
        "holdout_star": _portfolio(candidate_b_gross, 0.7),
    }
    parameters = {
        "stable": {"top_n": 3, "target_vol": 0.12},
        "stable_neighbor": {"top_n": 3, "target_vol": 0.13},
        "holdout_star": {"top_n": 8, "target_vol": 0.30},
    }
    return baseline, candidates, parameters


def test_walk_forward_selection_never_uses_final_holdout_for_ranking():
    baseline, candidates, parameters = _evaluation_inputs()

    result = evaluate_mirror_walk_forward(
        baseline=baseline,
        candidates=candidates,
        candidate_parameters=parameters,
        first_validation_start=pd.Timestamp("2022-01-01"),
        holdout_start=pd.Timestamp("2024-01-01"),
        start_dates=[pd.Timestamp("2022-01-01"), pd.Timestamp("2022-07-01")],
        crisis_periods={
            "synthetic_crisis": (
                pd.Timestamp("2020-03-01"),
                pd.Timestamp("2020-06-30"),
            )
        },
        trading_cost_bps=5.0,
        slippage_bps=2.0,
        baseline_trading_cost_bps=5.0,
        baseline_slippage_bps=2.0,
    )

    holdout_star = compare_period(
        baseline,
        candidates["holdout_star"],
        pd.Timestamp("2024-01-01"),
    )
    stable = compare_period(
        baseline,
        candidates["stable"],
        pd.Timestamp("2024-01-01"),
    )

    assert holdout_star["sharpe_improvement"] > stable["sharpe_improvement"]
    assert result["selected_label"] == "stable"
    assert result["selection_uses_holdout"] is False
    assert all(fold["validation_end"] < "2024-01-01" for fold in result["folds"])
    assert result["final_holdout"] == stable
    assert "final_holdout" not in result["candidate_ranking"][0]


def test_momentum_parameter_search_stays_diagnostic_without_independent_signal():
    baseline, candidates, parameters = _evaluation_inputs()

    result = evaluate_mirror_walk_forward(
        baseline=baseline,
        candidates=candidates,
        candidate_parameters=parameters,
        first_validation_start=pd.Timestamp("2022-01-01"),
        holdout_start=pd.Timestamp("2024-01-01"),
        start_dates=[pd.Timestamp("2022-01-01"), pd.Timestamp("2022-07-01")],
        crisis_periods={},
        trading_cost_bps=5.0,
        slippage_bps=2.0,
        baseline_trading_cost_bps=5.0,
        baseline_slippage_bps=2.0,
        independent_signal=False,
    )

    assert result["gates"]["same_interval_same_costs"] is True
    assert result["gates"]["independent_information"] is False
    assert result["gates"]["historical_universe_integrity"] is False
    assert result["admitted"] is False
    assert result["position_changes_authorized"] is False
    assert result["parameter_robustness"]["neighbors"]
    json.dumps(result)


def test_cost_mismatch_is_rejected_before_comparison():
    baseline, candidates, parameters = _evaluation_inputs()

    with pytest.raises(ValueError, match="identical costs"):
        evaluate_mirror_walk_forward(
            baseline=baseline,
            candidates=candidates,
            candidate_parameters=parameters,
            first_validation_start=pd.Timestamp("2022-01-01"),
            holdout_start=pd.Timestamp("2024-01-01"),
            start_dates=[pd.Timestamp("2022-01-01")],
            crisis_periods={},
            trading_cost_bps=6.0,
            slippage_bps=2.0,
            baseline_trading_cost_bps=5.0,
            baseline_slippage_bps=2.0,
        )


def test_fold_builder_requires_training_data_and_keeps_holdout_untouched():
    index = pd.bdate_range("2020-01-02", "2024-12-31")

    folds = build_walk_forward_folds(
        index,
        first_validation_start=pd.Timestamp("2022-01-01"),
        holdout_start=pd.Timestamp("2024-01-01"),
    )

    assert len(folds) == 2
    assert all(fold.validation_end < pd.Timestamp("2024-01-01") for fold in folds)
    assert all(fold.training_end < fold.validation_start for fold in folds)
