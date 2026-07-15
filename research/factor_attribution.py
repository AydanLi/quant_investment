from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


PROXY_FACTOR_DEFINITIONS = {
    "equity_market": "SPY minus BIL",
    "growth_tilt": "QQQ minus SPY",
    "size_tilt": "IWM minus SPY",
    "duration": "TLT minus BIL",
    "gold": "GLD minus BIL",
    "energy_tilt": "XLE minus SPY",
    "defensive_tilt": "XLV minus SPY",
}


@dataclass(frozen=True)
class FactorRegressionResult:
    coefficients: pd.Series
    standard_errors: pd.Series
    t_statistics: pd.Series
    fitted_returns: pd.Series
    residual_returns: pd.Series
    annualized_return_contribution: pd.Series
    variance_contribution: pd.Series
    r_squared: float
    adjusted_r_squared: float
    condition_number: float
    observations: int


@dataclass(frozen=True)
class RollingFactorAttribution:
    exposures: pd.DataFrame
    contributions: pd.DataFrame


def build_proxy_factor_returns(
    prices: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.Series]:
    """Build transparent ETF proxy factors from the existing local universe.

    Portfolio returns are regressed in excess of BIL.  The factors therefore
    describe broad market exposure plus relative ETF tilts without requiring a
    second data source or a network download.
    """
    required = {"SPY", "QQQ", "IWM", "TLT", "GLD", "XLE", "XLV", "BIL"}
    missing = sorted(required.difference(prices.columns))
    if missing:
        raise ValueError(f"Missing proxy-factor price columns: {missing}")

    returns = prices[sorted(required)].pct_change(fill_method=None)
    cash = returns["BIL"].rename("cash")
    factors = pd.DataFrame(
        {
            "equity_market": returns["SPY"] - cash,
            "growth_tilt": returns["QQQ"] - returns["SPY"],
            "size_tilt": returns["IWM"] - returns["SPY"],
            "duration": returns["TLT"] - cash,
            "gold": returns["GLD"] - cash,
            "energy_tilt": returns["XLE"] - returns["SPY"],
            "defensive_tilt": returns["XLV"] - returns["SPY"],
        }
    )
    return factors, cash


def _aligned_regression_data(
    portfolio_returns: pd.Series,
    factors: pd.DataFrame,
    cash_returns: Optional[pd.Series],
) -> tuple[pd.Series, pd.DataFrame, pd.Series]:
    cash = (
        cash_returns.rename("cash")
        if cash_returns is not None
        else pd.Series(0.0, index=portfolio_returns.index, name="cash")
    )
    aligned = pd.concat(
        [portfolio_returns.rename("portfolio"), cash, factors], axis=1
    ).dropna()
    if aligned.empty:
        raise ValueError("No aligned observations for factor regression.")
    excess = aligned["portfolio"] - aligned["cash"]
    return excess, aligned[factors.columns], aligned["cash"]


def _ols_coefficients(target: np.ndarray, factor_values: np.ndarray) -> np.ndarray:
    design = np.column_stack([np.ones(len(target)), factor_values])
    return np.linalg.lstsq(design, target, rcond=None)[0]


def _newey_west_covariance(
    design: np.ndarray,
    residuals: np.ndarray,
    lags: int,
) -> np.ndarray:
    bread = np.linalg.pinv(design.T @ design)
    weighted_design = design * residuals[:, None]
    meat = weighted_design.T @ weighted_design
    maximum_lag = min(int(lags), len(design) - 1)
    for lag in range(1, maximum_lag + 1):
        weight = 1.0 - lag / (maximum_lag + 1.0)
        cross = weighted_design[lag:].T @ weighted_design[:-lag]
        meat += weight * (cross + cross.T)
    covariance = bread @ meat @ bread
    return (covariance + covariance.T) / 2.0


def fit_factor_regression(
    portfolio_returns: pd.Series,
    factors: pd.DataFrame,
    *,
    cash_returns: Optional[pd.Series] = None,
    annualization: int = 252,
    newey_west_lags: int = 5,
) -> FactorRegressionResult:
    excess, aligned_factors, aligned_cash = _aligned_regression_data(
        portfolio_returns, factors, cash_returns
    )
    if len(excess) <= len(aligned_factors.columns) + 1:
        raise ValueError("Not enough observations for the requested factor model.")

    factor_values = aligned_factors.to_numpy(dtype=float)
    target = excess.to_numpy(dtype=float)
    design = np.column_stack([np.ones(len(target)), factor_values])
    coefficients = np.linalg.lstsq(design, target, rcond=None)[0]
    fitted = design @ coefficients
    residuals = target - fitted

    total_sum_squares = float(np.sum((target - target.mean()) ** 2))
    residual_sum_squares = float(np.sum(residuals**2))
    r_squared = (
        1.0 - residual_sum_squares / total_sum_squares
        if total_sum_squares > 0.0
        else 0.0
    )
    parameter_count = design.shape[1]
    adjusted_r_squared = 1.0 - (1.0 - r_squared) * (
        (len(target) - 1) / (len(target) - parameter_count)
    )

    covariance = _newey_west_covariance(
        design, residuals, newey_west_lags
    )
    standard_errors = np.sqrt(np.maximum(np.diag(covariance), 0.0))
    t_statistics = np.divide(
        coefficients,
        standard_errors,
        out=np.full_like(coefficients, np.nan),
        where=standard_errors > 0.0,
    )
    coefficient_names = ["alpha", *aligned_factors.columns]

    annualized_contribution = pd.Series(
        {
            "cash": float(aligned_cash.mean() * annualization),
            "alpha": float(coefficients[0] * annualization),
            **{
                factor: float(
                    coefficients[position + 1]
                    * aligned_factors[factor].mean()
                    * annualization
                )
                for position, factor in enumerate(aligned_factors.columns)
            },
            "residual": float(residuals.mean() * annualization),
        },
        dtype=float,
    )

    target_variance = float(np.var(target, ddof=1))
    if target_variance > 0.0:
        variance_contribution = pd.Series(
            {
                factor: float(
                    coefficients[position + 1]
                    * np.cov(
                        aligned_factors[factor].to_numpy(dtype=float),
                        target,
                        ddof=1,
                    )[0, 1]
                    / target_variance
                )
                for position, factor in enumerate(aligned_factors.columns)
            },
            dtype=float,
        )
        variance_contribution.loc["residual"] = float(
            1.0 - variance_contribution.sum()
        )
    else:
        variance_contribution = pd.Series(dtype=float)

    standardized_factors = (
        aligned_factors - aligned_factors.mean()
    ) / aligned_factors.std(ddof=0).replace(0.0, 1.0)
    condition_number = float(
        np.linalg.cond(standardized_factors.to_numpy(dtype=float))
    )

    return FactorRegressionResult(
        coefficients=pd.Series(coefficients, index=coefficient_names, dtype=float),
        standard_errors=pd.Series(
            standard_errors, index=coefficient_names, dtype=float
        ),
        t_statistics=pd.Series(
            t_statistics, index=coefficient_names, dtype=float
        ),
        fitted_returns=pd.Series(fitted, index=excess.index, name="fitted"),
        residual_returns=pd.Series(
            residuals, index=excess.index, name="residual"
        ),
        annualized_return_contribution=annualized_contribution,
        variance_contribution=variance_contribution,
        r_squared=float(r_squared),
        adjusted_r_squared=float(adjusted_r_squared),
        condition_number=condition_number,
        observations=len(excess),
    )


def rolling_factor_attribution(
    portfolio_returns: pd.Series,
    factors: pd.DataFrame,
    *,
    cash_returns: Optional[pd.Series] = None,
    window: int = 252,
    minimum_observations: int = 126,
) -> RollingFactorAttribution:
    """Create one-day-ahead attribution using only data before each date."""
    if window < minimum_observations:
        raise ValueError("window must be at least minimum_observations.")
    excess, aligned_factors, aligned_cash = _aligned_regression_data(
        portfolio_returns, factors, cash_returns
    )
    actual_returns = excess + aligned_cash

    exposure_rows = []
    contribution_rows = []
    dates = []
    for position in range(minimum_observations, len(excess)):
        training_start = max(0, position - window)
        train_target = excess.iloc[training_start:position]
        train_factors = aligned_factors.iloc[training_start:position]
        if len(train_target) < minimum_observations:
            continue

        coefficients = _ols_coefficients(
            train_target.to_numpy(dtype=float),
            train_factors.to_numpy(dtype=float),
        )
        date = excess.index[position]
        current_factors = aligned_factors.iloc[position]
        factor_contributions = current_factors * coefficients[1:]
        predicted_excess = float(coefficients[0] + factor_contributions.sum())
        predicted_return = float(aligned_cash.iloc[position] + predicted_excess)
        actual_return = float(actual_returns.iloc[position])

        exposure_rows.append(
            {"alpha": float(coefficients[0]), **dict(zip(aligned_factors.columns, coefficients[1:]))}
        )
        contribution_rows.append(
            {
                "cash": float(aligned_cash.iloc[position]),
                "alpha": float(coefficients[0]),
                **factor_contributions.to_dict(),
                "predicted_return": predicted_return,
                "actual_return": actual_return,
                "residual": actual_return - predicted_return,
            }
        )
        dates.append(date)

    return RollingFactorAttribution(
        exposures=pd.DataFrame(exposure_rows, index=pd.DatetimeIndex(dates)),
        contributions=pd.DataFrame(
            contribution_rows, index=pd.DatetimeIndex(dates)
        ),
    )


def rolling_attribution_summary(
    attribution: RollingFactorAttribution,
    *,
    annualization: int = 252,
) -> dict[str, object]:
    contributions = attribution.contributions
    if contributions.empty:
        raise ValueError("Rolling attribution contains no observations.")
    actual = contributions["actual_return"]
    predicted = contributions["predicted_return"]
    total_sum_squares = float(np.sum((actual - actual.mean()) ** 2))
    residual_sum_squares = float(np.sum((actual - predicted) ** 2))
    oos_r_squared = (
        1.0 - residual_sum_squares / total_sum_squares
        if total_sum_squares > 0.0
        else 0.0
    )
    component_columns = [
        column
        for column in contributions.columns
        if column not in {"predicted_return", "actual_return", "residual"}
    ]
    return {
        "observations": len(contributions),
        "oos_r_squared": float(oos_r_squared),
        "annualized_actual_return": float(actual.mean() * annualization),
        "annualized_predicted_return": float(predicted.mean() * annualization),
        "annualized_residual_return": float(
            contributions["residual"].mean() * annualization
        ),
        "annualized_component_contribution": {
            column: float(contributions[column].mean() * annualization)
            for column in component_columns
        },
        "maximum_reconciliation_error": float(
            (
                contributions["actual_return"]
                - contributions["predicted_return"]
                - contributions["residual"]
            )
            .abs()
            .max()
        ),
        "exposure_median": {
            column: float(attribution.exposures[column].median())
            for column in attribution.exposures.columns
        },
        "exposure_10th_percentile": {
            column: float(attribution.exposures[column].quantile(0.10))
            for column in attribution.exposures.columns
        },
        "exposure_90th_percentile": {
            column: float(attribution.exposures[column].quantile(0.90))
            for column in attribution.exposures.columns
        },
    }
