from __future__ import annotations

import numpy as np
import pandas as pd


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



def sharpe_ratio(returns: pd.Series, rf: float = 0.0, periods_per_year: int = 252) -> float:
    ret = returns.dropna()
    if ret.empty or ret.std() == 0:
        return np.nan
    excess = ret - rf / periods_per_year
    return float(excess.mean() / ret.std() * np.sqrt(periods_per_year))



def sortino_ratio(returns: pd.Series, rf: float = 0.0, periods_per_year: int = 252) -> float:
    ret = returns.dropna()
    if ret.empty:
        return np.nan
    downside = ret[ret < 0]
    if downside.empty or downside.std() == 0:
        return np.nan
    excess = ret - rf / periods_per_year
    return float(excess.mean() / downside.std() * np.sqrt(periods_per_year))



def cagr(equity_curve: pd.Series, periods_per_year: int = 252) -> float:
    curve = equity_curve.dropna()
    if len(curve) < 2:
        return np.nan
    total_return = curve.iloc[-1] / curve.iloc[0]
    years = (len(curve) - 1) / periods_per_year
    if years <= 0:
        return np.nan
    return float(total_return ** (1 / years) - 1)
