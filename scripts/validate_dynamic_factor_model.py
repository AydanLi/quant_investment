from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from itertools import product

import pandas as pd

from backtest.engine import Backtester
from config.settings import Config
from data.features import FeatureEngineer
from research.model_admission import evaluate_admission
from risk.covariance import DynamicFactorRiskModel
from risk.engine import RiskEngine
from strategy.momentum_rotation import MomentumRotationStrategy
from strategy.regime import RegimeDetector


def load_cached_market_data(
    database_path: str = "quant_research.db",
) -> dict[str, pd.DataFrame]:
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        raw = pd.read_sql_query(
            "select ticker, date, open, high, low, close, volume "
            "from market_data order by date, ticker",
            connection,
        )
    finally:
        connection.close()

    if raw.empty:
        raise ValueError("The market_data cache is empty.")
    raw["date"] = pd.to_datetime(raw["date"])
    frames: dict[str, pd.DataFrame] = {}
    for ticker, group in raw.groupby("ticker"):
        frame = group.set_index("date").drop(columns="ticker")
        frame.columns = [column.title() for column in frame.columns]
        frames[str(ticker)] = frame
    return frames


def run_portfolio(
    config: Config,
    prices: pd.DataFrame,
    returns: pd.DataFrame,
    features: dict[str, pd.DataFrame],
) -> pd.DataFrame:
    return Backtester(
        config=config,
        prices=prices,
        returns=returns,
        features=features,
        regime_detector=RegimeDetector(config),
        strategy=MomentumRotationStrategy(config),
        risk_engine=RiskEngine(config),
    ).run()["portfolio"]


def build_independence_signals(
    config: Config,
    prices: pd.DataFrame,
    returns: pd.DataFrame,
    features: dict[str, pd.DataFrame],
    oos_start: pd.Timestamp,
) -> tuple[pd.Series, pd.Series]:
    month_ends = (
        prices.index.to_series()
        .groupby(prices.index.to_period("M"))
        .tail(1)
        .index
    )
    month_ends = month_ends[month_ends >= oos_start]
    strategy = MomentumRotationStrategy(config)
    model = DynamicFactorRiskModel(
        half_life_days=config.ewma_half_life_days,
        pca_stress_multiplier=config.pca_stress_multiplier,
    )
    risk_assets = [
        ticker
        for ticker in config.universe
        if ticker != "BIL" and ticker in returns.columns
    ]

    momentum_values: dict[pd.Timestamp, float] = {}
    factor_values: dict[pd.Timestamp, float] = {}
    for date in month_ends:
        scores = strategy.score_assets(date, prices, features)
        positive = scores[scores > 0.0].head(config.top_n)
        if positive.empty:
            continue
        try:
            estimate = model.estimate(returns[risk_assets].loc[:date])
        except ValueError:
            continue
        momentum_values[date] = float(positive.mean())
        factor_values[date] = estimate.first_factor_share

    return pd.Series(momentum_values), pd.Series(factor_values)


def main() -> None:
    oos_start = pd.Timestamp("2022-01-01")
    baseline_config = Config(
        risk_model="sample",
        trading_cost_bps=5.0,
        slippage_bps=2.0,
    )
    candidate_config = replace(
        baseline_config,
        risk_model="dynamic_factor",
        ewma_half_life_days=20,
        pca_stress_multiplier=1.50,
    )

    data = load_cached_market_data()
    engineer = FeatureEngineer(data, baseline_config)
    prices = engineer.make_price_frame()
    returns = engineer.make_returns_frame(prices)
    features = engineer.compute_features(prices, returns)

    baseline = run_portfolio(baseline_config, prices, returns, features)
    candidate = run_portfolio(candidate_config, prices, returns, features)
    perturbations = {}
    for half_life, stress in product([16, 20, 24], [1.35, 1.50, 1.65]):
        label = f"half_life={half_life},stress={stress:.2f}"
        config = replace(
            candidate_config,
            ewma_half_life_days=half_life,
            pca_stress_multiplier=stress,
        )
        perturbations[label] = run_portfolio(config, prices, returns, features)

    momentum_signal, model_signal = build_independence_signals(
        candidate_config,
        prices,
        returns,
        features,
        oos_start,
    )
    admission = evaluate_admission(
        baseline=baseline,
        candidate=candidate,
        parameter_candidates=perturbations,
        momentum_signal=momentum_signal,
        model_signal=model_signal,
        oos_start=oos_start,
        first_test_year=2022,
        start_dates=[
            pd.Timestamp("2022-01-01"),
            pd.Timestamp("2023-01-01"),
            pd.Timestamp("2024-01-01"),
            pd.Timestamp("2025-01-01"),
        ],
        crisis_periods={
            "covid_2020": (
                pd.Timestamp("2020-02-19"),
                pd.Timestamp("2020-04-30"),
            ),
            "inflation_bear_2022": (
                pd.Timestamp("2022-01-03"),
                pd.Timestamp("2022-10-12"),
            ),
        },
    )
    output = {
        "model": "dynamic_factor",
        "data_start": str(prices.index.min().date()),
        "data_end": str(prices.index.max().date()),
        "oos_start": str(oos_start.date()),
        "baseline": {
            "risk_model": baseline_config.risk_model,
            "trading_cost_bps": baseline_config.trading_cost_bps,
            "slippage_bps": baseline_config.slippage_bps,
        },
        "candidate": {
            "risk_model": candidate_config.risk_model,
            "ewma_half_life_days": candidate_config.ewma_half_life_days,
            "pca_stress_multiplier": candidate_config.pca_stress_multiplier,
            "trading_cost_bps": candidate_config.trading_cost_bps,
            "slippage_bps": candidate_config.slippage_bps,
        },
        "admission": admission,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
