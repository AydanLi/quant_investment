import pandas as pd

from backtest.engine import Backtester
from config.settings import Config
from report.reporter import ReportGenerator


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
    index = pd.bdate_range("2024-01-01", periods=222)
    prices = pd.DataFrame(
        {"SPY": 100.0, "QQQ": 200.0, "BIL": 91.0},
        index=index,
    )
    returns = pd.DataFrame(0.0, index=index, columns=prices.columns)
    returns.loc[index[221], "SPY"] = 0.01

    config = Config(
        universe=["SPY", "QQQ", "BIL"],
        rebalance_frequency="D",
        top_n=1,
        max_asset_weight=1.0,
        trading_cost_bps=10.0,
        initial_capital=1000.0,
    )
    backtester = Backtester(
        config=config,
        prices=prices,
        returns=returns,
        features={},
        regime_detector=_NeutralRegimeDetector(),
        strategy=_AlternatingStrategy(index[220]),
        risk_engine=_PassThroughRiskEngine(),
    )
    return config, backtester.run()


def test_backtest_daily_return_matches_equity_after_costs():
    config, results = _run_cost_backtest()
    portfolio = results["portfolio"]

    first = portfolio.iloc[0]
    second = portfolio.iloc[1]

    assert abs(first["gross_return"]) < 1e-12
    assert abs(first["est_cost"] - 0.002) < 1e-12
    assert abs(first["daily_return"] + 0.002) < 1e-12
    assert abs(first["equity"] - 998.0) < 1e-9

    expected_second_return = (1.0 + 0.01) * (1.0 - 0.002) - 1.0
    assert abs(second["gross_return"] - 0.01) < 1e-12
    assert abs(second["est_cost"] - 0.002) < 1e-12
    assert abs(second["daily_return"] - expected_second_return) < 1e-12

    previous_equity = config.initial_capital
    for _, row in portfolio.iterrows():
        realized_return = row["equity"] / previous_equity - 1.0
        assert abs(realized_return - row["daily_return"]) < 1e-12
        previous_equity = row["equity"]


def test_broker_orders_capture_prices_and_costs():
    _, results = _run_cost_backtest()
    orders = results["orders"]
    portfolio = results["portfolio"]

    assert len(orders) == 4
    assert orders["price"].notna().all()
    assert abs(orders["est_cost"].sum() - 0.004) < 1e-12
    order_costs = orders.groupby("date")["est_cost"].sum()
    for date, cost in order_costs.items():
        assert abs(cost - portfolio.at[date, "est_cost"]) < 1e-12


def test_report_uses_initial_capital_and_net_equity_curve():
    config, results = _run_cost_backtest()
    portfolio = results["portfolio"]

    summary = ReportGenerator(config).summarize(portfolio)

    assert summary["Start Equity"] == config.initial_capital
    expected_total_return = portfolio["equity"].iloc[-1] / config.initial_capital - 1.0
    assert abs(summary["Total Return"] - expected_total_return) < 1e-12
    assert abs(summary["Max Drawdown"] + 0.002) < 1e-12
