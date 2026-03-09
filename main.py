from __future__ import annotations

from backtest.engine import Backtester
from config.settings import Config
from data.features import FeatureEngineer
from data.loader import MarketDataLoader
from report.reporter import ReportGenerator
from risk.engine import RiskEngine
from strategy.momentum_rotation import MomentumRotationStrategy
from strategy.regime import RegimeDetector


def main() -> None:
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
    for k, v in summary.items():
        if isinstance(v, float):
            if "Equity" in k:
                print(f"{k:<16}: ${v:,.2f}")
            elif k in {"Sharpe", "Avg Turnover"}:
                print(f"{k:<16}: {v:.4f}")
            else:
                print(f"{k:<16}: {v:.2%}")
        else:
            print(f"{k:<16}: {v}")
    print("==================================================")

    reporter.print_latest_signal(portfolio)

    if not orders.empty:
        print("Recent orders:")
        print(orders.tail(10).to_string(index=False))

    reporter.plot(portfolio, prices[config.benchmark])


if __name__ == "__main__":
    main()