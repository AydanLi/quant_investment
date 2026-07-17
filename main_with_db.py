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
from storage.store import ResearchStore
from strategy.momentum_rotation import MomentumRotationStrategy
from strategy.regime import RegimeDetector


def run_backtest_and_save() -> None:
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

    # Reuse the exact loaded snapshot. A second provider fetch could cross a
    # revision boundary or consume quota and would break run reproducibility.
    signal_service = SignalService(config, loader=loader)
    latest_signal = signal_service.generate_latest_allocation()

    print("Saving to SQLite...")
    store = ResearchStore()
    store.init_db()

    run_id = store.save_experiment_run(
        scenario_name="baseline_monthly_top3",
        config=config,
        summary=summary,
        latest_signal=latest_signal,
        dataset_snapshot_id=loader.dataset_snapshot_id,
        universe_version=config.universe_version,
        strategy_version=config.strategy_version,
    )
    store.save_portfolio_daily(run_id, portfolio)
    store.save_orders(run_id, orders)
    store.save_signals(run_id, latest_signal)

    print(f"Saved run_id={run_id} to quant_research.db")

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

    store.close()


if __name__ == "__main__":
    run_backtest_and_save()
