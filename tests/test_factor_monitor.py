import numpy as np
import pandas as pd

from research.factor_attribution import build_proxy_factor_returns
from services.factor_monitor import FACTOR_LABELS, build_factor_monitor


def _monitor_inputs(seed=19, periods=500):
    generator = np.random.default_rng(seed)
    index = pd.bdate_range("2022-01-03", periods=periods)
    common = generator.normal(0.0002, 0.008, periods)
    returns = {
        "SPY": common,
        "QQQ": common + generator.normal(0.0001, 0.003, periods),
        "IWM": common + generator.normal(-0.0001, 0.004, periods),
        "TLT": generator.normal(0.00005, 0.006, periods),
        "GLD": generator.normal(0.0001, 0.007, periods),
        "XLE": common + generator.normal(0.0, 0.006, periods),
        "XLV": 0.7 * common + generator.normal(0.00005, 0.003, periods),
        "BIL": np.full(periods, 0.00005),
    }
    prices = pd.DataFrame(
        {
            ticker: 100.0 * np.cumprod(1.0 + values)
            for ticker, values in returns.items()
        },
        index=index,
    )
    factors, cash = build_proxy_factor_returns(prices)
    portfolio_return = (
        cash
        + 0.35 * factors["equity_market"]
        + 0.20 * factors["growth_tilt"]
        + 0.15 * factors["gold"]
    ).fillna(0.0)
    portfolio = pd.DataFrame(
        {"date": index, "daily_return": portfolio_return.to_numpy()}
    )
    return portfolio, prices


def test_factor_monitor_is_read_only_and_reconciles_returns():
    portfolio, prices = _monitor_inputs()
    original = portfolio.copy(deep=True)

    result = build_factor_monitor(portfolio, prices)

    pd.testing.assert_frame_equal(portfolio, original)
    assert result.affects_weights is False
    assert result.rolling_summary["maximum_reconciliation_error"] < 1e-15
    assert result.rolling_summary["oos_r_squared"] > 0.95
    assert set(result.exposure_table.index) == set(FACTOR_LABELS)
    assert result.status in {"normal", "watch"}


def test_factor_monitor_rejects_portfolio_without_returns():
    _, prices = _monitor_inputs()

    try:
        build_factor_monitor(pd.DataFrame({"date": prices.index}), prices)
    except ValueError as exc:
        assert "daily_return" in str(exc)
    else:
        raise AssertionError("Expected missing daily_return to fail.")
