from __future__ import annotations

from typing import Mapping

import pandas as pd

from backtest.engine import Backtester
from config.settings import Config
from data.features import FeatureEngineer
from research.nested_walk_forward import EvaluationMetrics
from research.protocol import CandidateParameters, apply_candidate
from risk.engine import RiskEngine
from strategy.momentum_rotation import MomentumRotationStrategy
from strategy.regime import RegimeDetector
from utils.metrics import max_drawdown, sharpe_ratio


class CoreStrategyEvaluator:
    """Reference evaluator for the preregistered core strategy.

    It receives only the runner-provided training and validation slices. The
    combined slice supplies feature warmup, while scoring is restricted to the
    validation dates.
    """

    def __init__(self, base_config: Config) -> None:
        self.base_config = base_config

    def __call__(
        self,
        candidate: CandidateParameters,
        training: Mapping[str, pd.DataFrame],
        validation: Mapping[str, pd.DataFrame],
        cost_bps: float,
    ) -> EvaluationMetrics:
        config = apply_candidate(self.base_config, candidate, cost_bps=cost_bps)
        data: dict[str, pd.DataFrame] = {}
        for ticker in set(training).union(validation):
            data[ticker] = pd.concat(
                [training.get(ticker, pd.DataFrame()), validation.get(ticker, pd.DataFrame())]
            ).sort_index()
            data[ticker] = data[ticker][~data[ticker].index.duplicated(keep="last")]
        engineer = FeatureEngineer(data, config)
        prices = engineer.make_price_frame()
        opens = engineer.make_open_frame().reindex(prices.index)
        returns = engineer.make_returns_frame(prices)
        features = engineer.compute_features(prices, returns)
        adv = engineer.make_median_dollar_volume_frame().reindex(prices.index)
        result = Backtester(
            config=config,
            prices=prices,
            execution_prices=opens,
            median_dollar_volume=adv,
            returns=returns,
            features=features,
            regime_detector=RegimeDetector(config),
            strategy=MomentumRotationStrategy(config),
            risk_engine=RiskEngine(config),
        ).run()["portfolio"]
        validation_start = min(
            frame.index.min() for frame in validation.values() if not frame.empty
        )
        validation_end = max(
            frame.index.max() for frame in validation.values() if not frame.empty
        )
        portfolio = result.loc[validation_start:validation_end]
        if portfolio.empty:
            raise ValueError("Validation portfolio is empty after feature warmup.")
        bil_returns = returns.get(config.cash_asset, pd.Series(dtype=float)).reindex(
            portfolio.index
        ).fillna(0.0)
        excess_returns = portfolio["daily_return"] - bil_returns
        stop_rows = portfolio[portfolio["stop_triggered"].astype(bool)]
        minimum_drawdown = float(portfolio["drawdown"].min())
        overshoot = max(
            abs(minimum_drawdown) - config.portfolio_drawdown_stop,
            0.0,
        ) if not stop_rows.empty else 0.0
        return EvaluationMetrics(
            excess_sharpe=sharpe_ratio(excess_returns),
            net_return=float((1.0 + portfolio["daily_return"]).prod() - 1.0),
            benchmark_return=float((1.0 + bil_returns).prod() - 1.0),
            max_drawdown=max_drawdown((1.0 + portfolio["daily_return"]).cumprod()),
            stop_count=len(stop_rows),
            maximum_stop_overshoot=overshoot,
        )
