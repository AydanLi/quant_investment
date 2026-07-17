from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import json
from typing import Mapping

import numpy as np
import pandas as pd
from sqlalchemy import select

from backtest.engine import Backtester
from config.settings import Config
from data.adjustments import locally_adjust_ohlcv
from data.calendar import NyseCalendar
from data.features import FeatureEngineer
from research.model_admission import evaluate_admission
from research.risk_model_protocol import (
    dynamic_factor_candidate_grid,
    validate_risk_model_stage,
)
from risk.covariance import DynamicFactorRiskModel
from risk.engine import RiskEngine
from storage.db import create_db_engine
from storage.repositories.governance import GovernanceRepository
from storage.repositories.trusted_data import TrustedMarketDataRepository
from storage.schema import dataset_snapshots, strategy_versions
from strategy.momentum_rotation import MomentumRotationStrategy
from strategy.regime import RegimeDetector


def _database_url(database: str) -> str:
    return database if "://" in database else f"sqlite:///{Path(database).as_posix()}"


def load_snapshot_market_data(
    *,
    database: str = "quant_research.db",
    snapshot_id: int | None = None,
    require_actionable: bool = False,
) -> tuple[int, dict[str, pd.DataFrame]]:
    """Load only immutable trusted snapshot rows; never read legacy market_data."""
    engine = create_db_engine(_database_url(database))
    with engine.connect() as connection:
        if snapshot_id is None:
            row = connection.execute(
                select(
                    dataset_snapshots.c.id,
                    dataset_snapshots.c.status,
                    dataset_snapshots.c.quality_json,
                )
                .where(dataset_snapshots.c.status.in_(["TRUSTED", "WARNING"]))
                .order_by(dataset_snapshots.c.id.desc())
                .limit(1)
            ).one_or_none()
        else:
            row = connection.execute(
                select(
                    dataset_snapshots.c.id,
                    dataset_snapshots.c.status,
                    dataset_snapshots.c.quality_json,
                ).where(dataset_snapshots.c.id == int(snapshot_id))
            ).one_or_none()
    if row is None:
        raise ValueError("No matching trusted dataset snapshot is available.")
    if str(row.status) == "BLOCKED":
        raise ValueError("A blocked dataset snapshot cannot enter model admission.")
    stale_sessions = (row.quality_json or {}).get("stale_sessions")
    if require_actionable and stale_sessions != 0:
        raise ValueError("Model admission requires a zero-staleness dataset snapshot.")

    resolved_id = int(row.id)
    payload = TrustedMarketDataRepository(engine=engine).load_snapshot(resolved_id)
    actions_by_ticker: dict[str, list] = {}
    for action in payload.actions:
        actions_by_ticker.setdefault(action.ticker, []).append(action)
    frames = {
        ticker: locally_adjust_ohlcv(frame, actions_by_ticker.get(ticker, ()))
        for ticker, frame in payload.bars.items()
    }
    return resolved_id, frames


def load_cached_market_data(
    database_path: str = "quant_research.db",
    snapshot_id: int | None = None,
    require_actionable: bool = False,
) -> dict[str, pd.DataFrame]:
    """Compatibility name backed exclusively by immutable v3 snapshots."""
    return load_snapshot_market_data(
        database=database_path,
        snapshot_id=snapshot_id,
        require_actionable=require_actionable,
    )[1]


def prepare_snapshot_inputs(
    config: Config,
    *,
    database: str = "quant_research.db",
    snapshot_id: int | None = None,
    require_actionable: bool = False,
) -> tuple[
    int,
    dict[str, pd.DataFrame],
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    dict[str, pd.DataFrame],
    pd.DataFrame,
]:
    resolved_id, data = load_snapshot_market_data(
        database=database,
        snapshot_id=snapshot_id,
        require_actionable=require_actionable,
    )
    engineer = FeatureEngineer(data, config)
    prices = engineer.make_price_frame()
    execution_prices = engineer.make_open_frame().reindex(prices.index)
    returns = engineer.make_returns_frame(prices)
    features = engineer.compute_features(prices, returns)
    median_dollar_volume = engineer.make_median_dollar_volume_frame().reindex(
        prices.index
    )
    return (
        resolved_id,
        data,
        prices,
        execution_prices,
        returns,
        features,
        median_dollar_volume,
    )


def run_portfolio(
    config: Config,
    prices: pd.DataFrame,
    returns: pd.DataFrame,
    features: dict[str, pd.DataFrame],
    *,
    execution_prices: pd.DataFrame,
    median_dollar_volume: pd.DataFrame,
) -> pd.DataFrame:
    return Backtester(
        config=config,
        prices=prices,
        execution_prices=execution_prices,
        returns=returns,
        median_dollar_volume=median_dollar_volume,
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
    calendar = NyseCalendar()
    month_ends = pd.DatetimeIndex(
        [
            date
            for date in prices.index
            if date >= oos_start and calendar.is_month_end_session(date)
        ]
    )
    strategy = MomentumRotationStrategy(config)
    model = DynamicFactorRiskModel(
        half_life_days=config.ewma_half_life_days,
        pca_stress_multiplier=config.pca_stress_multiplier,
    )
    risk_assets = [
        ticker
        for ticker in config.universe
        if ticker != config.cash_asset and ticker in returns.columns
    ]

    momentum_values: dict[pd.Timestamp, float] = {}
    factor_values: dict[pd.Timestamp, float] = {}
    for date in month_ends:
        scores = strategy.score_assets(date, prices.loc[:date], {
            name: frame.loc[:date] for name, frame in features.items()
        })
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


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate the six preregistered dynamic risk models on an immutable snapshot."
    )
    parser.add_argument("--snapshot-id", type=int, required=True)
    parser.add_argument("--strategy-version", required=True)
    parser.add_argument("--database", default="quant_research.db")
    parser.add_argument("--oos-start", default="2022-01-01")
    args = parser.parse_args()

    db_url = _database_url(args.database)
    engine = create_db_engine(db_url)
    governance = GovernanceRepository(engine=engine)
    with engine.connect() as connection:
        frozen_record = connection.execute(
            select(
                strategy_versions.c.status,
                strategy_versions.c.dataset_snapshot_id,
            ).where(strategy_versions.c.version == args.strategy_version)
        ).one_or_none()
    if frozen_record is None or frozen_record.status != "frozen":
        raise ValueError("Risk-model admission requires a frozen core strategy version.")
    if int(frozen_record.dataset_snapshot_id or -1) != args.snapshot_id:
        raise ValueError(
            "Risk-model admission must use the dataset snapshot bound to the frozen core strategy."
        )
    baseline_config = Config(
        risk_model="sample",
        trading_cost_bps=5.0,
        slippage_bps=2.0,
        strategy_version=args.strategy_version,
        db_url=db_url,
    )
    candidates = dynamic_factor_candidate_grid()
    validate_risk_model_stage(
        core_strategy_frozen=governance.is_strategy_frozen(args.strategy_version),
        baseline_model=baseline_config.risk_model,
        evaluated_labels={candidate.label for candidate in candidates},
    )

    (
        snapshot_id,
        _,
        prices,
        execution_prices,
        returns,
        features,
        median_dollar_volume,
    ) = prepare_snapshot_inputs(
        baseline_config,
        database=args.database,
        snapshot_id=args.snapshot_id,
        require_actionable=True,
    )
    common = {
        "execution_prices": execution_prices,
        "median_dollar_volume": median_dollar_volume,
    }
    baseline = run_portfolio(
        baseline_config, prices, returns, features, **common
    )
    configurations = {
        candidate.label: replace(
            baseline_config,
            risk_model="dynamic_factor",
            ewma_half_life_days=candidate.half_life_days,
            pca_stress_multiplier=candidate.stress_multiplier,
        )
        for candidate in candidates
    }
    portfolios = {
        label: run_portfolio(config, prices, returns, features, **common)
        for label, config in configurations.items()
    }

    oos_start = pd.Timestamp(args.oos_start)
    start_dates = [
        pd.Timestamp(year, 1, 1)
        for year in range(oos_start.year, int(prices.index.max().year) + 1)
        if pd.Timestamp(year, 1, 1) <= prices.index.max()
    ]
    crisis_periods = {
        "covid_2020": (pd.Timestamp("2020-02-19"), pd.Timestamp("2020-04-30")),
        "inflation_bear_2022": (
            pd.Timestamp("2022-01-03"),
            pd.Timestamp("2022-10-12"),
        ),
    }
    evaluations: dict[str, object] = {}
    for label, config in configurations.items():
        momentum_signal, model_signal = build_independence_signals(
            config, prices, returns, features, oos_start
        )
        evaluations[label] = evaluate_admission(
            baseline=baseline,
            candidate=portfolios[label],
            parameter_candidates=portfolios,
            momentum_signal=momentum_signal,
            model_signal=model_signal,
            oos_start=oos_start,
            first_test_year=oos_start.year,
            start_dates=start_dates,
            crisis_periods=crisis_periods,
        )

    admitted = [
        label for label, result in evaluations.items() if bool(result["admitted"])
    ]
    selected = (
        max(
            admitted,
            key=lambda label: float(
                evaluations[label]["overall_oos"]["sharpe_improvement"]
            ),
        )
        if admitted
        else None
    )
    output = {
        "dataset_snapshot_id": snapshot_id,
        "strategy_version": args.strategy_version,
        "core_strategy_frozen": True,
        "baseline_model": "sample",
        "risk_model_default_remains": "sample" if selected is None else selected,
        "selected_admitted_candidate": selected,
        "candidate_count": len(candidates),
        "evaluations": evaluations,
    }
    print(
        json.dumps(
            _jsonable(output), ensure_ascii=False, indent=2, allow_nan=False
        )
    )


if __name__ == "__main__":
    main()
