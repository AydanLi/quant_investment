from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable, Mapping

import numpy as np
import pandas as pd

from research.protocol import (
    CandidateParameters,
    ResearchProtocol,
    parameter_neighbors,
)


@dataclass(frozen=True)
class ExpandingFold:
    training_start: pd.Timestamp
    training_end: pd.Timestamp
    validation_start: pd.Timestamp
    validation_end: pd.Timestamp


@dataclass(frozen=True)
class NestedOuterFold:
    outer: ExpandingFold
    inner: tuple[ExpandingFold, ...]


@dataclass(frozen=True)
class EvaluationMetrics:
    excess_sharpe: float
    net_return: float
    benchmark_return: float
    max_drawdown: float
    stop_count: int = 0
    maximum_stop_overshoot: float = 0.0

    @property
    def selection_score(self) -> float:
        if not np.isfinite(self.excess_sharpe):
            return float("-inf")
        return float(self.excess_sharpe)


Evaluator = Callable[
    [CandidateParameters, Mapping[str, pd.DataFrame], Mapping[str, pd.DataFrame], float],
    EvaluationMetrics,
]


def _evaluation_record(
    evaluator: Evaluator,
    candidate: CandidateParameters,
    training: Mapping[str, pd.DataFrame],
    validation: Mapping[str, pd.DataFrame],
    cost_bps: float,
) -> dict[str, object]:
    try:
        metric = evaluator(candidate, training, validation, cost_bps)
        return {"status": "evaluated", "metrics": asdict(metric)}
    except Exception as exc:  # individual trial failure must remain auditable
        failed = EvaluationMetrics(
            excess_sharpe=float("-inf"),
            net_return=float("-inf"),
            benchmark_return=float("nan"),
            max_drawdown=float("nan"),
        )
        return {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "metrics": asdict(failed),
        }


def _first_on_or_after(index: pd.DatetimeIndex, value: pd.Timestamp) -> pd.Timestamp | None:
    matches = index[index >= value]
    return pd.Timestamp(matches[0]) if len(matches) else None


def _last_before(index: pd.DatetimeIndex, value: pd.Timestamp) -> pd.Timestamp | None:
    matches = index[index < value]
    return pd.Timestamp(matches[-1]) if len(matches) else None


def build_nested_folds(
    index: pd.DatetimeIndex,
    *,
    outer_test_months: int = 12,
    minimum_outer_training_years: int = 5,
    minimum_inner_training_years: int = 3,
) -> tuple[NestedOuterFold, ...]:
    dates = pd.DatetimeIndex(index).dropna().sort_values().unique()
    if dates.empty:
        raise ValueError("Cannot build folds without sessions.")
    first_outer = _first_on_or_after(
        dates, pd.Timestamp(dates[0]) + pd.DateOffset(years=minimum_outer_training_years)
    )
    if first_outer is None:
        raise ValueError("At least five years of training history are required.")

    result: list[NestedOuterFold] = []
    outer_start = first_outer
    while outer_start <= dates[-1]:
        outer_end_exclusive = outer_start + pd.DateOffset(months=outer_test_months)
        if dates[-1] < outer_end_exclusive - pd.Timedelta(days=7):
            break
        outer_end = _last_before(dates, outer_end_exclusive)
        training_end = _last_before(dates, outer_start)
        if outer_end is None or training_end is None or outer_end < outer_start:
            break

        inner: list[ExpandingFold] = []
        inner_start = _first_on_or_after(
            dates, pd.Timestamp(dates[0]) + pd.DateOffset(years=minimum_inner_training_years)
        )
        while inner_start is not None and inner_start <= training_end:
            inner_end_exclusive = min(
                inner_start + pd.DateOffset(months=12), outer_start
            )
            inner_end = _last_before(dates, inner_end_exclusive)
            inner_training_end = _last_before(dates, inner_start)
            if (
                inner_end is not None
                and inner_training_end is not None
                and inner_end >= inner_start
            ):
                inner.append(
                    ExpandingFold(
                        training_start=pd.Timestamp(dates[0]),
                        training_end=inner_training_end,
                        validation_start=inner_start,
                        validation_end=inner_end,
                    )
                )
            if inner_end_exclusive >= outer_start:
                break
            inner_start = _first_on_or_after(dates, inner_end_exclusive)

        if not inner:
            raise ValueError("Every outer fold requires at least one inner selection fold.")
        result.append(
            NestedOuterFold(
                outer=ExpandingFold(
                    training_start=pd.Timestamp(dates[0]),
                    training_end=training_end,
                    validation_start=outer_start,
                    validation_end=outer_end,
                ),
                inner=tuple(inner),
            )
        )
        next_start = _first_on_or_after(dates, outer_end_exclusive)
        if next_start is None or next_start <= outer_start:
            break
        outer_start = next_start
    if not result:
        raise ValueError("No complete outer evaluation window was available.")
    return tuple(result)


def _slice_data(
    data: Mapping[str, pd.DataFrame], start: pd.Timestamp, end: pd.Timestamp
) -> dict[str, pd.DataFrame]:
    return {name: frame.loc[start:end].copy() for name, frame in data.items()}


class NestedExpandingAdmissionRunner:
    """Runs selection without exposing outer/future rows to the evaluator."""

    def __init__(
        self,
        protocol: ResearchProtocol,
        evaluator: Evaluator,
        *,
        calendar_key: str = "SPY",
    ) -> None:
        self.protocol = protocol
        self.evaluator = evaluator
        self.calendar_key = calendar_key

    def run(self, data: Mapping[str, pd.DataFrame]) -> dict[str, object]:
        if not data:
            raise ValueError("Research data cannot be empty.")
        calendar_frame = (
            data[self.calendar_key]
            if self.calendar_key in data
            else next(iter(data.values()))
        )
        shared_index = pd.DatetimeIndex(calendar_frame.index)
        folds = build_nested_folds(
            shared_index,
            outer_test_months=self.protocol.outer_test_months,
            minimum_outer_training_years=self.protocol.minimum_outer_training_years,
        )

        outer_results: list[dict[str, object]] = []
        all_trials: list[dict[str, object]] = []
        base_cost = 7.0
        for outer_number, nested in enumerate(folds, start=1):
            candidate_scores: dict[str, list[float]] = {
                candidate.label: [] for candidate in self.protocol.candidates
            }
            for candidate in self.protocol.candidates:
                trial_folds: list[dict[str, object]] = []
                for inner in nested.inner:
                    training = _slice_data(
                        data, inner.training_start, inner.training_end
                    )
                    validation = _slice_data(
                        data, inner.validation_start, inner.validation_end
                    )
                    record = _evaluation_record(
                        self.evaluator, candidate, training, validation, base_cost
                    )
                    candidate_scores[candidate.label].append(
                        float(record["metrics"]["excess_sharpe"])
                    )
                    trial_folds.append({"fold": asdict(inner), **record})
                all_trials.append(
                    {
                        "label": candidate.label,
                        "outer_fold": outer_number,
                        "parameters": candidate.to_dict(),
                        "folds": trial_folds,
                        "status": (
                            "failed"
                            if any(item["status"] == "failed" for item in trial_folds)
                            else "evaluated"
                        ),
                        "score": float(np.mean(candidate_scores[candidate.label])),
                    }
                )

            viable = [
                candidate
                for candidate in self.protocol.candidates
                if np.isfinite(np.mean(candidate_scores[candidate.label]))
            ]
            if not viable:
                raise ValueError(f"All candidates failed in outer fold {outer_number}.")
            selected = max(
                viable,
                key=lambda candidate: (
                    float(np.mean(candidate_scores[candidate.label])),
                    candidate.label,
                ),
            )
            outer_training = _slice_data(
                data, nested.outer.training_start, nested.outer.training_end
            )
            outer_test = _slice_data(
                data, nested.outer.validation_start, nested.outer.validation_end
            )
            scenarios = {}
            for cost in self.protocol.cost_scenarios_bps:
                record = _evaluation_record(
                    self.evaluator, selected, outer_training, outer_test, cost
                )
                scenarios[str(cost)] = {
                    **record["metrics"],
                    "evaluation_status": record["status"],
                    **({"error": record["error"]} if "error" in record else {}),
                }
            outer_results.append(
                {
                    "outer_fold": outer_number,
                    "fold": asdict(nested.outer),
                    "selected_label": selected.label,
                    "cost_scenarios": scenarios,
                }
            )

        # Freeze-date selection repeats the same expanding annual rule using
        # every completed historical year. Future paper rows are not part of
        # ``data`` and therefore cannot affect this decision.
        dates = shared_index.sort_values().unique()
        selection_start = _first_on_or_after(
            dates, pd.Timestamp(dates[0]) + pd.DateOffset(years=3)
        )
        final_folds: list[ExpandingFold] = []
        while selection_start is not None:
            end_exclusive = selection_start + pd.DateOffset(months=12)
            if dates[-1] < end_exclusive - pd.Timedelta(days=7):
                break
            validation_end = _last_before(dates, end_exclusive)
            training_end = _last_before(dates, selection_start)
            if validation_end is None or training_end is None:
                break
            final_folds.append(
                ExpandingFold(
                    training_start=pd.Timestamp(dates[0]),
                    training_end=training_end,
                    validation_start=selection_start,
                    validation_end=validation_end,
                )
            )
            selection_start = _first_on_or_after(dates, end_exclusive)
        if not final_folds:
            raise ValueError("Final freeze-date selection requires completed annual folds.")

        final_trials: list[dict[str, object]] = []
        for candidate in self.protocol.candidates:
            evaluations = []
            for fold in final_folds:
                record = _evaluation_record(
                    self.evaluator,
                    candidate,
                    _slice_data(data, fold.training_start, fold.training_end),
                    _slice_data(data, fold.validation_start, fold.validation_end),
                    base_cost,
                )
                evaluations.append({"fold": asdict(fold), **record})
            score = float(
                np.mean(
                    [item["metrics"]["excess_sharpe"] for item in evaluations]
                )
            )
            final_trials.append(
                {
                    "label": candidate.label,
                    "parameters": candidate.to_dict(),
                    "folds": evaluations,
                    "status": (
                        "failed"
                        if any(item["status"] == "failed" for item in evaluations)
                        else "evaluated"
                    ),
                    "score": score,
                }
            )
        viable_final = [item for item in final_trials if np.isfinite(float(item["score"]))]
        if not viable_final:
            raise ValueError("Every candidate failed freeze-date selection.")
        final_selected = max(
            viable_final, key=lambda item: (float(item["score"]), str(item["label"]))
        )
        selected_candidate = next(
            item
            for item in self.protocol.candidates
            if item.label == final_selected["label"]
        )
        trial_by_label = {str(item["label"]): item for item in final_trials}
        neighbors = parameter_neighbors(selected_candidate, self.protocol.candidates)
        neighbor_scores = [
            float(trial_by_label[item.label]["score"])
            for item in neighbors
            if item.label in trial_by_label
        ]
        neighbor_pass_rate = (
            float(np.mean(np.asarray(neighbor_scores) > 0.0))
            if neighbor_scores
            else 0.0
        )

        start_date_results: list[dict[str, object]] = []
        for offset in self.protocol.start_date_offsets_months:
            shifted_start = pd.Timestamp(dates[0]) + pd.DateOffset(months=offset)
            shifted_dates = dates[dates >= shifted_start]
            if len(shifted_dates) == 0:
                continue
            validation_start = _first_on_or_after(
                shifted_dates,
                pd.Timestamp(shifted_dates[0]) + pd.DateOffset(years=3),
            )
            scores: list[float] = []
            evaluations: list[dict[str, object]] = []
            while validation_start is not None:
                end_exclusive = validation_start + pd.DateOffset(months=12)
                if shifted_dates[-1] < end_exclusive - pd.Timedelta(days=7):
                    break
                training_end = _last_before(shifted_dates, validation_start)
                validation_end = _last_before(shifted_dates, end_exclusive)
                if training_end is None or validation_end is None:
                    break
                record = _evaluation_record(
                    self.evaluator,
                    selected_candidate,
                    _slice_data(data, shifted_dates[0], training_end),
                    _slice_data(data, validation_start, validation_end),
                    base_cost,
                )
                score = float(record["metrics"]["excess_sharpe"])
                scores.append(score)
                evaluations.append(
                    {
                        "training_start": shifted_dates[0],
                        "training_end": training_end,
                        "validation_start": validation_start,
                        "validation_end": validation_end,
                        **record,
                    }
                )
                validation_start = _first_on_or_after(shifted_dates, end_exclusive)
            start_date_results.append(
                {
                    "offset_months": offset,
                    "passed": bool(scores and np.median(scores) > 0.0),
                    "evaluations": evaluations,
                }
            )
        start_date_pass_rate = (
            float(np.mean([item["passed"] for item in start_date_results]))
            if start_date_results
            else 0.0
        )

        return {
            "protocol_hash": self.protocol.content_hash,
            "selection_uses_future_holdout": False,
            "outer_folds": outer_results,
            "trials": all_trials,
            "final_selection_trials": final_trials,
            "final_selected_label": final_selected["label"],
            "robustness": {
                "neighbor_pass_rate": neighbor_pass_rate,
                "start_date_pass_rate": start_date_pass_rate,
                "start_date_results": start_date_results,
            },
        }


def historical_admission_gates(
    outer_results: list[dict[str, object]],
    protocol: ResearchProtocol,
    *,
    aggregate_net_return: float,
    aggregate_bil_return: float,
    neighbor_pass_rate: float,
    start_date_pass_rate: float,
    elapsed_years: float,
    replacement_excess_sharpe_improvement: float | None = None,
    replacement_drawdown_improvement: float | None = None,
) -> dict[str, bool]:
    base_metrics = [item["cost_scenarios"]["7.0"] for item in outer_results]
    stress_metrics = [item["cost_scenarios"]["20.0"] for item in outer_results]
    excess = [float(item["excess_sharpe"]) for item in base_metrics]
    stop_count = sum(int(item["stop_count"]) for item in base_metrics)
    max_overshoot = max(
        (float(item["maximum_stop_overshoot"]) for item in base_metrics),
        default=float("inf"),
    )
    thresholds = protocol.thresholds
    gates = {
        "median_excess_sharpe": bool(excess and np.median(excess) > thresholds.minimum_median_excess_sharpe),
        "aggregate_above_bil": aggregate_net_return > aggregate_bil_return,
        "positive_outer_windows": bool(excess and np.mean(np.asarray(excess) > 0.0) >= thresholds.minimum_positive_outer_window_rate),
        "stress_cost_positive": bool(stress_metrics and all(float(item["excess_sharpe"]) > 0.0 for item in stress_metrics)),
        "neighbor_robustness": neighbor_pass_rate >= thresholds.minimum_neighbor_pass_rate,
        "start_date_robustness": start_date_pass_rate >= thresholds.minimum_start_date_pass_rate,
        "stop_overshoot": max_overshoot <= thresholds.maximum_stop_overshoot,
        "stop_frequency": stop_count <= (elapsed_years / 5.0) * thresholds.maximum_stops_per_five_years,
    }
    if replacement_excess_sharpe_improvement is not None:
        gates["replacement_sharpe"] = (
            replacement_excess_sharpe_improvement
            >= thresholds.replacement_excess_sharpe_improvement
        )
    if replacement_drawdown_improvement is not None:
        gates["replacement_drawdown"] = (
            replacement_drawdown_improvement
            >= thresholds.replacement_drawdown_improvement
        )
    return gates
