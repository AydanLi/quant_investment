from __future__ import annotations

from backtest.engine import Backtester
from config.settings import Config
from data.features import FeatureEngineer
from data.loader import MarketDataLoader
from report.reporter import ReportGenerator
from risk.engine import RiskEngine
from services.signal_service import SignalService
from strategy.momentum_rotation import MomentumRotationStrategy
from strategy.regime import RegimeDetector


def run_backtest() -> None:
    config = Config(
        start_date="2018-01-01",
        end_date=None,
        rebalance_frequency="M",
        top_n=3,
        min_momentum_threshold=0.0,
        target_annual_vol=0.12,
        max_asset_weight=0.40,
        risk_off_cash_weight=0.50,
        trading_cost_bps=5.0,
    )

    print("Loading data...")
    loader = MarketDataLoader(config)
    data = loader.load()

    print("Building features...")
    fe = FeatureEngineer(data, config)
    prices = fe.make_price_frame()
    returns = fe.make_returns_frame(prices)
    features = fe.compute_features(prices, returns)

    print("Running backtest...")
    regime_detector = RegimeDetector(config)
    strategy = MomentumRotationStrategy(config)
    risk_engine = RiskEngine(config)

    bt = Backtester(
        config=config,
        prices=prices,
        returns=returns,
        features=features,
        regime_detector=regime_detector,
        strategy=strategy,
        risk_engine=risk_engine,
    )
    results = bt.run()
    portfolio = results["portfolio"]
    orders = results["orders"]

    reporter = ReportGenerator(config)
    summary = reporter.summarize(portfolio)

    print("\n================ Backtest Summary ================")
    for key, value in summary.items():
        if isinstance(value, float):
            if "Equity" in key:
                print(f"{key:<16}: ${value:,.2f}")
            elif key in {"Sharpe", "Sortino", "Avg Turnover"}:
                print(f"{key:<16}: {value:.4f}")
            else:
                print(f"{key:<16}: {value:.2%}")
        else:
            print(f"{key:<16}: {value}")
    print("==================================================")

    reporter.print_latest_signal(portfolio)

    if not orders.empty:
        print("Recent orders:")
        print(orders.tail(10).to_string(index=False))

    reporter.plot(portfolio, prices[config.benchmark])


def run_signal_only() -> None:
    config = Config()
    service = SignalService(config)
    signal = service.generate_latest_allocation()
    print("Latest signal:")
    print(signal)


if __name__ == "__main__":
    run_backtest()
    # run_signal_only()