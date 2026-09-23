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
from data.models import DATA_QUALITY_MODEL_VERSION
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
    if require_actionable and (row.quality_json or {}).get("quality_model_version") != DATA_QUALITY_MODEL_VERSION:
        raise ValueError("Model admission requires a newly audited current-quality snapshot.")
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
    raw_close_prices: pd.DataFrame,
    corporate_actions: tuple = (),
) -> pd.DataFrame:
    return Backtester(
        config=config,
        prices=prices,
        execution_prices=execution_prices,
        raw_close_prices=raw_close_prices,
        corporate_actions=corporate_actions,
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
    from research.risk_admission import build_risk_protocol, run_risk_admission
    from research.runtime import canonical_hash
    from scripts.run_core_admission import load_immutable_snapshot

    parser = argparse.ArgumentParser(description="Evaluate a risk extension against untouched post-core evidence.")
    parser.add_argument("--snapshot-id", type=int, required=True)
    parser.add_argument("--strategy-version", required=True, help="Frozen parent core version")
    parser.add_argument("--candidate-version", help="New child version; never edits the parent")
    parser.add_argument("--database", default="quant_research.db")
    parser.add_argument("--holdout-start", required=True)
    parser.add_argument("--diagnostic-only", action="store_true")
    args = parser.parse_args()
    engine = create_db_engine(_database_url(args.database))
    governance = GovernanceRepository(engine=engine)
    parent = governance.load_frozen_runtime(args.strategy_version)
    if pd.Timestamp(args.holdout_start) <= pd.Timestamp(parent.research_cutoff):
        output = {"status": "CONDITIONAL_DIAGNOSTIC_ONLY", "admitted": False,
                  "risk_model_default_remains": "sample",
                  "reason": "The requested interval was used to discover the frozen core.",
                  "parent_runtime_hash": parent.runtime_hash}
        print(json.dumps(output, indent=2))
        return
    if not args.diagnostic_only and not args.candidate_version:
        parser.error("Formal risk research requires a distinct --candidate-version.")
    if args.diagnostic_only:
        parser.error("Diagnostic mode cannot inspect protected post-core holdout; register a formal child version.")
    child = args.candidate_version or args.strategy_version + "-diagnostic"
    if child == args.strategy_version:
        parser.error("The child version must differ from the frozen parent.")
    protocol = build_risk_protocol(parent, dataset_snapshot_id=args.snapshot_id,
                                   holdout_start=args.holdout_start)
    run_id = None
    if not args.diagnostic_only:
        governance.create_strategy_version(
            version=child, universe_version=str(parent.config["universe_version"]),
            protocol=protocol, dataset_snapshot_id=args.snapshot_id,
            code_commit=str(parent.code_identity["code_commit"]),
        )
        run_id = governance.start_admission(
            strategy_version=child, methodology="nested_risk_extension_v1",
            protocol_hash=canonical_hash(protocol),
            results={"parent_runtime_hash": parent.runtime_hash,
                     "evidence_role": "post_core_holdout"},
        )
    def record(trial):
        if run_id is not None:
            governance.save_admission_trial(run_id, **trial)
    finished = False
    try:
        data = load_immutable_snapshot(args.database, args.snapshot_id)
        output = run_risk_admission(parent, data, protocol, strategy_version=child,
                                    trial_callback=record)
        if args.diagnostic_only:
            output.update(admitted=False, status="CONDITIONAL_DIAGNOSTIC_ONLY",
                          risk_model_default_remains="sample")
        elif run_id is not None:
            governance.finish_admission(run_id,
                status="admitted" if output["admitted"] else "rejected", results=output)
            finished = True
            output["strategy_approval"] = "SEPARATE_MANUAL_STEP"
    except Exception as exc:
        if run_id is not None and not finished:
            governance.finish_admission(run_id, status="failed",
                results={"admitted": False, "error": str(exc)}, error_message=str(exc))
        raise
    print(json.dumps(_jsonable(output), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
