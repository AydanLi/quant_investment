from __future__ import annotations

from typing import Dict

import pandas as pd

from config.settings import Config
from execution.broker import MockBroker


class Backtester:
    def __init__(
        self,
        config: Config,
        prices: pd.DataFrame,
        returns: pd.DataFrame,
        features: dict,
        regime_detector,
        strategy,
        risk_engine,
    ):
        self.config = config
        self.prices = prices.copy()
        self.returns = returns.copy()
        self.features = features
        self.regime_detector = regime_detector
        self.strategy = strategy
        self.risk_engine = risk_engine
        self.broker = MockBroker()

    def _get_rebalance_dates(self) -> pd.DatetimeIndex:
        idx = self.prices.index
        if self.config.rebalance_frequency == "D":
            return idx
        if self.config.rebalance_frequency == "W":
            return idx.to_series().groupby(idx.to_period("W")).tail(1).index
        return idx.to_series().groupby(idx.to_period("M")).tail(1).index

    def run(self) -> Dict[str, pd.DataFrame]:
        rebalance_dates = set(self._get_rebalance_dates())
        min_warmup = 220
        dates = self.prices.index[min_warmup:]

        if len(dates) == 0:
            raise ValueError("Not enough data after warmup window. Extend start_date earlier.")

        current_weights = {"BIL": 1.0} if "BIL" in self.config.universe else {}
        history = []
        equity = self.config.initial_capital
        prev_date = None

        for date in dates:
            if prev_date is not None:
                daily_ret = 0.0
                for ticker, weight in current_weights.items():
                    if ticker in self.returns.columns and pd.notna(self.returns.at[date, ticker]):
                        daily_ret += weight * self.returns.at[date, ticker]
                equity *= (1.0 + daily_ret)
            else:
                daily_ret = 0.0

            regime = self.regime_detector.classify(date, self.prices, self.features)
            turnover = 0.0

            if date in rebalance_dates:
                target = self.strategy.target_weights(date, regime, self.prices, self.features)
                target = self.risk_engine.scale_to_target_vol(date, target, self.returns)
                target = self.risk_engine.enforce_weight_limits(target)

                ok, reason = self.risk_engine.pre_trade_check(target)
                if not ok:
                    raise ValueError(f"Pre-trade risk check failed on {date.date()}: {reason}")

                turnover = sum(
                    abs(target.get(k, 0.0) - current_weights.get(k, 0.0))
                    for k in set(target).union(current_weights)
                )
                est_cost = turnover * (self.config.trading_cost_bps / 10000.0)
                equity *= (1.0 - est_cost)

                self.broker.submit_orders(date, current_weights, target)
                current_weights = target

            snapshot = {
                "date": date,
                "equity": equity,
                "daily_return": daily_ret,
                "regime": regime,
                "turnover": turnover,
            }

            for ticker in self.config.universe:
                snapshot[f"w_{ticker}"] = current_weights.get(ticker, 0.0)

            history.append(snapshot)
            prev_date = date

        portfolio = pd.DataFrame(history).set_index("date")
        orders = pd.DataFrame(self.broker.order_log)
        return {"portfolio": portfolio, "orders": orders}