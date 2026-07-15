from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from research.factor_attribution import (
    FactorRegressionResult,
    RollingFactorAttribution,
    build_proxy_factor_returns,
    fit_factor_regression,
    rolling_attribution_summary,
    rolling_factor_attribution,
)


FACTOR_LABELS = {
    "equity_market": "市场",
    "growth_tilt": "成长",
    "size_tilt": "规模",
    "duration": "久期",
    "gold": "黄金",
    "energy_tilt": "能源",
    "defensive_tilt": "防御",
}


@dataclass(frozen=True)
class FactorMonitorResult:
    static_regression: FactorRegressionResult
    rolling_attribution: RollingFactorAttribution
    rolling_summary: dict[str, object]
    exposure_table: pd.DataFrame
    return_contribution: pd.Series
    risk_contribution: pd.Series
    warnings: tuple[str, ...]
    status: str
    affects_weights: bool = False


def _portfolio_return_series(portfolio: pd.DataFrame) -> pd.Series:
    if "daily_return" not in portfolio.columns:
        raise ValueError("portfolio must contain a daily_return column.")
    frame = portfolio.copy()
    if "date" in frame.columns:
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame.set_index("date")
    elif not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index)
    frame = frame.sort_index()
    if frame.index.has_duplicates:
        raise ValueError("portfolio contains duplicate dates.")
    returns = pd.to_numeric(frame["daily_return"], errors="coerce").dropna()
    if returns.empty:
        raise ValueError("portfolio has no usable daily returns.")
    return returns


def _normalized_prices(prices: pd.DataFrame) -> pd.DataFrame:
    frame = prices.copy()
    frame.index = pd.to_datetime(frame.index)
    frame = frame.sort_index()
    if frame.index.has_duplicates:
        raise ValueError("factor prices contain duplicate dates.")
    return frame.apply(pd.to_numeric, errors="coerce")


def build_factor_monitor(
    portfolio: pd.DataFrame,
    prices: pd.DataFrame,
    *,
    window: int = 252,
    minimum_observations: int = 126,
) -> FactorMonitorResult:
    """Build a read-only factor monitor for a stored experiment run."""
    portfolio_returns = _portfolio_return_series(portfolio)
    factors, cash = build_proxy_factor_returns(_normalized_prices(prices))

    static = fit_factor_regression(
        portfolio_returns,
        factors,
        cash_returns=cash,
    )
    rolling = rolling_factor_attribution(
        portfolio_returns,
        factors,
        cash_returns=cash,
        window=window,
        minimum_observations=minimum_observations,
    )
    summary = rolling_attribution_summary(rolling)

    exposure_columns = [
        factor for factor in FACTOR_LABELS if factor in rolling.exposures.columns
    ]
    exposures = rolling.exposures[exposure_columns]
    if exposures.empty:
        raise ValueError("Not enough observations to calculate rolling exposures.")
    latest = exposures.iloc[-1]
    lower = exposures.quantile(0.10)
    median = exposures.median()
    upper = exposures.quantile(0.90)

    statuses = {}
    warnings = []
    for factor in exposure_columns:
        label = FACTOR_LABELS[factor]
        if latest[factor] > upper[factor]:
            statuses[factor] = "高于历史90%分位"
            warnings.append(
                f"{label}暴露 {latest[factor]:.3f} 高于本次实验的历史90%分位。"
            )
        elif latest[factor] < lower[factor]:
            statuses[factor] = "低于历史10%分位"
            warnings.append(
                f"{label}暴露 {latest[factor]:.3f} 低于本次实验的历史10%分位。"
            )
        else:
            statuses[factor] = "正常区间"

    if float(summary["oos_r_squared"]) < 0.40:
        warnings.append("滚动样本外解释力低于40%，当前归因结果应谨慎使用。")
    if static.condition_number >= 30.0:
        warnings.append("代理因子存在较强共线性，单个暴露系数可能不稳定。")
    if (
        np.isfinite(static.t_statistics["alpha"])
        and static.t_statistics["alpha"] <= -1.96
    ):
        warnings.append("成本后回归Alpha显著为负，需要检查换手和择时损耗。")

    exposure_table = pd.DataFrame(
        {
            "因子": [FACTOR_LABELS[factor] for factor in exposure_columns],
            "最新暴露": [float(latest[factor]) for factor in exposure_columns],
            "历史10%": [float(lower[factor]) for factor in exposure_columns],
            "历史中位数": [float(median[factor]) for factor in exposure_columns],
            "历史90%": [float(upper[factor]) for factor in exposure_columns],
            "状态": [statuses[factor] for factor in exposure_columns],
        },
        index=exposure_columns,
    )
    return FactorMonitorResult(
        static_regression=static,
        rolling_attribution=rolling,
        rolling_summary=summary,
        exposure_table=exposure_table,
        return_contribution=static.annualized_return_contribution.copy(),
        risk_contribution=static.variance_contribution.copy(),
        warnings=tuple(warnings),
        status="watch" if warnings else "normal",
        affects_weights=False,
    )
