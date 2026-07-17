import pandas as pd

import numpy as np

from utils.metrics import (
    annualized_volatility,
    cagr,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
)


def test_max_drawdown_negative_or_zero():
    equity = pd.Series([100, 110, 105, 90, 95])
    result = max_drawdown(equity)
    assert result <= 0


def test_annualized_volatility_non_negative():
    returns = pd.Series([0.01, -0.02, 0.005, 0.003])
    result = annualized_volatility(returns)
    assert result >= 0


def test_cagr_float_output():
    equity = pd.Series([100, 105, 110, 120])
    result = cagr(equity, periods_per_year=4)
    assert isinstance(result, float)


def test_cagr_uses_elapsed_period_count():
    equity = pd.Series([100.0, 110.0])

    result = cagr(equity, periods_per_year=1)

    assert abs(result - 0.10) < 1e-12


def test_cagr_uses_actual_calendar_days_for_datetime_index():
    equity = pd.Series(
        [100.0, 121.0], index=pd.to_datetime(["2020-01-01", "2022-01-01"])
    )

    expected = 1.21 ** (1.0 / (731.0 / 365.2425)) - 1.0

    assert abs(cagr(equity) - expected) < 1e-12


def test_sortino_uses_full_sample_downside_deviation():
    returns = pd.Series([0.02, -0.01, 0.03, -0.02])
    downside_deviation = np.sqrt((0.0 + 0.01**2 + 0.0 + 0.02**2) / 4.0)
    expected = returns.mean() / downside_deviation * np.sqrt(252.0)

    assert abs(sortino_ratio(returns) - expected) < 1e-12


def test_sharpe_accepts_aligned_daily_risk_free_series():
    index = pd.date_range("2024-01-01", periods=4)
    returns = pd.Series([0.01, 0.02, -0.005, 0.003], index=index)
    daily_rf = pd.Series([0.001, 0.001, 0.001, 0.001], index=index)
    excess = returns - daily_rf
    expected = excess.mean() / excess.std() * np.sqrt(252.0)

    assert abs(sharpe_ratio(returns, daily_rf) - expected) < 1e-12


def test_ratios_return_nan_at_zero_denominator():
    assert np.isnan(sharpe_ratio(pd.Series([0.0, 0.0])))
    assert np.isnan(sortino_ratio(pd.Series([0.01, 0.02])))
