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


METRIC_POLICY = {
    "selection_sharpe": "sqrt(252) * mean(daily portfolio return - BIL return) / sample_std(excess return)",
    "risk_free_input": "observed BIL return, not the dashboard FRED risk-free series; zero additional rate after subtraction",
    "max_drawdown": "peak-to-trough NAV including initial capital before the first evaluated session",
}


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
        return self.evaluate_path(candidate, training, validation, cost_bps)["metrics"]

    def evaluate_path(
        self, candidate: CandidateParameters, training: Mapping[str, pd.DataFrame],
        validation: Mapping[str, pd.DataFrame], cost_bps: float, *, initial_state=None,
    ) -> dict[str, object]:
        config = apply_candidate(self.base_config, candidate, cost_bps=cost_bps)
        return evaluate_config_path(config, training, validation, initial_state=initial_state)


def evaluate_config_path(
    config: Config, training: Mapping[str, pd.DataFrame],
    validation: Mapping[str, pd.DataFrame], *, initial_state=None,
) -> dict[str, object]:
    """Warm features on history, but trade only validation from explicit state."""
    data: dict[str, pd.DataFrame] = {}
    actions = {}
    for ticker in set(training).union(validation):
        ticker_actions = {}
        actions_declared = False
        for source in (training.get(ticker), validation.get(ticker)):
            if source is not None:
                actions_declared = actions_declared or "corporate_actions" in source.attrs
                for action in source.attrs.get("corporate_actions", ()):
                    actions[repr(action)] = action
                    ticker_actions[repr(action)] = action
        data[ticker] = pd.concat(
            [training.get(ticker, pd.DataFrame()), validation.get(ticker, pd.DataFrame())]
        ).sort_index()
        data[ticker] = data[ticker][~data[ticker].index.duplicated(keep="last")]
        if actions_declared:
            data[ticker].attrs["corporate_actions"] = tuple(ticker_actions.values())
    engineer = FeatureEngineer(data, config)
    prices = engineer.make_price_frame()
    opens = engineer.make_open_frame().reindex(prices.index)
    returns = engineer.make_returns_frame(prices)
    features = engineer.compute_features(prices, returns)
    adv = engineer.make_median_dollar_volume_frame().reindex(prices.index)
    validation_start = min(frame.index.min() for frame in validation.values() if not frame.empty)
    validation_end = max(frame.index.max() for frame in validation.values() if not frame.empty)
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
        raw_close_prices=engineer.make_raw_close_frame().reindex(prices.index),
        corporate_actions=tuple(actions.values()),
        trade_start=validation_start, trade_end=validation_end,
        initial_state=initial_state,
    ).run()
    portfolio = result["portfolio"].loc[validation_start:validation_end]
    if portfolio.empty:
        raise ValueError("Validation portfolio is empty after feature warmup.")
    if config.cash_asset not in returns:
        raise ValueError(
            f"Validation requires {config.cash_asset} benchmark returns."
        )
    bil_returns = returns[config.cash_asset].reindex(portfolio.index)
    if bil_returns.isna().any():
        missing = bil_returns.index[bil_returns.isna()][0]
        raise ValueError(
            f"Missing {config.cash_asset} benchmark return on {missing.date()}."
        )
    portfolio = portfolio.copy()
    portfolio["benchmark_return"] = bil_returns
    return {**result, "portfolio": portfolio,
            "metrics": path_metrics(portfolio, config)}


def path_metrics(portfolio: pd.DataFrame, config: Config) -> EvaluationMetrics:
    """Use the same initial NAV and full benchmark coverage for every stage."""
    bil_returns = portfolio["benchmark_return"]
    excess_returns = portfolio["daily_return"] - bil_returns
    risky_weight_columns = [
        f"w_{ticker}"
        for ticker in config.universe
        if ticker != config.cash_asset and f"w_{ticker}" in portfolio
    ]
    degenerate_all_cash = not risky_weight_columns or bool(
        portfolio[risky_weight_columns].abs().sum(axis=1).le(1e-12).all()
    )
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
        max_drawdown=max_drawdown(pd.concat([
            pd.Series([1.0]),
            (1.0 + portfolio["daily_return"]).cumprod().reset_index(drop=True),
        ], ignore_index=True)),
        stop_count=len(stop_rows),
        maximum_stop_overshoot=overshoot,
        degenerate_all_cash=degenerate_all_cash,
        confirmed_cash_flows=not bool(portfolio.get(
            "unconfirmed_dividend_payments", pd.Series(False, index=portfolio.index)
        ).any()),
    )
