import pandas as pd

from utils.metrics import annualized_volatility, cagr, max_drawdown


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