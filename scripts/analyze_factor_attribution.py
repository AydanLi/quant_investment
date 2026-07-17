from __future__ import annotations

import argparse
import json
from dataclasses import replace

import pandas as pd

from config.settings import Config
from research.factor_attribution import (
    PROXY_FACTOR_DEFINITIONS,
    RollingFactorAttribution,
    build_proxy_factor_returns,
    fit_factor_regression,
    rolling_attribution_summary,
    rolling_factor_attribution,
)
from scripts.validate_dynamic_factor_model import (
    prepare_snapshot_inputs,
    run_portfolio,
)


def static_summary(result) -> dict[str, object]:
    return {
        "observations": result.observations,
        "r_squared": result.r_squared,
        "adjusted_r_squared": result.adjusted_r_squared,
        "condition_number": result.condition_number,
        "annualized_alpha": float(result.coefficients["alpha"] * 252.0),
        "alpha_newey_west_t_stat": float(result.t_statistics["alpha"]),
        "coefficients": result.coefficients.to_dict(),
        "t_statistics": result.t_statistics.to_dict(),
        "annualized_return_contribution": (
            result.annualized_return_contribution.to_dict()
        ),
        "variance_contribution": result.variance_contribution.to_dict(),
    }


def oos_rolling_attribution(
    portfolio: pd.DataFrame,
    factors: pd.DataFrame,
    cash: pd.Series,
    oos_start: pd.Timestamp,
) -> RollingFactorAttribution:
    full = rolling_factor_attribution(
        portfolio["daily_return"],
        factors,
        cash_returns=cash,
        window=252,
        minimum_observations=126,
    )
    return RollingFactorAttribution(
        exposures=full.exposures.loc[oos_start:],
        contributions=full.contributions.loc[oos_start:],
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only factor attribution on an immutable trusted snapshot."
    )
    parser.add_argument("--snapshot-id", type=int)
    parser.add_argument("--database", default="quant_research.db")
    args = parser.parse_args()
    oos_start = pd.Timestamp("2022-01-01")
    baseline_config = Config(
        risk_model="sample",
        trading_cost_bps=5.0,
        slippage_bps=2.0,
    )
    dynamic_config = replace(
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
    factors, cash = build_proxy_factor_returns(prices)

    baseline = run_portfolio(
        baseline_config,
        prices,
        returns,
        features,
        execution_prices=execution_prices,
        median_dollar_volume=median_dollar_volume,
    )
    dynamic = run_portfolio(
        dynamic_config,
        prices,
        returns,
        features,
        execution_prices=execution_prices,
        median_dollar_volume=median_dollar_volume,
    )

    baseline_static = fit_factor_regression(
        baseline["daily_return"].loc[oos_start:],
        factors.loc[oos_start:],
        cash_returns=cash.loc[oos_start:],
    )
    dynamic_static = fit_factor_regression(
        dynamic["daily_return"].loc[oos_start:],
        factors.loc[oos_start:],
        cash_returns=cash.loc[oos_start:],
    )
    baseline_rolling = oos_rolling_attribution(
        baseline, factors, cash, oos_start
    )
    dynamic_rolling = oos_rolling_attribution(
        dynamic, factors, cash, oos_start
    )
    baseline_rolling_summary = rolling_attribution_summary(
        baseline_rolling
    )
    dynamic_rolling_summary = rolling_attribution_summary(dynamic_rolling)
    baseline_static_summary = static_summary(baseline_static)
    dynamic_static_summary = static_summary(dynamic_static)

    expected_observations = len(dynamic.loc[oos_start:])
    diagnostics = {
        "same_dates": baseline.index.equals(dynamic.index),
        "baseline_reconciliation": (
            baseline_rolling_summary["maximum_reconciliation_error"] < 1e-12
        ),
        "dynamic_reconciliation": (
            dynamic_rolling_summary["maximum_reconciliation_error"] < 1e-12
        ),
        "rolling_coverage": (
            dynamic_rolling_summary["observations"] / expected_observations
            >= 0.95
        ),
        "well_conditioned": max(
            baseline_static.condition_number,
            dynamic_static.condition_number,
        )
        < 30.0,
        "material_explanatory_power": min(
            baseline_static.adjusted_r_squared,
            dynamic_static.adjusted_r_squared,
        )
        >= 0.50,
        "rolling_explanatory_power": min(
            baseline_rolling_summary["oos_r_squared"],
            dynamic_rolling_summary["oos_r_squared"],
        )
        >= 0.50,
    }

    output = {
        "dataset_snapshot_id": snapshot_id,
        "candidate_is_admitted": False,
        "data_start": str(prices.index.min().date()),
        "data_end": str(prices.index.max().date()),
        "analysis_start": str(oos_start.date()),
        "factor_definitions": PROXY_FACTOR_DEFINITIONS,
        "target": "portfolio daily return minus BIL daily return",
        "costs": {
            "trading_cost_bps": dynamic_config.trading_cost_bps,
            "slippage_bps": dynamic_config.slippage_bps,
        },
        "diagnostics": diagnostics,
        "diagnostics_passed": all(diagnostics.values()),
        "comparison": {
            "annualized_alpha_change": (
                dynamic_static_summary["annualized_alpha"]
                - baseline_static_summary["annualized_alpha"]
            ),
            "rolling_annualized_actual_return_change": (
                dynamic_rolling_summary["annualized_actual_return"]
                - baseline_rolling_summary["annualized_actual_return"]
            ),
            "static_exposure_change": {
                factor: (
                    dynamic_static.coefficients[factor]
                    - baseline_static.coefficients[factor]
                )
                for factor in factors.columns
            },
            "rolling_median_exposure_change": {
                factor: (
                    dynamic_rolling_summary["exposure_median"][factor]
                    - baseline_rolling_summary["exposure_median"][factor]
                )
                for factor in factors.columns
            },
        },
        "baseline_sample_covariance": {
            "static": baseline_static_summary,
            "rolling_oos": baseline_rolling_summary,
        },
        "dynamic_factor_system": {
            "static": dynamic_static_summary,
            "rolling_oos": dynamic_rolling_summary,
        },
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
