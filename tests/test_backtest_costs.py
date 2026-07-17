import pandas as pd
import pytest

from backtest.engine import Backtester
from backtest.ledger import PortfolioLedger
from config.settings import Config
from data.calendar import NyseCalendar
from report.reporter import ReportGenerator
from utils.metrics import max_drawdown


class _NeutralRegimeDetector:
    def classify(self, date, prices, features):
        return "neutral"


class _AlternatingStrategy:
    def __init__(self, first_date):
        self.first_date = first_date

    def target_weights(self, date, regime, prices, features):
        return {"SPY": 1.0} if date == self.first_date else {"QQQ": 1.0}


class _PassThroughRiskEngine:
    def scale_to_target_vol(self, date, weights, returns):
        return weights

    def enforce_weight_limits(self, weights):
        return weights

    def pre_trade_check(self, weights):
        return True, "OK"


def _run_cost_backtest():
    calendar = NyseCalendar()
    index = calendar.sessions("2023-01-03", "2024-03-31")[:255]
    close_prices = pd.DataFrame(
        {"SPY": 100.0, "QQQ": 200.0, "BIL": 91.0}, index=index
    )
    open_prices = close_prices.copy()
    # SPY earns a close-to-open-session return only after the T+1 purchase.
    close_prices.loc[index[253], "SPY"] = 101.0
    returns = close_prices.pct_change(fill_method=None)

    config = Config(
        universe=["SPY", "QQQ", "BIL"],
        rebalance_frequency="D",
        top_n=1,
        max_asset_weight=1.0,
        min_asset_weight=0.0,
        trading_cost_bps=10.0,
        slippage_bps=5.0,
        initial_capital=1000.0,
    )
    backtester = Backtester(
        config=config,
        prices=close_prices,
        execution_prices=open_prices,
        returns=returns,
        features={},
        regime_detector=_NeutralRegimeDetector(),
        strategy=_AlternatingStrategy(index[252]),
        risk_engine=_PassThroughRiskEngine(),
    )
    return config, backtester.run(), index


def test_backtest_daily_return_matches_equity_after_costs_and_t_plus_one():
    config, results, index = _run_cost_backtest()
    portfolio = results["portfolio"]
    orders = results["orders"]
    signals = results["signals"]

    assert portfolio.index[0] == index[252]
    assert portfolio.iloc[0]["turnover"] == 0.0
    assert signals.iloc[0]["signal_date"] == index[252]
    assert signals.iloc[0]["intended_execution_date"] == index[253]
    assert orders.iloc[0]["date"] == index[253]
    assert (orders["date"] > orders["signal_date"]).all()

    previous_equity = config.initial_capital
    for _, row in portfolio.iterrows():
        realized_return = row["equity"] / previous_equity - 1.0
        assert abs(realized_return - row["daily_return"]) < 1e-12
        expected_gross = (row["equity"] + row["cost_dollars"]) / previous_equity - 1.0
        assert abs(expected_gross - row["gross_return"]) < 1e-12
        assert row["cash"] >= -1e-9
        assert row["settled_cash"] >= -1e-9
        previous_equity = row["equity"]


def test_broker_orders_capture_open_prices_single_sided_costs_and_settlement():
    _, results, index = _run_cost_backtest()
    orders = results["orders"]
    portfolio = results["portfolio"]

    assert len(orders) == 4
    assert orders["price"].notna().all()
    assert orders.loc[orders["ticker"] == "SPY", "price"].eq(100.0).all()
    assert abs(orders["trading_cost_dollars"].sum() - (orders["notional"].sum() * 0.001)) < 1e-9
    assert abs(orders["slippage_dollars"].sum() - (orders["notional"].sum() * 0.0005)) < 1e-9
    for date, group in orders.groupby("date"):
        assert abs(group["trading_cost_dollars"].sum() + group["slippage_dollars"].sum() - portfolio.at[date, "cost_dollars"]) < 1e-9
    assert portfolio.at[index[253], "unsettled_cash"] > 0.0


def test_report_uses_initial_capital_net_equity_and_provisional_label():
    config, results, _ = _run_cost_backtest()
    portfolio = results["portfolio"]
    summary = ReportGenerator(config).summarize(portfolio, orders=results["orders"])

    assert summary["Start Equity"] == config.initial_capital
    assert abs(summary["Total Return"] - (portfolio["equity"].iloc[-1] / config.initial_capital - 1.0)) < 1e-12
    initial = pd.Series(
        [config.initial_capital],
        index=[portfolio.index[0] - pd.Timedelta(days=1)],
    )
    expected_drawdown = max_drawdown(pd.concat([initial, portfolio["equity"]]))
    assert abs(summary["Max Drawdown"] - expected_drawdown) < 1e-12
    assert summary["Metric Status"] == "PROVISIONAL"


def _cash_ledger(config: Config) -> tuple[PortfolioLedger, pd.Series]:
    prices = pd.Series({"SPY": 100.0})
    ledger = PortfolioLedger.initialize(
        config,
        session=pd.Timestamp("2024-01-02"),
        prices=prices,
    )
    return ledger, prices


def test_square_root_impact_starts_at_point_one_percent_adv():
    config = Config(
        universe=["SPY"],
        initial_capital=1000.0,
        trading_cost_bps=5.0,
        slippage_bps=2.0,
    )
    ledger, prices = _cash_ledger(config)

    execution = ledger.rebalance(
        signal_date=pd.Timestamp("2024-01-02"),
        execution_date=pd.Timestamp("2024-01-03"),
        target_weights={"SPY": 1.0},
        prices=prices,
        median_dollar_volume=pd.Series({"SPY": 900_000.0}),
    )

    order = ledger.order_log[0]
    assert order["adv_fraction"] >= config.impact_model_adv_threshold
    expected_bps = config.impact_coefficient_bps * (
        order["adv_fraction"] / config.impact_model_adv_threshold
    ) ** 0.5
    assert order["impact_cost_dollars"] == pytest.approx(
        order["notional"] * expected_bps / 10_000.0
    )
    assert execution["est_impact"] > 0.0


def test_historical_order_above_one_percent_adv_is_blocked():
    config = Config(universe=["SPY"], initial_capital=1000.0)
    ledger, prices = _cash_ledger(config)

    with pytest.raises(ValueError, match="exceeds 1% ADV"):
        ledger.rebalance(
            signal_date=pd.Timestamp("2024-01-02"),
            execution_date=pd.Timestamp("2024-01-03"),
            target_weights={"SPY": 1.0},
            prices=prices,
            median_dollar_volume=pd.Series({"SPY": 50_000.0}),
        )


def test_risk_off_execution_has_twenty_bp_cost_floor():
    config = Config(
        universe=["SPY"],
        initial_capital=1000.0,
        trading_cost_bps=5.0,
        slippage_bps=2.0,
    )
    ledger, prices = _cash_ledger(config)

    ledger.rebalance(
        signal_date=pd.Timestamp("2024-01-02"),
        execution_date=pd.Timestamp("2024-01-03"),
        target_weights={"SPY": 1.0},
        prices=prices,
        risk_off=True,
    )

    order = ledger.order_log[0]
    execution_cost = (
        order["trading_cost_dollars"] + order["slippage_dollars"]
    ) / order["notional"]
    assert execution_cost == pytest.approx(20.0 / 10_000.0)


def test_average_cost_basis_produces_net_realized_trade_pnl():
    config = Config(
        universe=["SPY"],
        initial_capital=1000.0,
        trading_cost_bps=5.0,
        slippage_bps=2.0,
    )
    ledger, prices = _cash_ledger(config)
    ledger.rebalance(
        signal_date=pd.Timestamp("2024-01-02"),
        execution_date=pd.Timestamp("2024-01-03"),
        target_weights={"SPY": 1.0},
        prices=prices,
    )
    ledger.settle(pd.Timestamp("2024-01-04"))
    ledger.rebalance(
        signal_date=pd.Timestamp("2024-01-04"),
        execution_date=pd.Timestamp("2024-01-05"),
        target_weights={config.synthetic_cash_asset: 1.0},
        prices=pd.Series({"SPY": 110.0}),
    )

    buy, sell = ledger.order_log
    assert buy["average_entry_cost"] > buy["price"]
    assert buy["realized_pnl"] is None
    expected_gross = (
        sell["price"] - buy["average_entry_cost"]
    ) * sell["quantity"]
    exit_cost = (
        sell["trading_cost_dollars"]
        + sell["slippage_dollars"]
        + sell["impact_cost_dollars"]
    )
    assert sell["gross_realized_pnl"] == pytest.approx(expected_gross)
    assert sell["realized_pnl"] == pytest.approx(expected_gross - exit_cost)
    assert sell["realized_pnl"] > 0.0
