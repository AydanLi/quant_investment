import numpy as np
import pandas as pd

from research.monte_carlo import (
    circular_block_indices,
    paired_block_bootstrap,
)


def _portfolio(returns, index):
    returns = np.asarray(returns, dtype=float)
    turnover = np.zeros(len(index))
    turnover[::20] = 0.25
    trading_cost = turnover * 0.0005
    slippage = turnover * 0.0002
    total_cost = trading_cost + slippage
    gross_return = (1.0 + returns) / (1.0 - total_cost) - 1.0
    return pd.DataFrame(
        {
            "gross_return": gross_return,
            "daily_return": returns,
            "turnover": turnover,
            "est_trading_cost": trading_cost,
            "est_slippage": slippage,
            "est_cost": total_cost,
        },
        index=index,
    )


def test_circular_block_indices_are_reproducible_and_contiguous():
    first = circular_block_indices(
        11, simulations=4, horizon=17, block_length=5, seed=7
    )
    second = circular_block_indices(
        11, simulations=4, horizon=17, block_length=5, seed=7
    )

    np.testing.assert_array_equal(first, second)
    for position in range(1, first.shape[1]):
        if position % 5:
            np.testing.assert_array_equal(
                first[:, position], (first[:, position - 1] + 1) % 11
            )


def test_paired_bootstrap_uses_same_paths_and_preserves_cost_audit():
    generator = np.random.default_rng(31)
    index = pd.bdate_range("2022-01-03", periods=500)
    baseline_returns = generator.normal(0.00025, 0.012, len(index))
    baseline_returns[100:110] -= 0.025
    candidate_returns = 0.65 * baseline_returns + 0.00025
    baseline = _portfolio(baseline_returns, index)
    candidate = _portfolio(candidate_returns, index)

    result = paired_block_bootstrap(
        baseline,
        candidate,
        simulations=500,
        block_length=20,
        seed=101,
    )
    paired = result.summary["paired"]

    assert paired["probability_sharpe_improvement"] > 0.95
    assert paired["probability_drawdown_reduction"] > 0.95
    assert result.summary["source_totals"]["candidate"]["est_cost"] > 0.0
    assert result.horizon == len(index)


def test_identical_portfolios_have_zero_paired_improvement():
    generator = np.random.default_rng(5)
    index = pd.bdate_range("2023-01-02", periods=260)
    portfolio = _portfolio(
        generator.normal(0.0003, 0.008, len(index)), index
    )

    result = paired_block_bootstrap(
        portfolio,
        portfolio.copy(),
        simulations=100,
        block_length=10,
        seed=9,
    )

    assert result.path_metrics["sharpe_improvement"].abs().max() == 0.0
    assert result.path_metrics["drawdown_reduction"].abs().max() == 0.0
    assert result.path_metrics["return_improvement"].abs().max() == 0.0


def test_paired_bootstrap_rejects_mismatched_dates_and_missing_costs():
    index = pd.bdate_range("2024-01-02", periods=40)
    baseline = _portfolio(np.full(len(index), 0.0001), index)

    try:
        paired_block_bootstrap(baseline, baseline.iloc[1:])
    except ValueError as exc:
        assert "same dates" in str(exc)
    else:
        raise AssertionError("Expected mismatched dates to fail.")

    try:
        paired_block_bootstrap(
            baseline.drop(columns="est_slippage"), baseline
        )
    except ValueError as exc:
        assert "est_slippage" in str(exc)
    else:
        raise AssertionError("Expected missing slippage to fail.")

    not_net = baseline.copy()
    not_net.loc[index[0], "daily_return"] += 0.001
    try:
        paired_block_bootstrap(not_net, baseline)
    except ValueError as exc:
        assert "not net" in str(exc)
    else:
        raise AssertionError("Expected a return that excludes costs to fail.")
