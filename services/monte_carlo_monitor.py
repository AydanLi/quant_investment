from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
import pandas as pd

from research.monte_carlo import circular_block_indices


@dataclass(frozen=True)
class MonteCarloMonitorResult:
    simulations: int
    horizon: int
    block_length: int
    observations: int
    probability_of_loss: float
    tail_max_drawdown: float
    median_max_drawdown: float
    median_total_return: float
    median_sharpe: float
    median_turnover: float
    median_cost: float
    distribution_table: pd.DataFrame
    sensitivity_table: pd.DataFrame
    equity_quantiles: pd.DataFrame
    warnings: tuple[str, ...]
    status: str
    affects_weights: bool = False


def _normalized_portfolio(
    portfolio: pd.DataFrame,
) -> tuple[pd.DataFrame, tuple[str, ...]]:
    if "daily_return" not in portfolio.columns:
        raise ValueError("portfolio must contain a daily_return column.")
    frame = portfolio.copy(deep=True)
    if "date" in frame.columns:
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame.set_index("date")
    elif not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index)
    frame = frame.sort_index()
    if frame.index.has_duplicates:
        raise ValueError("portfolio contains duplicate dates.")

    warnings = []
    frame["daily_return"] = pd.to_numeric(
        frame["daily_return"], errors="coerce"
    )
    frame = frame.dropna(subset=["daily_return"])
    if len(frame) < 60:
        raise ValueError("At least 60 daily returns are required.")
    if (frame["daily_return"] <= -1.0).any():
        raise ValueError("portfolio contains a return at or below -100%.")

    for column, message in (
        ("turnover", "该 run 缺少换手数据，换手分布按0处理。"),
        ("est_cost", "该 run 缺少成本数据，成本分布按0处理。"),
    ):
        if column not in frame.columns:
            frame[column] = 0.0
            warnings.append(message)
        else:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
            if frame[column].isna().any():
                raise ValueError(f"portfolio contains invalid {column} values.")
            if (frame[column] < 0.0).any():
                raise ValueError(f"portfolio contains negative {column} values.")
    return frame, tuple(warnings)


def _simulate(
    frame: pd.DataFrame,
    *,
    simulations: int,
    horizon: int,
    block_length: int,
    seed: int,
) -> dict[str, np.ndarray]:
    indices = circular_block_indices(
        len(frame),
        simulations=simulations,
        horizon=horizon,
        block_length=block_length,
        seed=seed,
    )
    returns = frame["daily_return"].to_numpy(dtype=float)[indices]
    equity = np.cumprod(1.0 + returns, axis=1)
    running_peak = np.maximum.accumulate(
        np.concatenate([np.ones((simulations, 1)), equity], axis=1),
        axis=1,
    )[:, 1:]
    max_drawdown = (equity / running_peak - 1.0).min(axis=1)
    standard_deviation = returns.std(axis=1, ddof=1)
    sharpe = np.divide(
        returns.mean(axis=1) * np.sqrt(252.0),
        standard_deviation,
        out=np.full(simulations, np.nan),
        where=standard_deviation > 0.0,
    )
    return {
        "equity": equity,
        "total_return": equity[:, -1] - 1.0,
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "turnover": frame["turnover"].to_numpy(dtype=float)[indices].sum(axis=1),
        "cost": frame["est_cost"].to_numpy(dtype=float)[indices].sum(axis=1),
    }


def _percentiles(values: np.ndarray) -> tuple[float, float, float]:
    return tuple(float(value) for value in np.quantile(values, [0.05, 0.50, 0.95]))


def build_monte_carlo_monitor(
    portfolio: pd.DataFrame,
    *,
    simulations: int = 3000,
    horizon: int = 252,
    block_length: int = 20,
    seed: int = 20260715,
    sensitivity_blocks: Sequence[int] = (10, 20, 40),
    sensitivity_simulations: int = 1000,
) -> MonteCarloMonitorResult:
    """Build a deterministic, single-run, read-only tail-risk monitor."""
    frame, data_warnings = _normalized_portfolio(portfolio)
    if block_length > len(frame):
        raise ValueError("block_length cannot exceed available observations.")
    if horizon < 20:
        raise ValueError("horizon must be at least 20 trading days.")

    paths = _simulate(
        frame,
        simulations=simulations,
        horizon=horizon,
        block_length=block_length,
        seed=seed,
    )
    distribution_rows = []
    for label, key, unit in (
        ("总收益", "total_return", "percent"),
        ("最大回撤", "max_drawdown", "percent"),
        ("Sharpe", "sharpe", "number"),
        ("换手", "turnover", "number"),
        ("估算成本", "cost", "percent"),
    ):
        lower, median, upper = _percentiles(paths[key])
        distribution_rows.append(
            {
                "指标": label,
                "5%": lower,
                "中位数": median,
                "95%": upper,
                "单位": unit,
            }
        )
    distribution_table = pd.DataFrame(distribution_rows)

    sensitivity_rows = []
    for offset, candidate_block in enumerate(sensitivity_blocks):
        if candidate_block < 1 or candidate_block > len(frame):
            continue
        result = _simulate(
            frame,
            simulations=sensitivity_simulations,
            horizon=horizon,
            block_length=int(candidate_block),
            seed=seed + 100 + offset,
        )
        sensitivity_rows.append(
            {
                "区块长度": int(candidate_block),
                "亏损概率": float((result["total_return"] < 0.0).mean()),
                "5%尾部回撤": float(np.quantile(result["max_drawdown"], 0.05)),
                "中位总收益": float(np.median(result["total_return"])),
                "中位Sharpe": float(np.nanmedian(result["sharpe"])),
            }
        )
    sensitivity_table = pd.DataFrame(sensitivity_rows)

    equity_with_initial = np.concatenate(
        [np.ones((simulations, 1)), paths["equity"]], axis=1
    )
    equity_quantiles = pd.DataFrame(
        np.quantile(equity_with_initial, [0.05, 0.50, 0.95], axis=0).T,
        columns=["5%路径", "中位路径", "95%路径"],
    )
    equity_quantiles.index.name = "交易日"

    probability_of_loss = float((paths["total_return"] < 0.0).mean())
    tail_max_drawdown = float(np.quantile(paths["max_drawdown"], 0.05))
    median_max_drawdown = float(np.median(paths["max_drawdown"]))
    median_total_return = float(np.median(paths["total_return"]))
    median_sharpe = float(np.nanmedian(paths["sharpe"]))
    warnings = list(data_warnings)
    if len(frame) < 252:
        warnings.append("历史数据少于252个交易日，年度尾部估计可信度有限。")
    if probability_of_loss >= 0.25:
        warnings.append(f"未来{horizon}日模拟亏损概率达到 {probability_of_loss:.1%}。")
    if tail_max_drawdown <= -0.20:
        warnings.append(f"5%尾部路径最大回撤达到 {tail_max_drawdown:.1%}。")
    if median_total_return <= 0.0:
        warnings.append("模拟中位总收益不为正，需要继续观察。")
    if not sensitivity_table.empty:
        signs = sensitivity_table["中位总收益"] > 0.0
        if signs.nunique() > 1:
            warnings.append("区块长度变化会改变中位收益方向，结果对参数敏感。")
    if frame["turnover"].sum() > 0.0 and frame["est_cost"].sum() == 0.0:
        warnings.append("存在换手但没有记录成本，成本后结果可能被高估。")

    return MonteCarloMonitorResult(
        simulations=simulations,
        horizon=horizon,
        block_length=block_length,
        observations=len(frame),
        probability_of_loss=probability_of_loss,
        tail_max_drawdown=tail_max_drawdown,
        median_max_drawdown=median_max_drawdown,
        median_total_return=median_total_return,
        median_sharpe=median_sharpe,
        median_turnover=float(np.median(paths["turnover"])),
        median_cost=float(np.median(paths["cost"])),
        distribution_table=distribution_table,
        sensitivity_table=sensitivity_table,
        equity_quantiles=equity_quantiles,
        warnings=tuple(warnings),
        status="watch" if warnings else "normal",
        affects_weights=False,
    )
