"""Strict walk-forward evaluation over the latest read-only brokerage mirror."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import itertools
import json
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from backtest.engine import Backtester
from config.settings import Config
from data.features import FeatureEngineer
from research.mirror_walk_forward import evaluate_mirror_walk_forward
from risk.engine import RiskEngine
from storage.db import get_engine
from storage.repositories.brokerage_mirror import BrokerageMirrorRepository
from storage.repositories.market_data import MarketDataRepository
from strategy.momentum_rotation import MomentumRotationStrategy
from strategy.regime import RegimeDetector
from utils.metrics import annualized_volatility, cagr, max_drawdown, sharpe_ratio


TRADING_COST_BPS = 5.0
SLIPPAGE_BPS = 2.0
METHODOLOGY = "expanding_walk_forward_with_untouched_holdout"


def _select_eligible_universe(
    prices: pd.DataFrame,
    tickers: list[str],
    minimum_price: float,
    eligibility_cutoff: pd.Timestamp,
) -> tuple[pd.DataFrame, list[str]]:
    if "SPY" not in prices or "^VIX" not in prices:
        raise ValueError("Market data must include SPY and ^VIX.")

    training_benchmark = prices["SPY"].loc[:eligibility_cutoff].dropna()
    if len(training_benchmark) < 252:
        raise ValueError(
            "At least 252 benchmark sessions are required before the first "
            "validation window."
        )
    training_sessions = training_benchmark.index
    first_decile_cutoff = training_sessions[max(int(len(training_sessions) * 0.10), 0)]
    eligible = []
    for ticker in tickers:
        series = prices.get(ticker)
        if series is None:
            continue
        training_series = series.loc[:eligibility_cutoff].dropna()
        if training_series.empty:
            continue
        coverage = training_series.reindex(training_sessions).notna().mean()
        if (
            training_series.first_valid_index() <= first_decile_cutoff
            and coverage >= 0.90
            and float(training_series.iloc[-1]) >= minimum_price
        ):
            eligible.append(ticker)

    eligible = sorted(set(eligible + ["BIL"]))
    if eligible == ["BIL"]:
        raise ValueError("No mirrored risk assets pass the pre-validation filters.")
    required_columns = list(dict.fromkeys(eligible + ["SPY", "^VIX"]))
    return prices[required_columns].copy(), eligible


def _download(
    tickers: list[str],
    start: str,
    minimum_price: float,
    eligibility_cutoff: pd.Timestamp,
) -> tuple[pd.DataFrame, list[str]]:
    requested = sorted(set(tickers + ["BIL", "SPY", "^VIX"]))
    raw = yf.download(
        tickers=requested,
        start=start,
        auto_adjust=True,
        progress=False,
        group_by="ticker",
        threads=True,
    )
    closes: dict[str, pd.Series] = {}
    for ticker in requested:
        try:
            series = raw[ticker]["Close"].dropna()
        except (KeyError, TypeError):
            continue
        if not series.empty:
            closes[ticker] = series
    prices = pd.DataFrame(closes).sort_index()
    return _select_eligible_universe(
        prices,
        tickers,
        minimum_price,
        eligibility_cutoff,
    )


def _load_cached(
    repository: MarketDataRepository,
    tickers: list[str],
    start: str,
    minimum_price: float,
    eligibility_cutoff: pd.Timestamp,
) -> tuple[pd.DataFrame, list[str]]:
    requested = sorted(set(tickers + ["BIL", "SPY", "^VIX"]))
    prices = repository.get_close_frame(requested, start=start)
    if prices.empty:
        raise ValueError("The local market-data cache is empty for this universe.")
    prices = prices.copy()
    prices.index = pd.to_datetime(prices.index)
    missing_count = len(set(requested) - set(prices.columns))
    if missing_count:
        raise ValueError(
            "The local cache is missing "
            f"{missing_count} of {len(requested)} required symbols. "
            "External symbol disclosure remains disabled."
        )
    return _select_eligible_universe(
        prices,
        tickers,
        minimum_price,
        eligibility_cutoff,
    )


def _metrics(portfolio: pd.DataFrame) -> dict[str, float]:
    if portfolio.empty:
        raise ValueError("Cannot calculate display metrics for an empty period.")
    returns = portfolio["daily_return"].fillna(0.0)
    equity = (1.0 + returns).cumprod()
    years = max(len(returns) / 252.0, 1 / 252.0)
    return {
        "cagr": cagr(pd.concat([pd.Series([1.0]), equity.reset_index(drop=True)])),
        "annual_vol": annualized_volatility(returns),
        "sharpe": sharpe_ratio(returns),
        "max_drawdown": max_drawdown(
            pd.concat([pd.Series([1.0]), equity.reset_index(drop=True)])
        ),
        "annual_turnover": float(portfolio["turnover"].sum() / years),
    }


def _run(config: Config, prices: pd.DataFrame, features: dict) -> pd.DataFrame:
    returns = prices.pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan)
    return Backtester(
        config=config,
        prices=prices,
        returns=returns,
        features=features,
        regime_detector=RegimeDetector(config),
        strategy=MomentumRotationStrategy(config),
        risk_engine=RiskEngine(config),
    ).run()["portfolio"]


def _candidate_parameters() -> dict[str, dict[str, object]]:
    candidates = {}
    grid = itertools.product(
        ["W", "M"],
        [3, 5, 8],
        [0.0, 0.03],
        [0.10, 0.15],
        [0.25, 0.40],
        [0.30, 0.60],
    )
    for frequency, top_n, threshold, target_vol, max_weight, cash in grid:
        label = (
            f"{frequency}_n{top_n}_mom{threshold:.2f}_vol{target_vol:.2f}_"
            f"cap{max_weight:.2f}_cash{cash:.2f}"
        )
        candidates[label] = {
            "rebalance_frequency": frequency,
            "top_n": top_n,
            "min_momentum_threshold": threshold,
            "target_annual_vol": target_vol,
            "max_asset_weight": max_weight,
            "risk_off_cash_weight": cash,
        }
    return candidates


def _config(
    *,
    start: str,
    universe: list[str],
    parameters: dict[str, object],
) -> Config:
    return Config(
        start_date=start,
        universe=universe,
        benchmark="SPY",
        rebalance_frequency=str(parameters["rebalance_frequency"]),
        top_n=int(parameters["top_n"]),
        min_momentum_threshold=float(parameters["min_momentum_threshold"]),
        target_annual_vol=float(parameters["target_annual_vol"]),
        max_asset_weight=float(parameters["max_asset_weight"]),
        risk_off_cash_weight=float(parameters["risk_off_cash_weight"]),
        trading_cost_bps=TRADING_COST_BPS,
        slippage_bps=SLIPPAGE_BPS,
        risk_model="dynamic_factor",
    )


def _source_fingerprint(project_root: Path) -> str:
    roots = [
        project_root / name
        for name in [
            "backtest",
            "config",
            "data",
            "research",
            "risk",
            "strategy",
            "utils",
        ]
    ]
    files = [Path(__file__).resolve()]
    for root in roots:
        files.extend(root.rglob("*.py"))
    digest = hashlib.sha256()
    for path in sorted(set(files)):
        digest.update(str(path.relative_to(project_root)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _ranking_frame(result: dict[str, object]) -> pd.DataFrame:
    rows = []
    for item in result["candidate_ranking"]:
        rows.append(
            {
                "label": item["label"],
                **item["parameters"],
                "selection_score": item["selection_score"],
                "window_win_rate": item["window_win_rate"],
                "mean_sharpe_improvement": item["mean_sharpe_improvement"],
                "median_drawdown_reduction": item[
                    "median_drawdown_reduction"
                ],
                "mean_return_improvement": item["mean_return_improvement"],
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--first-validation", default="2023-01-01")
    parser.add_argument(
        "--holdout-start",
        "--split",
        dest="holdout_start",
        default="2025-01-01",
    )
    parser.add_argument(
        "--minimum-price",
        "--min-latest-price",
        dest="minimum_price",
        type=float,
        default=5.0,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".runtime/mirror_optimization"),
    )
    parser.add_argument(
        "--allow-external-symbol-disclosure",
        action="store_true",
        help=(
            "Explicitly allow sending the mirrored symbol list to Yahoo "
            "Finance. By default only the local cache is used."
        ),
    )
    args = parser.parse_args()

    first_validation = pd.Timestamp(args.first_validation)
    holdout_start = pd.Timestamp(args.holdout_start)
    mirror = BrokerageMirrorRepository(get_engine()).get_latest(
        "robinhood", "0908"
    )
    if mirror.empty:
        raise ValueError("No Robinhood mirror snapshot exists for account_ref 0908.")
    held = sorted(mirror["symbol"].unique().tolist())
    eligibility_cutoff = first_validation - pd.Timedelta(days=1)
    try:
        if args.allow_external_symbol_disclosure:
            prices, universe = _download(
                held,
                args.start,
                args.minimum_price,
                eligibility_cutoff,
            )
        else:
            prices, universe = _load_cached(
                MarketDataRepository(get_engine()),
                held,
                args.start,
                args.minimum_price,
                eligibility_cutoff,
            )
    except ValueError as exc:
        raise SystemExit(f"Optimization stopped: {exc}") from None
    feature_config = Config(start_date=args.start, universe=universe)
    features = FeatureEngineer({}, feature_config).compute_features(
        prices,
        prices.pct_change(fill_method=None),
    )

    baseline_parameters = {
        "rebalance_frequency": "M",
        "top_n": 3,
        "min_momentum_threshold": 0.0,
        "target_annual_vol": 0.12,
        "max_asset_weight": 0.40,
        "risk_off_cash_weight": 0.50,
    }
    baseline = _run(
        _config(
            start=args.start,
            universe=universe,
            parameters=baseline_parameters,
        ),
        prices,
        features,
    )
    parameter_grid = _candidate_parameters()
    candidates = {
        label: _run(
            _config(start=args.start, universe=universe, parameters=parameters),
            prices,
            features,
        )
        for label, parameters in parameter_grid.items()
    }

    start_dates = [
        first_validation,
        first_validation + pd.DateOffset(months=3),
        first_validation + pd.DateOffset(months=6),
    ]
    crisis_periods = {
        "2022_inflation_bear": (
            pd.Timestamp("2022-01-03"),
            pd.Timestamp("2022-10-14"),
        ),
        "2023_regional_banks": (
            pd.Timestamp("2023-03-08"),
            pd.Timestamp("2023-05-01"),
        ),
    }
    evaluation = evaluate_mirror_walk_forward(
        baseline=baseline,
        candidates=candidates,
        candidate_parameters=parameter_grid,
        first_validation_start=first_validation,
        holdout_start=holdout_start,
        start_dates=start_dates,
        crisis_periods=crisis_periods,
        trading_cost_bps=TRADING_COST_BPS,
        slippage_bps=SLIPPAGE_BPS,
        baseline_trading_cost_bps=TRADING_COST_BPS,
        baseline_slippage_bps=SLIPPAGE_BPS,
        independent_signal=False,
        historical_universe_integrity=False,
    )

    selected_label = evaluation["selected_label"]
    selected_portfolio = candidates[selected_label]
    holdout_portfolio = selected_portfolio.loc[holdout_start:]
    holdout_metrics = _metrics(holdout_portfolio)
    latest = selected_portfolio.iloc[-1]
    weights = {
        ticker: float(latest.get(f"w_{ticker}", 0.0))
        for ticker in universe
        if float(latest.get(f"w_{ticker}", 0.0)) > 1e-6
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    _ranking_frame(evaluation).to_csv(
        args.output.with_suffix(".csv"),
        index=False,
    )
    project_root = Path(__file__).resolve().parent.parent
    best = {
        **evaluation["selected_parameters"],
        **{f"test_{key}": value for key, value in holdout_metrics.items()},
    }
    summary = {
        "schema_version": 2,
        "methodology": METHODOLOGY,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_fingerprint": _source_fingerprint(project_root),
        "mirror_snapshot_id": int(mirror["snapshot_id"].iloc[0]),
        "held_symbols": held,
        "eligible_universe": universe,
        "excluded_symbols": sorted(set(held) - set(universe)),
        "minimum_pre_validation_price": args.minimum_price,
        "train_period": [str(first_validation.date()), args.holdout_start],
        "test_period": [
            args.holdout_start,
            str(selected_portfolio.index.max().date()),
        ],
        "baseline_parameters": baseline_parameters,
        "best": best,
        "admission": evaluation,
        "admitted": evaluation["admitted"],
        "position_changes_authorized": evaluation[
            "position_changes_authorized"
        ],
        "latest_signal_date": str(selected_portfolio.index[-1].date()),
        "latest_regime": str(latest["regime"]),
        "latest_weights": weights,
    }
    encoded = json.dumps(summary, indent=2, ensure_ascii=False)
    args.output.with_suffix(".json").write_text(encoded, encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
