from __future__ import annotations

from config.settings import Config
from data.features import FeatureEngineer
from data.loader import MarketDataLoader
from risk.engine import RiskEngine
from strategy.momentum_rotation import MomentumRotationStrategy
from strategy.regime import RegimeDetector


class SignalService:
    def __init__(self, config: Config):
        self.config = config

    def generate_latest_allocation(self) -> dict:
        loader = MarketDataLoader(self.config)
        data = loader.load()

        fe = FeatureEngineer(data, self.config)
        prices = fe.make_price_frame()
        returns = fe.make_returns_frame(prices)
        features = fe.compute_features(prices, returns)

        date = prices.index[-1]
        regime_detector = RegimeDetector(self.config)
        strategy = MomentumRotationStrategy(self.config)
        risk_engine = RiskEngine(self.config)

        regime = regime_detector.classify(date, prices, features)
        target = strategy.target_weights(date, regime, prices, features)
        target = risk_engine.scale_to_target_vol(date, target, returns)
        target = risk_engine.enforce_weight_limits(target)

        ok, reason = risk_engine.pre_trade_check(target)
        if not ok:
            raise ValueError(f"Latest signal failed pre-trade check: {reason}")

        return {
            "date": str(date.date()),
            "regime": regime,
            "weights": target,
        }