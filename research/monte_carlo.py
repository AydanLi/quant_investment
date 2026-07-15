from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping, Optional

import numpy as np
import pandas as pd


REQUIRED_PORTFOLIO_COLUMNS = (
    "gross_return",
    "daily_return",
    "turnover",
    "est_trading_cost",
    "est_slippage",
    "est_cost",
)


@dataclass(frozen=True)
class MonteCarloThresholds:
    minimum_median_sharpe_improvement: float = 0.05
    minimum_median_drawdown_reduction: float = 0.10
    minimum_joint_improvement_probability: float = 0.60
    minimum_after_cost_return_win_probability: float = 0.50
    minimum_perturbation_pass_rate: float = 0.67
    minimum_start_date_pass_rate: float = 0.67


@dataclass(frozen=True)
class PairedBootstrapResult:
    simulations: int
    horizon: int
    block_length: int
    seed: int
    path_metrics: pd.DataFrame
    summary: dict[str, object]


def circular_block_indices(
    observations: int,
    *,
    simulations: int,
    horizon: int,
    block_length: int,
    seed: int,
) -> np.ndarray:
    """Create reproducible circular block-bootstrap row indices."""
    if observations < 2:
        raise ValueError("At least two observations are required.")
    if simulations < 1:
        raise ValueError("simulations must be at least 1.")
    if horizon < 2:
        raise ValueError("horizon must be at least 2.")
    if not 1 <= block_length <= observations:
        raise ValueError("block_length must be between 1 and observations.")

    generator = np.random.default_rng(seed)
    block_count = int(np.ceil(horizon / block_length))
    starts = generator.integers(
        0, observations, size=(simulations, block_count)
    )
    offsets = np.arange(block_length)
    blocks = (starts[:, :, None] + offsets) % observations
    return blocks.reshape(simulations, -1)[:, :horizon]


def _validate_and_align(
    baseline: pd.DataFrame,
    candidate: pd.DataFrame,
    start: Optional[pd.Timestamp],
    end: Optional[pd.Timestamp],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    baseline_period = baseline.loc[start:end].copy()
    candidate_period = candidate.loc[start:end].copy()
    if baseline_period.empty or candidate_period.empty:
        raise ValueError("The requested Monte Carlo period is empty.")
    if not baseline_period.index.equals(candidate_period.index):
        raise ValueError("Baseline and candidate must use exactly the same dates.")

    missing = {
        "baseline": sorted(
            set(REQUIRED_PORTFOLIO_COLUMNS).difference(baseline_period.columns)
        ),
        "candidate": sorted(
            set(REQUIRED_PORTFOLIO_COLUMNS).difference(candidate_period.columns)
        ),
    }
    if missing["baseline"] or missing["candidate"]:
        raise ValueError(f"Missing required portfolio columns: {missing}")

    for label, frame in (
        ("baseline", baseline_period),
        ("candidate", candidate_period),
    ):
        numeric = frame.loc[:, REQUIRED_PORTFOLIO_COLUMNS].apply(
            pd.to_numeric, errors="coerce"
        )
        if numeric.isna().any().any():
            raise ValueError(f"{label} contains non-numeric or missing values.")
        if (numeric["daily_return"] <= -1.0).any():
            raise ValueError(f"{label} contains a return at or below -100%.")
        if (
            numeric[
                [
                    "turnover",
                    "est_trading_cost",
                    "est_slippage",
                    "est_cost",
                ]
            ]
            < 0.0
        ).any().any():
            raise ValueError(f"{label} contains negative turnover or costs.")
        if not np.allclose(
            numeric["est_cost"],
            numeric["est_trading_cost"] + numeric["est_slippage"],
            rtol=1e-9,
            atol=1e-12,
        ):
            raise ValueError(
                f"{label} total cost does not equal trading cost plus slippage."
            )
        expected_net_return = (
            (1.0 + numeric["gross_return"]) * (1.0 - numeric["est_cost"])
            - 1.0
        )
        if not np.allclose(
            numeric["daily_return"],
            expected_net_return,
            rtol=1e-9,
            atol=1e-12,
        ):
            raise ValueError(
                f"{label} daily_return is not net of its recorded costs."
            )
        frame.loc[:, REQUIRED_PORTFOLIO_COLUMNS] = numeric
    return baseline_period, candidate_period


def _path_metrics(frame: pd.DataFrame, indices: np.ndarray) -> dict[str, np.ndarray]:
    sampled_returns = frame["daily_return"].to_numpy(dtype=float)[indices]
    equity = np.cumprod(1.0 + sampled_returns, axis=1)
    running_peak = np.maximum.accumulate(
        np.concatenate([np.ones((len(equity), 1)), equity], axis=1),
        axis=1,
    )[:, 1:]
    drawdown = equity / running_peak - 1.0
    standard_deviation = sampled_returns.std(axis=1, ddof=1)
    sharpe = np.divide(
        sampled_returns.mean(axis=1) * np.sqrt(252.0),
        standard_deviation,
        out=np.full(len(sampled_returns), np.nan),
        where=standard_deviation > 0.0,
    )

    metrics = {
        "sharpe": sharpe,
        "max_drawdown": drawdown.min(axis=1),
        "total_return": equity[:, -1] - 1.0,
    }
    for column in (
        "turnover",
        "est_trading_cost",
        "est_slippage",
        "est_cost",
    ):
        metrics[column] = frame[column].to_numpy(dtype=float)[indices].sum(axis=1)
    return metrics


def _percentiles(values: pd.Series) -> dict[str, float]:
    return {
        "p05": float(values.quantile(0.05)),
        "median": float(values.median()),
        "p95": float(values.quantile(0.95)),
    }


def _build_summary(
    path_metrics: pd.DataFrame,
    baseline: pd.DataFrame,
    candidate: pd.DataFrame,
) -> dict[str, object]:
    distribution_columns = (
        "sharpe",
        "max_drawdown",
        "total_return",
        "turnover",
        "est_trading_cost",
        "est_slippage",
        "est_cost",
    )
    distributions = {
        label: {
            metric: _percentiles(path_metrics[f"{label}_{metric}"])
            for metric in distribution_columns
        }
        for label in ("baseline", "candidate")
    }
    paired = {
        "sharpe_improvement": _percentiles(
            path_metrics["sharpe_improvement"]
        ),
        "drawdown_reduction": _percentiles(
            path_metrics["drawdown_reduction"]
        ),
        "return_improvement": _percentiles(
            path_metrics["return_improvement"]
        ),
        "probability_sharpe_improvement": float(
            (path_metrics["sharpe_improvement"] > 0.0).mean()
        ),
        "probability_drawdown_reduction": float(
            (path_metrics["drawdown_reduction"] > 0.0).mean()
        ),
        "probability_return_improvement": float(
            (path_metrics["return_improvement"] > 0.0).mean()
        ),
        "probability_joint_improvement": float(
            (
                (path_metrics["sharpe_improvement"] > 0.0)
                & (path_metrics["drawdown_reduction"] > 0.0)
            ).mean()
        ),
    }
    source_totals = {
        label: {
            column: float(frame[column].sum())
            for column in (
                "turnover",
                "est_trading_cost",
                "est_slippage",
                "est_cost",
            )
        }
        for label, frame in (("baseline", baseline), ("candidate", candidate))
    }
    source_totals["dates"] = {
        "start": str(baseline.index.min().date()),
        "end": str(baseline.index.max().date()),
        "observations": len(baseline),
    }
    return {
        "distributions": distributions,
        "paired": paired,
        "source_totals": source_totals,
        "candidate_probability_of_loss": float(
            (path_metrics["candidate_total_return"] < 0.0).mean()
        ),
        "baseline_probability_of_loss": float(
            (path_metrics["baseline_total_return"] < 0.0).mean()
        ),
    }


def paired_block_bootstrap(
    baseline: pd.DataFrame,
    candidate: pd.DataFrame,
    *,
    start: Optional[pd.Timestamp] = None,
    end: Optional[pd.Timestamp] = None,
    simulations: int = 5000,
    horizon: Optional[int] = None,
    block_length: int = 20,
    seed: int = 20260715,
) -> PairedBootstrapResult:
    """Bootstrap identical net-return blocks for a baseline and candidate.

    ``daily_return`` is already net of the stored trading-cost and slippage
    columns. Costs are sampled for audit and must not be subtracted a second
    time.
    """
    baseline_period, candidate_period = _validate_and_align(
        baseline, candidate, start, end
    )
    path_horizon = len(baseline_period) if horizon is None else int(horizon)
    indices = circular_block_indices(
        len(baseline_period),
        simulations=simulations,
        horizon=path_horizon,
        block_length=block_length,
        seed=seed,
    )
    baseline_metrics = _path_metrics(baseline_period, indices)
    candidate_metrics = _path_metrics(candidate_period, indices)
    path_metrics = pd.DataFrame(
        {
            **{
                f"baseline_{name}": values
                for name, values in baseline_metrics.items()
            },
            **{
                f"candidate_{name}": values
                for name, values in candidate_metrics.items()
            },
        }
    )
    path_metrics["sharpe_improvement"] = (
        path_metrics["candidate_sharpe"] - path_metrics["baseline_sharpe"]
    )
    baseline_drawdown = path_metrics["baseline_max_drawdown"].abs()
    path_metrics["drawdown_reduction"] = np.divide(
        baseline_drawdown - path_metrics["candidate_max_drawdown"].abs(),
        baseline_drawdown,
        out=np.zeros(len(path_metrics), dtype=float),
        where=baseline_drawdown > 0.0,
    )
    path_metrics["return_improvement"] = (
        path_metrics["candidate_total_return"]
        - path_metrics["baseline_total_return"]
    )
    summary = _build_summary(
        path_metrics, baseline_period, candidate_period
    )
    return PairedBootstrapResult(
        simulations=simulations,
        horizon=path_horizon,
        block_length=block_length,
        seed=seed,
        path_metrics=path_metrics,
        summary=summary,
    )


def _variant_passes(result: PairedBootstrapResult) -> bool:
    paired = result.summary["paired"]
    return bool(
        paired["sharpe_improvement"]["median"] > 0.0
        and paired["drawdown_reduction"]["median"] >= 0.0
    )


def evaluate_monte_carlo_robustness(
    main_result: PairedBootstrapResult,
    *,
    block_perturbations: Mapping[str, PairedBootstrapResult],
    start_date_results: Mapping[str, PairedBootstrapResult],
    thresholds: MonteCarloThresholds = MonteCarloThresholds(),
) -> dict[str, object]:
    """Evaluate robustness evidence; this does not admit a trading signal."""
    paired = main_result.summary["paired"]
    source = main_result.summary["source_totals"]
    perturbation_passes = {
        label: _variant_passes(result)
        for label, result in block_perturbations.items()
    }
    start_passes = {
        label: _variant_passes(result)
        for label, result in start_date_results.items()
    }
    perturbation_pass_rate = (
        sum(perturbation_passes.values()) / len(perturbation_passes)
        if perturbation_passes
        else 0.0
    )
    start_date_pass_rate = (
        sum(start_passes.values()) / len(start_passes)
        if start_passes
        else 0.0
    )
    gates = {
        "median_oos_sharpe": (
            paired["sharpe_improvement"]["median"]
            >= thresholds.minimum_median_sharpe_improvement
        ),
        "median_maximum_drawdown": (
            paired["drawdown_reduction"]["median"]
            >= thresholds.minimum_median_drawdown_reduction
        ),
        "majority_of_paths": (
            paired["probability_joint_improvement"]
            >= thresholds.minimum_joint_improvement_probability
        ),
        "after_costs_and_slippage": (
            paired["probability_return_improvement"]
            >= thresholds.minimum_after_cost_return_win_probability
            and source["candidate"]["est_trading_cost"] > 0.0
            and source["candidate"]["est_slippage"] > 0.0
        ),
        "block_length_robustness": (
            perturbation_pass_rate
            >= thresholds.minimum_perturbation_pass_rate
        ),
        "start_date_robustness": (
            start_date_pass_rate >= thresholds.minimum_start_date_pass_rate
        ),
    }
    return {
        "robustness_supported": all(gates.values()),
        "admitted_as_trading_model": False,
        "affects_weights": False,
        "signal_independence": "not_applicable_no_signal",
        "thresholds": asdict(thresholds),
        "gates": gates,
        "block_length_pass_rate": perturbation_pass_rate,
        "block_length_passes": perturbation_passes,
        "start_date_pass_rate": start_date_pass_rate,
        "start_date_passes": start_passes,
    }
