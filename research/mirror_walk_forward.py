from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from research.model_admission import (
    AdmissionThresholds,
    compare_period,
    performance_metrics,
)


@dataclass(frozen=True)
class WalkForwardFold:
    training_start: pd.Timestamp
    training_end: pd.Timestamp
    validation_start: pd.Timestamp
    validation_end: pd.Timestamp


def build_walk_forward_folds(
    index: pd.DatetimeIndex,
    *,
    first_validation_start: pd.Timestamp,
    holdout_start: pd.Timestamp,
    fold_months: int = 12,
    minimum_training_observations: int = 252,
) -> list[WalkForwardFold]:
    """Build expanding-training folds that end before the final holdout."""
    dates = pd.DatetimeIndex(index).sort_values().unique()
    first_validation_start = pd.Timestamp(first_validation_start)
    holdout_start = pd.Timestamp(holdout_start)
    if dates.empty:
        raise ValueError("Cannot build walk-forward folds without dates.")
    if first_validation_start >= holdout_start:
        raise ValueError("first_validation_start must be before holdout_start.")
    if fold_months < 1:
        raise ValueError("fold_months must be positive.")

    folds: list[WalkForwardFold] = []
    fold_start = first_validation_start
    while fold_start < holdout_start:
        fold_end_exclusive = min(
            fold_start + pd.DateOffset(months=fold_months),
            holdout_start,
        )
        training_dates = dates[dates < fold_start]
        validation_dates = dates[
            (dates >= fold_start) & (dates < fold_end_exclusive)
        ]
        if len(training_dates) >= minimum_training_observations and len(
            validation_dates
        ):
            folds.append(
                WalkForwardFold(
                    training_start=pd.Timestamp(training_dates.min()),
                    training_end=pd.Timestamp(training_dates.max()),
                    validation_start=pd.Timestamp(validation_dates.min()),
                    validation_end=pd.Timestamp(validation_dates.max()),
                )
            )
        fold_start = fold_end_exclusive

    if not folds:
        raise ValueError(
            "No walk-forward folds have enough training and validation data."
        )
    return folds


def _comparison_passes(comparison: Mapping[str, object]) -> bool:
    baseline = comparison["baseline"]
    candidate = comparison["candidate"]
    return bool(
        float(comparison["sharpe_improvement"]) > 0.0
        and float(comparison["drawdown_reduction"]) >= 0.0
        and float(candidate["total_return"]) > float(baseline["total_return"])
    )


def _comparison_score(comparison: Mapping[str, object]) -> float:
    values = np.asarray(
        [
            comparison["sharpe_improvement"],
            comparison["drawdown_reduction"],
            comparison["return_improvement"],
        ],
        dtype=float,
    )
    if not np.isfinite(values).all():
        return float("-inf")
    return float(values[0] + 0.25 * values[1] + 0.10 * values[2])


def _candidate_validation_summary(
    baseline: pd.DataFrame,
    candidate: pd.DataFrame,
    folds: Sequence[WalkForwardFold],
) -> dict[str, object]:
    comparisons = [
        compare_period(
            baseline,
            candidate,
            fold.validation_start,
            fold.validation_end,
        )
        for fold in folds
    ]
    scores = [_comparison_score(comparison) for comparison in comparisons]
    pass_rate = sum(_comparison_passes(item) for item in comparisons) / len(
        comparisons
    )
    return {
        "selection_score": float(np.mean(scores)),
        "window_win_rate": pass_rate,
        "mean_sharpe_improvement": float(
            np.mean([item["sharpe_improvement"] for item in comparisons])
        ),
        "median_drawdown_reduction": float(
            np.median([item["drawdown_reduction"] for item in comparisons])
        ),
        "mean_return_improvement": float(
            np.mean([item["return_improvement"] for item in comparisons])
        ),
        "comparisons": comparisons,
    }


def _parameter_neighbors(
    selected_label: str,
    candidate_parameters: Mapping[str, Mapping[str, object]],
) -> list[str]:
    selected = candidate_parameters[selected_label]
    neighbors = []
    for label, parameters in candidate_parameters.items():
        if label == selected_label or parameters.keys() != selected.keys():
            continue
        differences = sum(parameters[key] != selected[key] for key in selected)
        if differences == 1:
            neighbors.append(label)
    return sorted(neighbors)


def _regime_results(
    baseline: pd.DataFrame,
    candidate: pd.DataFrame,
    start: pd.Timestamp,
) -> dict[str, object]:
    baseline_period = baseline.loc[start:]
    candidate_period = candidate.loc[start:]
    groups = {
        "bull": {"bull_trend"},
        "bear": {"bear_high_vol"},
        "sideways": {"neutral"},
        "risk_off": {"risk_off"},
    }
    results = {}
    for label, regimes in groups.items():
        mask = baseline_period["regime"].isin(regimes)
        if not mask.any():
            continue
        results[label] = {
            "baseline": asdict(performance_metrics(baseline_period.loc[mask])),
            "candidate": asdict(performance_metrics(candidate_period.loc[mask])),
        }
    return results


def _crisis_results(
    baseline: pd.DataFrame,
    candidate: pd.DataFrame,
    crisis_periods: Mapping[str, tuple[pd.Timestamp, pd.Timestamp]],
) -> dict[str, object]:
    results = {}
    minimum = pd.Timestamp(baseline.index.min())
    maximum = pd.Timestamp(baseline.index.max())
    for label, (start, end) in crisis_periods.items():
        actual_start = max(pd.Timestamp(start), minimum)
        actual_end = min(pd.Timestamp(end), maximum)
        if actual_start <= actual_end and len(baseline.loc[actual_start:actual_end]):
            results[label] = compare_period(
                baseline,
                candidate,
                actual_start,
                actual_end,
            )
    return results


def evaluate_mirror_walk_forward(
    *,
    baseline: pd.DataFrame,
    candidates: Mapping[str, pd.DataFrame],
    candidate_parameters: Mapping[str, Mapping[str, object]],
    first_validation_start: pd.Timestamp,
    holdout_start: pd.Timestamp,
    start_dates: Sequence[pd.Timestamp],
    crisis_periods: Mapping[str, tuple[pd.Timestamp, pd.Timestamp]],
    trading_cost_bps: float,
    slippage_bps: float,
    baseline_trading_cost_bps: float,
    baseline_slippage_bps: float,
    independent_signal: bool = False,
    historical_universe_integrity: bool = False,
    fold_months: int = 12,
    minimum_training_observations: int = 252,
    thresholds: AdmissionThresholds = AdmissionThresholds(),
) -> dict[str, object]:
    """Select on pre-holdout folds, then evaluate one frozen final candidate."""
    if not candidates:
        raise ValueError("At least one candidate is required.")
    if set(candidates) != set(candidate_parameters):
        raise ValueError("Every candidate must have a parameter definition.")
    if (
        trading_cost_bps != baseline_trading_cost_bps
        or slippage_bps != baseline_slippage_bps
    ):
        raise ValueError("Baseline and candidates must use identical costs.")
    if not all(baseline.index.equals(item.index) for item in candidates.values()):
        raise ValueError("Baseline and candidates must use exactly the same dates.")

    holdout_start = pd.Timestamp(holdout_start)
    folds = build_walk_forward_folds(
        baseline.index,
        first_validation_start=pd.Timestamp(first_validation_start),
        holdout_start=holdout_start,
        fold_months=fold_months,
        minimum_training_observations=minimum_training_observations,
    )
    if baseline.loc[holdout_start:].empty:
        raise ValueError("The final holdout period is empty.")

    fold_audit = []
    for fold in folds:
        training_comparisons = {
            label: compare_period(
                baseline,
                candidate,
                fold.training_start,
                fold.training_end,
            )
            for label, candidate in candidates.items()
        }
        training_winner = max(
            training_comparisons,
            key=lambda label: _comparison_score(training_comparisons[label]),
        )
        fold_audit.append(
            {
                "training_start": str(fold.training_start.date()),
                "training_end": str(fold.training_end.date()),
                "validation_start": str(fold.validation_start.date()),
                "validation_end": str(fold.validation_end.date()),
                "training_winner": training_winner,
                "winner_validation": compare_period(
                    baseline,
                    candidates[training_winner],
                    fold.validation_start,
                    fold.validation_end,
                ),
            }
        )

    summaries = {
        label: _candidate_validation_summary(baseline, candidate, folds)
        for label, candidate in candidates.items()
    }
    selected_label = max(
        summaries,
        key=lambda label: float(summaries[label]["selection_score"]),
    )
    selected = candidates[selected_label]
    selected_summary = summaries[selected_label]

    holdout = compare_period(baseline, selected, holdout_start)
    pre_holdout_end = folds[-1].validation_end
    start_comparisons = {
        str(pd.Timestamp(start).date()): compare_period(
            baseline,
            selected,
            pd.Timestamp(start),
            pre_holdout_end,
        )
        for start in start_dates
        if pd.Timestamp(start) <= pre_holdout_end
    }
    start_pass_rate = (
        sum(_comparison_passes(item) for item in start_comparisons.values())
        / len(start_comparisons)
        if start_comparisons
        else 0.0
    )

    neighbors = _parameter_neighbors(selected_label, candidate_parameters)
    neighbor_results = {
        label: {
            key: value
            for key, value in summaries[label].items()
            if key != "comparisons"
        }
        for label in neighbors
    }
    parameter_pass_rate = (
        sum(
            float(result["window_win_rate"]) >= 0.50
            and float(result["mean_sharpe_improvement"]) > 0.0
            and float(result["median_drawdown_reduction"]) >= 0.0
            for result in neighbor_results.values()
        )
        / len(neighbor_results)
        if neighbor_results
        else 0.0
    )

    candidate_holdout = holdout["candidate"]
    baseline_holdout = holdout["baseline"]
    gates = {
        "same_interval_same_costs": True,
        "holdout_sharpe": (
            float(holdout["sharpe_improvement"])
            >= thresholds.minimum_sharpe_improvement
        ),
        "maximum_drawdown": (
            float(holdout["drawdown_reduction"])
            >= thresholds.minimum_drawdown_reduction
        ),
        "rolling_windows": (
            float(selected_summary["window_win_rate"])
            >= thresholds.minimum_window_win_rate
        ),
        "after_costs": (
            float(candidate_holdout["total_return"])
            > float(baseline_holdout["total_return"])
            and float(candidate_holdout["trading_cost"]) > 0.0
            and float(candidate_holdout["slippage_cost"]) > 0.0
        ),
        "parameter_robustness": (
            parameter_pass_rate >= thresholds.minimum_parameter_pass_rate
        ),
        "start_date_robustness": (
            start_pass_rate >= thresholds.minimum_start_date_pass_rate
        ),
        "independent_information": bool(independent_signal),
        "historical_universe_integrity": bool(historical_universe_integrity),
    }
    admitted = all(gates.values())

    ranking = []
    for label, summary in sorted(
        summaries.items(),
        key=lambda item: float(item[1]["selection_score"]),
        reverse=True,
    ):
        ranking.append(
            {
                "label": label,
                "parameters": dict(candidate_parameters[label]),
                **{key: value for key, value in summary.items() if key != "comparisons"},
            }
        )

    return {
        "methodology": "expanding_walk_forward_with_untouched_holdout",
        "selected_label": selected_label,
        "selected_parameters": dict(candidate_parameters[selected_label]),
        "selection_uses_holdout": False,
        "folds": fold_audit,
        "selected_validation_folds": selected_summary["comparisons"],
        "window_win_rate": selected_summary["window_win_rate"],
        "candidate_ranking": ranking,
        "final_holdout": holdout,
        "parameter_robustness": {
            "neighbors": neighbor_results,
            "pass_rate": parameter_pass_rate,
        },
        "start_date_robustness": {
            "comparisons": start_comparisons,
            "pass_rate": start_pass_rate,
        },
        "regimes": _regime_results(baseline, selected, holdout_start),
        "crises": _crisis_results(baseline, selected, crisis_periods),
        "cost_assumptions": {
            "trading_cost_bps": trading_cost_bps,
            "slippage_bps": slippage_bps,
        },
        "thresholds": asdict(thresholds),
        "gates": gates,
        "admitted": admitted,
        "position_changes_authorized": admitted,
        "independent_signal": bool(independent_signal),
        "historical_universe_integrity": bool(historical_universe_integrity),
    }
