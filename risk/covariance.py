from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class CovarianceEstimate:
    covariance: np.ndarray
    observations: int
    first_factor_share: float


class DynamicFactorRiskModel:
    """EWMA covariance with an explicit stress on the dominant PCA factor.

    Recent returns receive more weight through the EWMA half-life.  The largest
    covariance eigenvalue is then multiplied by ``pca_stress_multiplier`` so a
    portfolio cannot treat several ETFs driven by the same market factor as
    independent diversification.
    """

    def __init__(
        self,
        *,
        half_life_days: int = 20,
        pca_stress_multiplier: float = 1.5,
        minimum_lookback_days: int = 120,
        minimum_observations: int = 60,
        annualization: int = 252,
    ):
        if half_life_days < 1:
            raise ValueError("half_life_days must be at least 1.")
        if pca_stress_multiplier < 1.0:
            raise ValueError("pca_stress_multiplier must be at least 1.0.")
        if minimum_observations < 2:
            raise ValueError("minimum_observations must be at least 2.")

        self.half_life_days = int(half_life_days)
        self.pca_stress_multiplier = float(pca_stress_multiplier)
        self.minimum_lookback_days = int(minimum_lookback_days)
        self.minimum_observations = int(minimum_observations)
        self.annualization = int(annualization)

    @property
    def lookback_days(self) -> int:
        return max(self.minimum_lookback_days, self.half_life_days * 5)

    def estimate(self, returns: pd.DataFrame) -> CovarianceEstimate:
        history = returns.tail(self.lookback_days).dropna(how="any")
        if len(history) < self.minimum_observations:
            raise ValueError(
                "Dynamic factor covariance requires at least "
                f"{self.minimum_observations} complete observations."
            )

        values = history.to_numpy(dtype=float)
        ages = np.arange(len(values) - 1, -1, -1, dtype=float)
        weights = np.power(0.5, ages / self.half_life_days)
        weights /= weights.sum()

        mean = np.sum(values * weights[:, None], axis=0)
        centered = values - mean
        covariance = (centered * weights[:, None]).T @ centered
        covariance *= self.annualization
        covariance = (covariance + covariance.T) / 2.0

        eigenvalues, eigenvectors = np.linalg.eigh(covariance)
        eigenvalues = np.maximum(eigenvalues, 0.0)
        total_variance = float(eigenvalues.sum())
        largest = int(np.argmax(eigenvalues))
        first_factor_share = (
            float(eigenvalues[largest] / total_variance)
            if total_variance > 0.0
            else 0.0
        )
        eigenvalues[largest] *= self.pca_stress_multiplier
        stressed = eigenvectors @ np.diag(eigenvalues) @ eigenvectors.T
        stressed = (stressed + stressed.T) / 2.0

        if not np.isfinite(stressed).all():
            raise ValueError("Dynamic factor covariance produced non-finite values.")
        return CovarianceEstimate(
            covariance=stressed,
            observations=len(history),
            first_factor_share=first_factor_share,
        )
