import numpy as np
import pandas as pd

from research.model_admission import compare_period, evaluate_admission


def _portfolio(returns, index, risk_scale=1.0):
    returns = pd.Series(returns, index=index, dtype=float)
    turnover = pd.Series(0.0, index=index)
    turnover.loc[index.to_series().groupby(index.to_period("M")).tail(1).index] = risk_scale
    trading = turnover * 0.0005
    slippage = turnover * 0.0002
    net = returns - trading - slippage
    return pd.DataFrame(
        {
            "daily_return": net,
            "turnover": turnover,
            "est_trading_cost": trading,
            "est_slippage": slippage,
            "est_cost": trading + slippage,
            "regime": np.resize(
                ["bull_trend", "neutral", "bear_high_vol", "risk_off"],
                len(index),
            ),
        },
        index=index,
    )


def test_compare_period_rejects_mismatched_dates():
    index = pd.bdate_range("2022-01-03", periods=10)
    baseline = _portfolio(np.zeros(10), index)
    candidate = baseline.iloc[1:]

    try:
        compare_period(baseline, candidate, index[0])
    except ValueError as exc:
        assert "same dates" in str(exc)
    else:
        raise AssertionError("Expected mismatched comparison dates to fail.")


def test_admission_requires_every_gate_and_accepts_robust_candidate():
    index = pd.bdate_range("2022-01-03", "2024-12-31")
    cycle = np.resize([0.006, -0.005, 0.003, -0.004, 0.004], len(index))
    baseline = _portfolio(cycle, index, risk_scale=1.0)
    candidate = _portfolio(cycle * 0.65 + 0.00045, index, risk_scale=0.8)
    signal_index = pd.date_range("2022-01-31", periods=36, freq="ME")
    momentum = pd.Series(np.sin(np.arange(36)), index=signal_index)
    model = pd.Series(np.cos(np.arange(36) * 0.5), index=signal_index)

    result = evaluate_admission(
        baseline=baseline,
        candidate=candidate,
        parameter_candidates={"lower": candidate, "upper": candidate},
        momentum_signal=momentum,
        model_signal=model,
        oos_start=pd.Timestamp("2022-01-01"),
        first_test_year=2022,
        start_dates=[pd.Timestamp("2022-01-01"), pd.Timestamp("2023-01-01")],
        crisis_periods={
            "synthetic": (pd.Timestamp("2022-01-03"), pd.Timestamp("2022-06-30"))
        },
    )

    assert result["admitted"] is True
    assert all(result["gates"].values())
    assert result["window_win_rate"] == 1.0
