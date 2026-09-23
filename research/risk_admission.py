"""Risk extensions are selected on development data and tested only after core discovery."""
from __future__ import annotations

from dataclasses import asdict, replace
from typing import Callable, Mapping

import numpy as np
import pandas as pd

from research.core_evaluator import evaluate_config_path, path_metrics, METRIC_POLICY
from research.model_admission import AdmissionThresholds
from research.nested_walk_forward import ExpandingFold, build_nested_folds
from research.risk_model_protocol import dynamic_factor_candidate_grid
from research.runtime import FrozenRuntimeManifest, build_runtime_manifest, canonical_hash


def build_risk_protocol(parent: FrozenRuntimeManifest, *, dataset_snapshot_id: int,
                        holdout_start: str) -> dict[str, object]:
    if parent.config["risk_model"] != "sample":
        raise ValueError("Risk extension requires a frozen sample-covariance core parent.")
    cutoff = pd.Timestamp(parent.research_cutoff)
    holdout = pd.Timestamp(holdout_start)
    if holdout <= cutoff:
        raise ValueError("Risk holdout must start after the parent core research cutoff.")
    return {
        "protocol_version": "risk-extension-v1", "candidate_count": 6,
        "parent_runtime_hash": parent.runtime_hash,
        "base_config": dict(parent.config), "code_identity": dict(parent.code_identity),
        "dataset_snapshot_id": dataset_snapshot_id,
        "core_research_cutoff": str(cutoff.date()), "holdout_start": str(holdout.date()),
        "minimum_holdout_sessions": 252,
        "minimum_independent_outer_windows": 3,
        "start_date_offsets_months": [0, 3, 6],
        "minimum_outer_training_years": 5, "minimum_inner_training_years": 3,
        "outer_test_months": 12,
        "candidates": [asdict(item) for item in dynamic_factor_candidate_grid()],
        "thresholds": asdict(AdmissionThresholds()),
        "deployment_policy": "fixed_parameters_until_manually_approved_new_version",
    }


def run_risk_admission(
    parent: FrozenRuntimeManifest, data: Mapping[str, pd.DataFrame],
    protocol: Mapping[str, object], *, strategy_version: str,
    evaluator: Callable = evaluate_config_path, trial_callback: Callable | None = None,
    independence_evaluator: Callable | None = None,
) -> dict[str, object]:
    """Old rows may train the conditional model, never count as independent OOS.

    Development outer folds end before the reserved holdout. Every risk choice
    is made in its own training history. The final fixed choice is selected on
    development folds, then evaluated once on the withheld period.
    """
    if protocol["parent_runtime_hash"] != parent.runtime_hash:
        raise ValueError("Risk protocol belongs to a different frozen core.")
    expected_protocol = build_risk_protocol(parent, dataset_snapshot_id=int(protocol["dataset_snapshot_id"]),
                                            holdout_start=str(protocol["holdout_start"]))
    if canonical_hash(protocol) != canonical_hash(expected_protocol):
        raise ValueError("Risk protocol differs from the complete preregistered candidate/gate definition.")
    base = parent.to_config()
    dates = pd.DatetimeIndex(data[base.benchmark].index).sort_values()
    holdout_start = pd.Timestamp(protocol["holdout_start"])
    cutoff = pd.Timestamp(parent.research_cutoff)
    if holdout_start <= cutoff:
        raise ValueError("Risk holdout overlaps the core discovery period.")
    development_dates = dates[dates < holdout_start]
    holdout_dates = dates[dates >= holdout_start]
    candidates = dynamic_factor_candidate_grid()
    configs = {candidate.label: replace(
        base, risk_model="dynamic_factor", ewma_half_life_days=candidate.half_life_days,
        pca_stress_multiplier=candidate.stress_multiplier,
    ) for candidate in candidates}
    trials = []

    def finish_trial(stage, fold_key, label, metric):
        trial = {"stage": stage, "fold_key": fold_key, "label": label,
                 "parameters": asdict(next(item for item in candidates if item.label == label)),
                 "folds": [asdict(metric)], "score": metric.selection_score,
                 "status": "DEGENERATE_ALL_CASH" if metric.degenerate_all_cash else "evaluated"}
        trials.append(trial)
        if trial_callback is not None:
            trial_callback(trial)

    def slice_data(start, end):
        return {ticker: frame.loc[start:end].copy() for ticker, frame in data.items()}

    def evaluate(config, fold, state=None):
        return evaluator(config,
                         slice_data(dates[0], fold.training_end),
                         slice_data(fold.validation_start, fold.validation_end),
                         initial_state=state)

    result = {"methodology": "nested_risk_extension_v1", "admitted": False,
              "status": "INSUFFICIENT_EVIDENCE", "selected_admitted_candidate": None,
              "baseline_model": "sample", "core_strategy_frozen": True,
              "candidate_count": 6, "parent_runtime_hash": parent.runtime_hash,
              "evidence_role": "post_core_holdout", "protocol_hash": canonical_hash(protocol),
              "selection_uses_future_holdout": False, "risk_model_default_remains": "sample",
              "old_data_role": "conditional_training_only", "trials": trials}
    result["metric_policy"] = METRIC_POLICY
    try:
        folds = build_nested_folds(
            development_dates,
            minimum_outer_training_years=int(protocol["minimum_outer_training_years"]),
            minimum_inner_training_years=int(protocol["minimum_inner_training_years"]),
        )
    except ValueError as exc:
        return {**result, "reason": str(exc)}
    # The parent core must not have seen any independent test observation.
    independent = [item for item in folds if item.outer.validation_start > cutoff]
    if len(independent) < int(protocol["minimum_independent_outer_windows"]) or len(holdout_dates) < int(protocol["minimum_holdout_sessions"]):
        return {**result, "reason": "Require three complete post-core outer windows and 252 untouched holdout sessions."}
    states = {"baseline": None, "selected": None}
    paths = {"baseline": [], "selected": []}
    outer_comparisons = []
    final_scores = {label: [] for label in configs}
    development_metrics = {label: [] for label in configs}
    development_baselines = []
    for number, nested in enumerate(independent, 1):
        scores = {label: [] for label in configs}
        for label, config in configs.items():
            for inner_number, inner in enumerate(nested.inner, 1):
                metric = evaluate(config, inner)["metrics"]
                scores[label].append(metric.selection_score)
                finish_trial("risk_inner", f"{number}/{inner_number}", label, metric)
        selected = max(scores, key=lambda label: (float(np.mean(scores[label])), label))
        if not np.isfinite(np.mean(scores[selected])):
            return {**result, "status": "REJECTED", "reason": "Every inner candidate is invalid."}
        pair = {}
        for name, config in (("baseline", base), ("selected", configs[selected])):
            path = evaluate(config, nested.outer, states[name])
            states[name] = path["final_state"]
            paths[name].append(path["portfolio"])
            pair[name] = path["metrics"]
        outer_comparisons.append({"selected_label": selected,
                                  "fold": asdict(nested.outer),
                                  **{name: asdict(metric) for name, metric in pair.items()}})
        development_baselines.append(evaluate(base, nested.outer)["metrics"])
        # These development results are selection evidence, not final holdout.
        for label, config in configs.items():
            metric = evaluate(config, nested.outer)["metrics"]
            final_scores[label].append(metric.selection_score)
            development_metrics[label].append(metric)
            finish_trial("risk_development", str(number), label, metric)
    final_label = max(final_scores, key=lambda label: (float(np.mean(final_scores[label])), label))
    for label, scores in final_scores.items():
        trial = {"stage": "final_selection_summary", "fold_key": "aggregate", "label": label,
                 "parameters": asdict(next(item for item in candidates if item.label == label)),
                 "folds": [asdict(metric) for metric in development_metrics[label]],
                 "score": float(np.mean(scores)),
                 "status": "evaluated" if all(np.isfinite(scores)) else "DEGENERATE_ALL_CASH"}
        trials.append(trial)
        if trial_callback is not None:
            trial_callback(trial)
    if not np.isfinite(np.mean(final_scores[final_label])):
        return {**result, "status": "REJECTED", "reason": "No finite final candidate score."}
    holdout_fold = ExpandingFold(dates[0], development_dates[-1], holdout_dates[0], holdout_dates[-1])
    # A fixed candidate starts the reserved period from the same declared cash
    # account as the baseline. Hypothetical training holdings are not imported.
    final_path = evaluate(configs[final_label], holdout_fold)
    baseline_path = evaluate(base, holdout_fold)
    final_metric, baseline_metric = final_path["metrics"], baseline_path["metrics"]
    finish_trial("risk_holdout", "reserved", final_label, final_metric)
    thresholds = AdmissionThresholds(**protocol["thresholds"])
    reduction = ((abs(baseline_metric.max_drawdown) - abs(final_metric.max_drawdown))
                 / abs(baseline_metric.max_drawdown)) if baseline_metric.max_drawdown < 0 else 0.0
    win_rate = float(np.mean([item["selected"]["net_return"] > item["baseline"]["net_return"]
                              for item in outer_comparisons]))
    def improves(metric, benchmark):
        return (not metric.degenerate_all_cash and metric.excess_sharpe > benchmark.excess_sharpe
                and metric.max_drawdown >= benchmark.max_drawdown)

    parameter_rates = {label: float(np.mean([
        improves(metric, benchmark) for metric, benchmark in zip(metrics, development_baselines)
    ])) for label, metrics in development_metrics.items()}
    neighbor_rate = float(np.mean([rate >= thresholds.minimum_window_win_rate
                                   for rate in parameter_rates.values()]))
    start_date_results = []
    for nested in independent:
        for offset in protocol["start_date_offsets_months"]:
            requested_start = nested.outer.validation_start + pd.DateOffset(months=int(offset))
            start = dates[dates >= requested_start][0]
            prior = dates[dates < start][-1]
            shifted = ExpandingFold(dates[0], prior, start, nested.outer.validation_end)
            candidate_metric = evaluate(configs[final_label], shifted)["metrics"]
            sample_metric = evaluate(base, shifted)["metrics"]
            finish_trial("risk_start_date", str(start.date()), final_label, candidate_metric)
            start_date_results.append({"start": str(start.date()), "candidate": asdict(candidate_metric),
                                       "baseline": asdict(sample_metric), "passed": improves(candidate_metric, sample_metric)})
    start_rate = float(np.mean([item["passed"] for item in start_date_results]))
    independence = (independence_evaluator or signal_independence)(
        configs[final_label], slice_data(dates[0], development_dates[-1]),
        independent[0].outer.validation_start)
    correlation = float(independence["correlation"])
    gates = {"independent_dates": True,
             "non_degenerate": not final_metric.degenerate_all_cash and not baseline_metric.degenerate_all_cash,
             "holdout_sharpe": final_metric.excess_sharpe - baseline_metric.excess_sharpe >= thresholds.minimum_sharpe_improvement,
             "holdout_drawdown": reduction >= thresholds.minimum_drawdown_reduction,
             "after_cost_return": final_metric.net_return > baseline_metric.net_return,
             "rolling_windows": win_rate >= thresholds.minimum_window_win_rate,
             "parameter_robustness": neighbor_rate >= thresholds.minimum_parameter_pass_rate,
             "start_date_robustness": start_rate >= thresholds.minimum_start_date_pass_rate,
             "independent_information": independence["observations"] >= 12 and np.isfinite(correlation)
                 and abs(correlation) <= thresholds.maximum_signal_correlation,
             "confirmed_cash_flows": final_metric.confirmed_cash_flows and baseline_metric.confirmed_cash_flows
                 and all(metric.confirmed_cash_flows for metrics in development_metrics.values() for metric in metrics)
                 and all(metric.confirmed_cash_flows for metric in development_baselines)
                 and all(item["candidate"]["confirmed_cash_flows"] and item["baseline"]["confirmed_cash_flows"]
                         for item in start_date_results)}
    admitted = all(gates.values())
    selected_config = replace(configs[final_label], strategy_version=strategy_version)
    manifest = build_runtime_manifest(
        selected_config, code_identity=parent.code_identity,
        research_cutoff=str(dates[-1].date()), dataset_snapshot_id=int(protocol["dataset_snapshot_id"]),
        protocol_hash=canonical_hash(protocol), selected_label=final_label,
        parent_runtime_hash=parent.runtime_hash,
    )
    return {**result, "status": "ADMITTED" if admitted else "REJECTED", "admitted": admitted,
            "gates": gates, "selected_label": final_label,
            "selected_admitted_candidate": final_label if admitted else None,
            "risk_model_default_remains": final_label if admitted else "sample",
            "outer_folds": outer_comparisons,
            "parameter_pass_rates": parameter_rates, "start_date_results": start_date_results,
            "independent_information": independence,
            "continuous_outer_metrics": {name: asdict(path_metrics(pd.concat(frames), base))
                                          for name, frames in paths.items()},
            "final_holdout": {"candidate": asdict(final_metric), "baseline": asdict(baseline_metric),
                              "start": str(holdout_dates[0].date()), "end": str(holdout_dates[-1].date())},
            "runtime_manifest": manifest.to_dict(), "runtime_hash": manifest.runtime_hash,
            "evaluations": {final_label: {"admitted": admitted, "gates": gates}},
            "deployment_policy": "fixed_parameters_until_manually_approved_new_version"}


def signal_independence(config, data, start) -> dict[str, object]:
    """Conditional signal diagnostic on development data only, before holdout."""
    from data.calendar import NyseCalendar
    from data.features import FeatureEngineer
    from risk.covariance import DynamicFactorRiskModel
    from strategy.momentum_rotation import MomentumRotationStrategy

    engineer = FeatureEngineer(dict(data), config)
    prices = engineer.make_price_frame()
    returns = engineer.make_returns_frame(prices)
    features = engineer.compute_features(prices, returns)
    strategy = MomentumRotationStrategy(config)
    model = DynamicFactorRiskModel(half_life_days=config.ewma_half_life_days,
                                   pca_stress_multiplier=config.pca_stress_multiplier)
    assets = [ticker for ticker in config.universe if ticker != config.cash_asset and ticker in returns]
    rows = []
    for date in prices.index:
        if date < start or not NyseCalendar().is_month_end_session(date):
            continue
        scores = strategy.score_assets(date, prices.loc[:date],
                                       {name: frame.loc[:date] for name, frame in features.items()})
        positive = scores[scores > 0].head(config.top_n)
        if positive.empty:
            continue
        try:
            estimate = model.estimate(returns[assets].loc[:date])
        except ValueError:
            continue
        rows.append((float(positive.mean()), estimate.first_factor_share))
    values = pd.DataFrame(rows, columns=["momentum", "factor_share"])
    correlation = float(values.corr().iloc[0, 1]) if len(values) >= 12 else float("nan")
    return {"correlation": correlation, "observations": len(values), "data_role": "development_diagnostic"}
