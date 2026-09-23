import pandas as pd
import pytest

from backtest.engine import Backtester
from config.settings import Config
from data.calendar import NyseCalendar
from risk.controls import PortfolioRiskMonitor, RiskStatus


def test_drawdown_halt_triggers_once_and_requires_manual_monthly_reentry():
    monitor = PortfolioRiskMonitor(Config(), 10_000.0)

    status, drawdown, events = monitor.evaluate(
        nav=8_400.0, daily_return=-0.02, weights={"SPY": 0.3, "BIL": 0.7}
    )
    repeated = monitor.evaluate(
        nav=8_300.0, daily_return=-0.01, weights={"BIL": 1.0}
    )

    assert status == RiskStatus.DRAWDOWN_HALTED
    assert abs(drawdown + 0.16) < 1e-12
    assert [event.code for event in events] == ["PORTFOLIO_DRAWDOWN_STOP"]
    assert repeated[0] == RiskStatus.DRAWDOWN_HALTED
    assert repeated[2] == ()
    with pytest.raises(ValueError, match="next monthly"):
        monitor.authorize_reentry(
            session="2026-07-20",
            next_monthly_rebalance_session="2026-07-31",
            nav=8_300.0,
            reconciliation_ok=True,
            incident_recorded=True,
            authorized_by="operator",
        )
    monitor.authorize_reentry(
        session="2026-07-31",
        next_monthly_rebalance_session="2026-07-31",
        nav=8_300.0,
        reconciliation_ok=True,
        incident_recorded=True,
        authorized_by="operator",
    )
    assert monitor.drawdown_halted is False
    assert monitor.high_water == 8_300.0


def test_daily_loss_and_drift_warning_review_thresholds():
    monitor = PortfolioRiskMonitor(Config(), 10_000.0)
    status, _, events = monitor.evaluate(
        nav=9_490.0, daily_return=-0.051, weights={"SPY": 0.36, "BIL": 0.64}
    )
    assert status == RiskStatus.DAILY_LOSS_HALT
    assert {event.code for event in events} == {"DAILY_LOSS_HALT", "POSITION_DRIFT_WARNING"}

    monitor = PortfolioRiskMonitor(Config(), 10_000.0)
    status, _, events = monitor.evaluate(
        nav=10_000.0, daily_return=0.0, weights={"SPY": 0.41, "BIL": 0.59}
    )
    assert status == RiskStatus.DRIFT_REVIEW
    assert [event.code for event in events] == ["POSITION_DRIFT_REVIEW"]


class _Neutral:
    def classify(self, date, prices, features):
        return "neutral"


class _AlwaysSpy:
    def target_weights(self, date, regime, prices, features):
        return {"SPY": 1.0}


class _PassThroughRisk:
    def scale_to_target_vol(self, date, weights, returns):
        return weights

    def enforce_weight_limits(self, weights):
        return weights

    def pre_trade_check(self, weights):
        return True, "OK"


def _run_monthly(prices, opens, *, mode="next_month_end"):
    config = Config(
        universe=["SPY", "BIL"],
        rebalance_frequency="M",
        max_asset_weight=1.0,
        min_asset_weight=0.0,
        trading_cost_bps=0.0,
        slippage_bps=0.0,
    )
    return Backtester(
        config=config,
        prices=prices,
        raw_close_prices=prices,
        execution_prices=opens,
        returns=prices.pct_change(fill_method=None),
        features={},
        regime_detector=_Neutral(),
        strategy=_AlwaysSpy(),
        risk_engine=_PassThroughRisk(),
        drawdown_reentry_mode=mode,
    ).run()


def test_drawdown_liquidates_t_plus_one_and_reenters_next_month_end():
    calendar = NyseCalendar()
    index = calendar.sessions("2022-01-03", "2024-12-31")
    month_ends = calendar.rebalance_sessions(index, "M")
    first_signal = month_ends[month_ends >= index[252]][0]
    first_execution = index[index.get_loc(first_signal) + 1]
    stop_session = index[index.get_loc(first_execution) + 5]
    prices = pd.DataFrame({"SPY": 100.0, "BIL": 100.0}, index=index)
    prices.loc[stop_session:, "SPY"] = 80.0
    opens = prices.copy()

    result = _run_monthly(prices, opens)
    portfolio = result["portfolio"]
    orders = result["orders"]
    reentry_session = calendar.next_month_end_session(stop_session)

    assert portfolio.at[stop_session, "stop_triggered"]
    assert (
        orders.loc[
            (orders["ticker"] == "SPY") & (orders["side"] == "SELL"), "date"
        ].min()
        == index[index.get_loc(stop_session) + 1]
    )
    assert portfolio.at[reentry_session, "research_reentry"]
    assert portfolio.at[reentry_session, "high_water"] == pytest.approx(
        portfolio.at[reentry_session, "equity"]
    )
    assert result["signals"].set_index("signal_date").at[
        reentry_session, "research_reentry"
    ]
    assert (
        orders.loc[
            (orders["ticker"] == "SPY")
            & (orders["side"] == "BUY")
            & (orders["date"] > reentry_session)
        ].shape[0]
        == 1
    )


def test_permanent_cash_requires_explicit_stress_mode():
    calendar = NyseCalendar()
    index = calendar.sessions("2022-01-03", "2024-12-31")
    first_signal = calendar.rebalance_sessions(index, "M")
    first_signal = first_signal[first_signal >= index[252]][0]
    first_execution = index[index.get_loc(first_signal) + 1]
    stop_session = index[index.get_loc(first_execution) + 5]
    prices = pd.DataFrame({"SPY": 100.0, "BIL": 100.0}, index=index)
    prices.loc[stop_session:, "SPY"] = 80.0

    result = _run_monthly(
        prices,
        prices.copy(),
        mode="permanent_cash_stress",
    )
    sell_date = result["orders"].loc[
        (result["orders"]["ticker"] == "SPY")
        & (result["orders"]["side"] == "SELL"),
        "date",
    ].min()

    assert not result["portfolio"]["research_reentry"].any()
    assert result["portfolio"]["risk_status"].iloc[-1] == RiskStatus.DRAWDOWN_HALTED
    assert result["orders"].loc[
        (result["orders"]["ticker"] == "SPY")
        & (result["orders"]["side"] == "BUY")
        & (result["orders"]["date"] > sell_date)
    ].empty


def test_daily_loss_on_month_end_does_not_discard_monthly_signal():
    calendar = NyseCalendar()
    index = calendar.sessions("2022-01-03", "2024-12-31")
    month_ends = calendar.rebalance_sessions(index, "M")
    first_signal = month_ends[month_ends >= index[252]][0]
    loss_session = month_ends[month_ends > first_signal][0]
    prices = pd.DataFrame({"SPY": 100.0, "BIL": 100.0}, index=index)
    prices.loc[loss_session:, "SPY"] = 94.5

    result = _run_monthly(prices, prices.copy())

    assert result["portfolio"].at[loss_session, "risk_status"] == RiskStatus.DAILY_LOSS_HALT
    assert loss_session in set(result["signals"]["signal_date"])
