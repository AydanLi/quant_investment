"""Walk-forward parameter search over the latest read-only brokerage mirror."""
from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from backtest.engine import Backtester
from config.settings import Config
from data.features import FeatureEngineer
from risk.engine import RiskEngine
from storage.db import get_engine
from storage.repositories.brokerage_mirror import BrokerageMirrorRepository
from strategy.momentum_rotation import MomentumRotationStrategy
from strategy.regime import RegimeDetector
from utils.metrics import annualized_volatility, cagr, max_drawdown, sharpe_ratio


def _download(
    tickers: list[str], start: str, min_latest_price: float
) -> tuple[pd.DataFrame, list[str]]:
    raw = yf.download(
        tickers=sorted(set(tickers + ["BIL", "SPY", "^VIX"])),
        start=start,
        auto_adjust=True,
        progress=False,
        group_by="ticker",
        threads=True,
    )
    closes: dict[str, pd.Series] = {}
    for ticker in sorted(set(tickers + ["BIL", "SPY", "^VIX"])):
        try:
            series = raw[ticker]["Close"].dropna()
        except (KeyError, TypeError):
            continue
        if not series.empty:
            closes[ticker] = series
    prices = pd.DataFrame(closes).sort_index()
    benchmark_sessions = prices["SPY"].dropna().index
    cutoff = benchmark_sessions[int(len(benchmark_sessions) * 0.10)]
    eligible = []
    for ticker in tickers:
        series = prices.get(ticker)
        if series is None:
            continue
        coverage = series.reindex(benchmark_sessions).notna().mean()
        if (
            series.first_valid_index() <= cutoff
            and coverage >= 0.90
            and float(series.dropna().iloc[-1]) >= min_latest_price
        ):
            eligible.append(ticker)
    eligible = sorted(set(eligible + ["BIL"]))
    required_columns = list(dict.fromkeys(eligible + ["SPY", "^VIX"]))
    return prices[required_columns].copy(), eligible


def _metrics(portfolio: pd.DataFrame) -> dict[str, float]:
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--split", default="2025-01-01")
    parser.add_argument("--min-latest-price", type=float, default=5.0)
    parser.add_argument("--output", type=Path, default=Path(".runtime/mirror_optimization"))
    args = parser.parse_args()

    mirror = BrokerageMirrorRepository(get_engine()).get_latest("robinhood", "0908")
    if mirror.empty:
        raise ValueError("No Robinhood mirror snapshot exists for account_ref 0908.")
    held = sorted(mirror["symbol"].unique().tolist())
    prices, universe = _download(held, args.start, args.min_latest_price)
    feature_config = Config(start_date=args.start, universe=universe)
    features = FeatureEngineer({}, feature_config).compute_features(
        prices, prices.pct_change(fill_method=None)
    )

    grid = itertools.product(
        ["W", "M"], [3, 5, 8], [0.0, 0.03], [0.10, 0.15], [0.25, 0.40], [0.30, 0.60]
    )
    rows = []
    portfolios: dict[int, pd.DataFrame] = {}
    for run_id, (frequency, top_n, threshold, target_vol, max_weight, cash) in enumerate(grid, 1):
        config = Config(
            start_date=args.start,
            universe=universe,
            benchmark="SPY",
            rebalance_frequency=frequency,
            top_n=top_n,
            min_momentum_threshold=threshold,
            target_annual_vol=target_vol,
            max_asset_weight=max_weight,
            risk_off_cash_weight=cash,
            trading_cost_bps=5.0,
            slippage_bps=2.0,
            risk_model="dynamic_factor",
        )
        portfolio = _run(config, prices, features)
        split = pd.Timestamp(args.split)
        train = _metrics(portfolio.loc[portfolio.index < split])
        test = _metrics(portfolio.loc[portfolio.index >= split])
        score = (
            0.35 * train["sharpe"]
            + 0.65 * test["sharpe"]
            - 0.50 * abs(test["max_drawdown"])
            - 0.02 * test["annual_turnover"]
            - 0.20 * abs(train["sharpe"] - test["sharpe"])
        )
        rows.append(
            {
                "run_id": run_id,
                "score": score,
                "frequency": frequency,
                "top_n": top_n,
                "momentum_threshold": threshold,
                "target_vol": target_vol,
                "max_weight": max_weight,
                "risk_off_cash": cash,
                **{f"train_{k}": v for k, v in train.items()},
                **{f"test_{k}": v for k, v in test.items()},
            }
        )
        portfolios[run_id] = portfolio

    results = pd.DataFrame(rows).sort_values("score", ascending=False)
    best = results.iloc[0]
    best_portfolio = portfolios[int(best["run_id"])]
    latest = best_portfolio.iloc[-1]
    weights = {
        ticker: float(latest.get(f"w_{ticker}", 0.0))
        for ticker in universe
        if float(latest.get(f"w_{ticker}", 0.0)) > 1e-6
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    results.to_csv(args.output.with_suffix(".csv"), index=False)
    summary = {
        "mirror_snapshot_id": int(mirror["snapshot_id"].iloc[0]),
        "held_symbols": held,
        "eligible_universe": universe,
        "excluded_symbols": sorted(set(held) - set(universe)),
        "minimum_latest_price": args.min_latest_price,
        "train_period": [str(best_portfolio.index.min().date()), args.split],
        "test_period": [args.split, str(best_portfolio.index.max().date())],
        "best": best.to_dict(),
        "latest_signal_date": str(best_portfolio.index[-1].date()),
        "latest_regime": str(latest["regime"]),
        "latest_weights": weights,
    }
    args.output.with_suffix(".json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
