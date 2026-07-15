from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, Iterable, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from utils.metrics import max_drawdown, sharpe_ratio


@dataclass(frozen=True)
class AdmissionThresholds:
    minimum_sharpe_improvement: float = 0.05
    minimum_drawdown_reduction: float = 0.10
    minimum_window_win_rate: float = 0.60
    minimum_parameter_pass_rate: float = 0.67
    minimum_start_date_pass_rate: float = 0.67
    maximum_signal_correlation: float = 0.80


@dataclass(frozen=True)
class PerformanceMetrics:
    sharpe: float
    max_drawdown: float
    total_return: float
    total_turnover: float
    total_cost: float
    trading_cost: float
    slippage_cost: float
    observations: int


def performance_metrics(portfolio: pd.DataFrame) -> PerformanceMetrics:
    if portfolio.empty:
        raise ValueError("Cannot calculate metrics for an empty portfolio.")
    returns = portfolio["daily_return"].dropna().astype(float)
    equity = (1.0 + returns).cumprod()
    return PerformanceMetrics(
        sharpe=sharpe_ratio(returns),
        max_drawdown=max_drawdown(equity),
        total_return=float(equity.iloc[-1] - 1.0),
        total_turnover=float(portfolio["turnover"].fillna(0.0).sum()),
        total_cost=float(portfolio["est_cost"].fillna(0.0).sum()),
        trading_cost=float(
            portfolio.get("est_trading_cost", pd.Series(0.0, index=portfolio.index))
            .fillna(0.0)
            .sum()
        ),
        slippage_cost=float(
            portfolio.get("est_slippage", pd.Series(0.0, index=portfolio.index))
            .fillna(0.0)
            .sum()
        ),
        observations=len(returns),
    )


def _aligned_period(
    baseline: pd.DataFrame,
    candidate: pd.DataFrame,
    start: pd.Timestamp,
    end: Optional[pd.Timestamp] = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    baseline_period = baseline.loc[start:end]
    candidate_period = candidate.loc[start:end]
    if baseline_period.empty or candidate_period.empty:
        raise ValueError(f"No comparison data from {start} to {end}.")
    if not baseline_period.index.equals(candidate_period.index):
        raise ValueError("Baseline and candidate must use exactly the same dates.")
    return baseline_period, candidate_period


def compare_period(
    baseline: pd.DataFrame,
    candidate: pd.DataFrame,
    start: pd.Timestamp,
    end: Optional[pd.Timestamp] = None,
) -> Dict[str, object]:
    baseline_period, candidate_period = _aligned_period(
        baseline, candidate, start, end
    )
    baseline_metrics = performance_metrics(baseline_period)
    candidate_metrics = performance_metrics(candidate_period)
    baseline_drawdown = abs(baseline_metrics.max_drawdown)
    drawdown_reduction = (
        (baseline_drawdown - abs(candidate_metrics.max_drawdown))
        / baseline_drawdown
        if baseline_drawdown > 0.0
        else 0.0
    )
    return {
        "start": str(baseline_period.index.min().date()),
        "end": str(baseline_period.index.max().date()),
        "baseline": asdict(baseline_metrics),
        "candidate": asdict(candidate_metrics),
        "sharpe_improvement": candidate_metrics.sharpe - baseline_metrics.sharpe,
        "drawdown_reduction": drawdown_reduction,
        "return_improvement": (
            candidate_metrics.total_return - baseline_metrics.total_return
        ),
    }


def walk_forward_comparisons(
    baseline: pd.DataFrame,
    candidate: pd.DataFrame,
    *,
    first_test_year: int,
    training_years: int = 3,
) -> list[Dict[str, object]]:
    last_year = int(baseline.index.max().year)
    folds = []
    for test_year in range(first_test_year, last_year + 1):
        test_start = pd.Timestamp(test_year, 1, 1)
        test_end = min(
            pd.Timestamp(test_year, 12, 31), pd.Timestamp(baseline.index.max())
        )
        if test_start > test_end:
            continue
        comparison = compare_period(baseline, candidate, test_start, test_end)
        comparison.update(
            {
                "training_start": str(
                    (test_start - pd.DateOffset(years=training_years)).date()
                ),
                "training_end": str((test_start - pd.Timedelta(days=1)).date()),
                "test_year": test_year,
            }
        )
        folds.append(comparison)
    return folds


def _comparison_passes(comparison: Mapping[str, object]) -> bool:
    return bool(
        float(comparison["sharpe_improvement"]) > 0.0
        and float(comparison["drawdown_reduction"]) >= 0.0
    )


def _sensitivity_results(
    baseline: pd.DataFrame,
    candidates: Mapping[str, pd.DataFrame],
    oos_start: pd.Timestamp,
) -> Dict[str, object]:
    comparisons = {
        label: compare_period(baseline, portfolio, oos_start)
        for label, portfolio in candidates.items()
    }
    pass_count = sum(_comparison_passes(item) for item in comparisons.values())
    return {
        "pass_rate": pass_count / len(comparisons) if comparisons else 0.0,
        "comparisons": comparisons,
    }


def _start_date_results(
    baseline: pd.DataFrame,
    candidate: pd.DataFrame,
    start_dates: Sequence[pd.Timestamp],
) -> Dict[str, object]:
    comparisons = {
        str(start.date()): compare_period(baseline, candidate, start)
        for start in start_dates
    }
    pass_count = sum(_comparison_passes(item) for item in comparisons.values())
    return {
        "pass_rate": pass_count / len(comparisons) if comparisons else 0.0,
        "comparisons": comparisons,
    }


def _regime_results(
    baseline: pd.DataFrame,
    candidate: pd.DataFrame,
    oos_start: pd.Timestamp,
) -> Dict[str, object]:
    baseline_oos, candidate_oos = _aligned_period(
        baseline, candidate, oos_start
    )
    regime_groups = {
        "bull": {"bull_trend"},
        "bear": {"bear_high_vol"},
        "sideways": {"neutral"},
        "risk_off": {"risk_off"},
    }
    results: Dict[str, object] = {}
    for label, values in regime_groups.items():
        mask = baseline_oos["regime"].isin(values)
        if not mask.any():
            continue
        base_slice = baseline_oos.loc[mask]
        candidate_slice = candidate_oos.loc[mask]
        results[label] = {
            "baseline": asdict(performance_metrics(base_slice)),
            "candidate": asdict(performance_metrics(candidate_slice)),
        }
    return results


def _crisis_results(
    baseline: pd.DataFrame,
    candidate: pd.DataFrame,
    periods: Mapping[str, tuple[pd.Timestamp, pd.Timestamp]],
) -> Dict[str, object]:
    return {
        label: compare_period(baseline, candidate, start, end)
        for label, (start, end) in periods.items()
    }


def evaluate_admission(
    *,
    baseline: pd.DataFrame,
    candidate: pd.DataFrame,
    parameter_candidates: Mapping[str, pd.DataFrame],
    momentum_signal: pd.Series,
    model_signal: pd.Series,
    oos_start: pd.Timestamp,
    first_test_year: int,
    start_dates: Sequence[pd.Timestamp],
    crisis_periods: Mapping[str, tuple[pd.Timestamp, pd.Timestamp]],
    thresholds: AdmissionThresholds = AdmissionThresholds(),
) -> Dict[str, object]:
    overall = compare_period(baseline, candidate, oos_start)
    folds = walk_forward_comparisons(
        baseline,
        candidate,
        first_test_year=first_test_year,
    )
    window_win_rate = (
        sum(_comparison_passes(fold) for fold in folds) / len(folds)
        if folds
        else 0.0
    )
    parameter_results = _sensitivity_results(
        baseline, parameter_candidates, oos_start
    )
    start_date_results = _start_date_results(
        baseline, candidate, start_dates
    )

    aligned_signals = pd.concat(
        [momentum_signal.rename("momentum"), model_signal.rename("model")], axis=1
    ).dropna()
    signal_correlation = (
        float(aligned_signals.corr().iloc[0, 1])
        if len(aligned_signals) >= 12
        else np.nan
    )

    candidate_metrics = overall["candidate"]
    baseline_metrics = overall["baseline"]
    gates = {
        "oos_sharpe": (
            float(overall["sharpe_improvement"])
            >= thresholds.minimum_sharpe_improvement
        ),
        "maximum_drawdown": (
            float(overall["drawdown_reduction"])
            >= thresholds.minimum_drawdown_reduction
        ),
        "rolling_windows": window_win_rate >= thresholds.minimum_window_win_rate,
        "after_costs": (
            float(candidate_metrics["total_return"])
            > float(baseline_metrics["total_return"])
            and float(candidate_metrics["slippage_cost"]) > 0.0
            and float(candidate_metrics["trading_cost"]) > 0.0
        ),
        "parameter_robustness": (
            float(parameter_results["pass_rate"])
            >= thresholds.minimum_parameter_pass_rate
        ),
        "start_date_robustness": (
            float(start_date_results["pass_rate"])
            >= thresholds.minimum_start_date_pass_rate
        ),
        "independent_information": (
            np.isfinite(signal_correlation)
            and abs(signal_correlation) <= thresholds.maximum_signal_correlation
        ),
    }

    return {
        "admitted": all(gates.values()),
        "thresholds": asdict(thresholds),
        "gates": gates,
        "overall_oos": overall,
        "window_win_rate": window_win_rate,
        "walk_forward_folds": folds,
        "parameter_robustness": parameter_results,
        "start_date_robustness": start_date_results,
        "signal_correlation": signal_correlation,
        "signal_observations": len(aligned_signals),
        "regimes": _regime_results(baseline, candidate, oos_start),
        "crises": _crisis_results(baseline, candidate, crisis_periods),
    }
