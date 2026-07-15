import numpy as np
import pandas as pd

from services.monte_carlo_monitor import build_monte_carlo_monitor


def _portfolio(seed=41, periods=600, mean_return=0.0004):
    generator = np.random.default_rng(seed)
    index = pd.bdate_range("2022-01-03", periods=periods)
    turnover = np.zeros(periods)
    turnover[::20] = 0.30
    cost = turnover * 0.0007
    returns = generator.normal(mean_return, 0.008, periods) - cost
    return pd.DataFrame(
        {
            "date": index,
            "daily_return": returns,
            "turnover": turnover,
            "est_cost": cost,
        }
    )


def test_monte_carlo_monitor_is_read_only_and_reproducible():
    portfolio = _portfolio()
    original = portfolio.copy(deep=True)

    first = build_monte_carlo_monitor(
        portfolio,
        simulations=300,
        horizon=126,
        sensitivity_simulations=100,
    )
    second = build_monte_carlo_monitor(
        portfolio,
        simulations=300,
        horizon=126,
        sensitivity_simulations=100,
    )

    pd.testing.assert_frame_equal(portfolio, original)
    pd.testing.assert_frame_equal(first.equity_quantiles, second.equity_quantiles)
    assert first.probability_of_loss == second.probability_of_loss
    assert first.affects_weights is False
    assert len(first.equity_quantiles) == 127
    assert set(first.distribution_table["指标"]) == {
        "总收益",
        "最大回撤",
        "Sharpe",
        "换手",
        "估算成本",
    }


def test_monte_carlo_monitor_warns_on_loss_dominated_history():
    portfolio = _portfolio(mean_return=-0.0015)

    result = build_monte_carlo_monitor(
        portfolio,
        simulations=300,
        horizon=126,
        sensitivity_simulations=100,
    )

    assert result.status == "watch"
    assert result.probability_of_loss >= 0.25
    assert any("亏损概率" in warning for warning in result.warnings)


def test_monte_carlo_monitor_rejects_invalid_inputs():
    portfolio = _portfolio()

    try:
        build_monte_carlo_monitor(portfolio.drop(columns="daily_return"))
    except ValueError as exc:
        assert "daily_return" in str(exc)
    else:
        raise AssertionError("Expected missing returns to fail.")

    invalid_cost = portfolio.copy()
    invalid_cost.loc[0, "est_cost"] = -0.01
    try:
        build_monte_carlo_monitor(invalid_cost)
    except ValueError as exc:
        assert "negative est_cost" in str(exc)
    else:
        raise AssertionError("Expected negative costs to fail.")
