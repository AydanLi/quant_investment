from __future__ import annotations

import numpy as np
import pandas as pd


def _excess_returns(
    returns: pd.Series,
    rf: float | pd.Series = 0.0,
    periods_per_year: int = 252,
) -> pd.Series:
    ret = returns.dropna().astype(float)
    if isinstance(rf, pd.Series):
        aligned = pd.concat(
            [ret.rename("return"), rf.astype(float).rename("rf")], axis=1
        ).dropna()
        return aligned["return"] - aligned["rf"]
    return ret - float(rf) / periods_per_year


def annualized_volatility(returns: pd.Series, periods_per_year: int = 252) -> float:
    ret = returns.dropna()
    if ret.empty:
        return np.nan
    return float(ret.std() * np.sqrt(periods_per_year))



def max_drawdown(equity_curve: pd.Series) -> float:
    curve = equity_curve.dropna()
    if curve.empty:
        return np.nan
    running_max = curve.cummax()
    dd = curve / running_max - 1.0
    return float(dd.min())



def sharpe_ratio(
    returns: pd.Series,
    rf: float | pd.Series = 0.0,
    periods_per_year: int = 252,
) -> float:
    excess = _excess_returns(returns, rf, periods_per_year)
    volatility = excess.std()
    if excess.empty or not np.isfinite(volatility) or volatility == 0:
        return np.nan
    return float(excess.mean() / volatility * np.sqrt(periods_per_year))



def sortino_ratio(
    returns: pd.Series,
    rf: float | pd.Series = 0.0,
    periods_per_year: int = 252,
) -> float:
    """Annualized Sortino using full-sample downside deviation.

    Positive observations remain in the denominator as zero downside.  This
    avoids the common upward bias from taking the standard deviation only of
    negative observations.
    """
    excess = _excess_returns(returns, rf, periods_per_year)
    if excess.empty:
        return np.nan
    downside = np.minimum(excess.to_numpy(dtype=float), 0.0)
    downside_deviation = float(np.sqrt(np.mean(np.square(downside))))
    if not np.isfinite(downside_deviation) or downside_deviation == 0.0:
        return np.nan
    return float(excess.mean() / downside_deviation * np.sqrt(periods_per_year))



def cagr(equity_curve: pd.Series, periods_per_year: int = 252) -> float:
    curve = equity_curve.dropna()
    if len(curve) < 2:
        return np.nan
    total_return = curve.iloc[-1] / curve.iloc[0]
    if isinstance(curve.index, pd.DatetimeIndex):
        elapsed_days = (curve.index[-1] - curve.index[0]).total_seconds() / 86_400
        years = elapsed_days / 365.2425
    else:
        years = (len(curve) - 1) / periods_per_year
    if years <= 0:
        return np.nan
    return float(total_return ** (1 / years) - 1)


def calmar_ratio(equity_curve: pd.Series) -> float:
    growth = cagr(equity_curve)
    drawdown = abs(max_drawdown(equity_curve))
    if not np.isfinite(growth) or not np.isfinite(drawdown) or drawdown == 0.0:
        return np.nan
    return float(growth / drawdown)


def trade_win_rate(realized_pnl: pd.Series) -> float:
    pnl = realized_pnl.dropna().astype(float)
    if pnl.empty:
        return np.nan
    return float((pnl > 0.0).mean())


def profit_factor(realized_pnl: pd.Series) -> float:
    pnl = realized_pnl.dropna().astype(float)
    gross_profit = float(pnl[pnl > 0.0].sum())
    gross_loss = abs(float(pnl[pnl < 0.0].sum()))
    if gross_loss == 0.0:
        return np.nan
    return gross_profit / gross_loss
