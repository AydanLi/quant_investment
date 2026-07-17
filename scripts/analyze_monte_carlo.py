from __future__ import annotations

import argparse
import json
from dataclasses import replace

import pandas as pd

from config.settings import Config
from research.monte_carlo import (
    PairedBootstrapResult,
    evaluate_monte_carlo_robustness,
    paired_block_bootstrap,
)
from scripts.validate_dynamic_factor_model import (
    prepare_snapshot_inputs,
    run_portfolio,
)


def _serialized(result: PairedBootstrapResult) -> dict[str, object]:
    return {
        "simulations": result.simulations,
        "horizon": result.horizon,
        "block_length": result.block_length,
        "seed": result.seed,
        "summary": result.summary,
    }


def _conditional_results(
    baseline: pd.DataFrame,
    candidate: pd.DataFrame,
    *,
    seed: int,
) -> dict[str, PairedBootstrapResult]:
    regime_groups = {
        "bull": {"bull_trend"},
        "bear": {"bear_high_vol"},
        "sideways": {"neutral"},
        "risk_off": {"risk_off"},
    }
    results = {}
    for offset, (label, regimes) in enumerate(regime_groups.items()):
        mask = baseline["regime"].isin(regimes)
        baseline_slice = baseline.loc[mask]
        candidate_slice = candidate.loc[mask]
        if len(baseline_slice) < 2:
            continue
        results[label] = paired_block_bootstrap(
            baseline_slice,
            candidate_slice,
            simulations=2500,
            block_length=1,
            seed=seed + offset,
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only Monte Carlo diagnostic on an immutable trusted snapshot."
    )
    parser.add_argument("--snapshot-id", type=int)
    parser.add_argument("--database", default="quant_research.db")
    args = parser.parse_args()
    seed = 20260715
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
    )
    run_kwargs = {
        "execution_prices": execution_prices,
        "median_dollar_volume": median_dollar_volume,
    }
    baseline = run_portfolio(
        baseline_config, prices, returns, features, **run_kwargs
    )
    candidate = run_portfolio(
        candidate_config, prices, returns, features, **run_kwargs
    )

    main_result = paired_block_bootstrap(
        baseline,
        candidate,
        start=oos_start,
        simulations=5000,
        block_length=20,
        seed=seed,
    )
    block_perturbations = {}
    for block_length in (5, 10, 20, 40, 60):
        label = f"block_length={block_length}"
        block_perturbations[label] = (
            main_result
            if block_length == main_result.block_length
            else paired_block_bootstrap(
                baseline,
                candidate,
                start=oos_start,
                simulations=2500,
                block_length=block_length,
                seed=seed + block_length,
            )
        )

    start_date_results = {}
    for offset, start in enumerate(
        (
            pd.Timestamp("2022-01-01"),
            pd.Timestamp("2023-01-01"),
            pd.Timestamp("2024-01-01"),
            pd.Timestamp("2025-01-01"),
        )
    ):
        label = str(start.date())
        start_date_results[label] = (
            main_result
            if start == oos_start
            else paired_block_bootstrap(
                baseline,
                candidate,
                start=start,
                simulations=2500,
                block_length=20,
                seed=seed + 100 + offset,
            )
        )

    baseline_oos = baseline.loc[oos_start:]
    candidate_oos = candidate.loc[oos_start:]
    regime_results = _conditional_results(
        baseline_oos, candidate_oos, seed=seed + 200
    )

    crisis_periods = {
        "covid_2020": (
            pd.Timestamp("2020-02-19"),
            pd.Timestamp("2020-04-30"),
        ),
        "inflation_bear_2022": (
            pd.Timestamp("2022-01-03"),
            pd.Timestamp("2022-10-12"),
        ),
    }
    crisis_results = {
        label: paired_block_bootstrap(
            baseline,
            candidate,
            start=start,
            end=end,
            simulations=2500,
            block_length=5,
            seed=seed + 300 + offset,
        )
        for offset, (label, (start, end)) in enumerate(crisis_periods.items())
    }

    robustness = evaluate_monte_carlo_robustness(
        main_result,
        block_perturbations=block_perturbations,
        start_date_results=start_date_results,
    )
    output = {
        "dataset_snapshot_id": snapshot_id,
        "method": "paired_circular_block_bootstrap",
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
        "interpretation": {
            "admitted_as_trading_model": False,
            "affects_weights": False,
            "cost_treatment": (
                "daily_return is already net of trading costs and slippage; "
                "cost columns are resampled for audit and are not subtracted twice"
            ),
        },
        "main": _serialized(main_result),
        "robustness": robustness,
        "block_perturbations": {
            label: _serialized(result)
            for label, result in block_perturbations.items()
        },
        "start_dates": {
            label: _serialized(result)
            for label, result in start_date_results.items()
        },
        "regimes": {
            label: _serialized(result)
            for label, result in regime_results.items()
        },
        "crises": {
            label: _serialized(result)
            for label, result in crisis_results.items()
        },
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
