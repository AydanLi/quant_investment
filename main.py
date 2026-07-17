from __future__ import annotations

from backtest.engine import Backtester
from config.settings import Config
from data.features import FeatureEngineer
from data.trusted_loader import TrustedMarketDataLoader
from data.providers import FredRiskFreeProvider, ProviderError
from report.benchmarks import build_benchmark_returns
from report.reporter import ReportGenerator
from risk.engine import RiskEngine
from services.signal_service import SignalService
from strategy.momentum_rotation import MomentumRotationStrategy
from strategy.regime import RegimeDetector


def run_backtest() -> None:
    config = Config()

    print("Loading data...")
    loader = TrustedMarketDataLoader(config)
    data = loader.load()

    print("Building features...")
    fe = FeatureEngineer(data, config)
    prices = fe.make_price_frame()
    execution_prices = fe.make_open_frame().reindex(prices.index)
    median_dollar_volume = fe.make_median_dollar_volume_frame().reindex(prices.index)
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
        execution_prices=execution_prices,
        median_dollar_volume=median_dollar_volume,
    )
    results = bt.run()
    portfolio = results["portfolio"]
    orders = results["orders"]

    reporter = ReportGenerator(config)
    try:
        risk_free = FredRiskFreeProvider().fetch_daily_returns(
            config.start_date, config.end_date
        )
    except ProviderError:
        risk_free = None
    summary = reporter.summarize(
        portfolio,
        risk_free_returns=risk_free,
        benchmark_returns=build_benchmark_returns(prices),
        orders=orders,
        asset_returns=returns,
    )

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
