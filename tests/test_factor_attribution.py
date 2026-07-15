import numpy as np
import pandas as pd

from research.factor_attribution import (
    build_proxy_factor_returns,
    fit_factor_regression,
    rolling_attribution_summary,
    rolling_factor_attribution,
)


def _synthetic_regression_data(seed=11, periods=420):
    generator = np.random.default_rng(seed)
    index = pd.bdate_range("2021-01-04", periods=periods)
    factors = pd.DataFrame(
        generator.normal(0.0, 0.01, size=(periods, 3)),
        index=index,
        columns=["market", "duration", "gold"],
    )
    cash = pd.Series(0.00005, index=index, name="cash")
    noise = generator.normal(0.0, 0.001, periods)
    portfolio = (
        cash
        + 0.0002
        + 0.65 * factors["market"]
        - 0.20 * factors["duration"]
        + 0.30 * factors["gold"]
        + noise
    )
    return portfolio, factors, cash


def test_factor_regression_recovers_known_exposures():
    portfolio, factors, cash = _synthetic_regression_data()

    result = fit_factor_regression(
        portfolio, factors, cash_returns=cash
    )

    assert abs(result.coefficients["market"] - 0.65) < 0.02
    assert abs(result.coefficients["duration"] + 0.20) < 0.02
    assert abs(result.coefficients["gold"] - 0.30) < 0.02
    assert abs(result.coefficients["alpha"] - 0.0002) < 0.0001
    assert result.r_squared > 0.95
    assert result.condition_number < 2.0
    assert abs(result.variance_contribution.sum() - 1.0) < 1e-12


def test_rolling_attribution_reconciles_actual_returns():
    portfolio, factors, cash = _synthetic_regression_data()
    attribution = rolling_factor_attribution(
        portfolio,
        factors,
        cash_returns=cash,
        window=252,
        minimum_observations=126,
    )
    summary = rolling_attribution_summary(attribution)

    assert len(attribution.contributions) == len(portfolio) - 126
    assert summary["maximum_reconciliation_error"] < 1e-15
    assert summary["oos_r_squared"] > 0.95


def test_rolling_exposure_does_not_use_current_or_future_values():
    portfolio, factors, cash = _synthetic_regression_data()
    comparison_date = factors.index[250]
    changed_factors = factors.copy()
    changed_factors.loc[comparison_date:] *= 50.0

    original = rolling_factor_attribution(
        portfolio,
        factors,
        cash_returns=cash,
        window=200,
        minimum_observations=126,
    )
    changed = rolling_factor_attribution(
        portfolio,
        changed_factors,
        cash_returns=cash,
        window=200,
        minimum_observations=126,
    )

    assert np.allclose(
        original.exposures.loc[comparison_date],
        changed.exposures.loc[comparison_date],
    )


def test_proxy_factors_use_declared_etf_spreads():
    index = pd.bdate_range("2024-01-02", periods=3)
    prices = pd.DataFrame(
        {
            "SPY": [100.0, 101.0, 102.0],
            "QQQ": [100.0, 102.0, 104.0],
            "IWM": [100.0, 100.5, 101.0],
            "TLT": [100.0, 99.0, 100.0],
            "GLD": [100.0, 101.5, 101.0],
            "XLE": [100.0, 103.0, 102.0],
            "XLV": [100.0, 100.8, 101.6],
            "BIL": [100.0, 100.01, 100.02],
        },
        index=index,
    )

    factors, cash = build_proxy_factor_returns(prices)

    assert abs(
        factors.loc[index[1], "equity_market"]
        - (prices["SPY"].pct_change().loc[index[1]] - cash.loc[index[1]])
    ) < 1e-12
    assert abs(
        factors.loc[index[1], "growth_tilt"]
        - (
            prices["QQQ"].pct_change().loc[index[1]]
            - prices["SPY"].pct_change().loc[index[1]]
        )
    ) < 1e-12
