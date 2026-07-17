from __future__ import annotations

from typing import Dict, Tuple

import numpy as np
import pandas as pd

from config.settings import Config
from risk.covariance import DynamicFactorRiskModel


class RiskEngine:
    def __init__(self, config: Config):
        config.validate_risk_constraints()
        self.config = config
        self.dynamic_factor_model = DynamicFactorRiskModel(
            half_life_days=config.ewma_half_life_days,
            pca_stress_multiplier=config.pca_stress_multiplier,
        )

    def _cash_bucket(self) -> str:
        return (
            self.config.cash_asset
            if self.config.cash_asset in self.config.universe
            else self.config.synthetic_cash_asset
        )

    def _is_cash(self, ticker: str) -> bool:
        return ticker in {self.config.cash_asset, self.config.synthetic_cash_asset}

    @staticmethod
    def normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
        total = sum(max(v, 0.0) for v in weights.values())
        if total <= 0:
            return {k: 0.0 for k in weights}
        return {k: max(v, 0.0) / total for k, v in weights.items()}

    def scale_to_target_vol(self, date: pd.Timestamp, raw_weights: Dict[str, float], returns: pd.DataFrame) -> Dict[str, float]:
        if not raw_weights:
            return raw_weights

        tickers = [
            t for t in raw_weights
            if t in returns.columns and not self._is_cash(t)
        ]
        if not tickers:
            return raw_weights

        history = returns[tickers].loc[:date]
        if self.config.risk_model == "dynamic_factor":
            try:
                estimate = self.dynamic_factor_model.estimate(history)
            except ValueError as exc:
                raise ValueError(
                    "Dynamic risk model could not produce an admissible covariance estimate."
                ) from exc
            cov = estimate.covariance
        else:
            hist = history.tail(60).dropna(how="all")
            if len(hist) < 20:
                return raw_weights
            cov = hist.cov().values * 252

        w = np.array([raw_weights[t] for t in tickers])
        if not np.isfinite(cov).all():
            return raw_weights

        port_vol = float(np.sqrt(np.dot(w.T, np.dot(cov, w))))
        if port_vol <= 0:
            return raw_weights

        scale = min(1.0, self.config.target_annual_vol / port_vol)
        scaled = {
            ticker: weight * scale
            for ticker, weight in raw_weights.items()
            if not self._is_cash(ticker)
        }
        existing_cash = sum(
            weight for ticker, weight in raw_weights.items() if self._is_cash(ticker)
        )
        residual = 1.0 - existing_cash - sum(scaled.values())
        cash = self._cash_bucket()
        scaled[cash] = existing_cash + max(residual, 0.0)

        return scaled

    def enforce_weight_limits(self, weights: Dict[str, float]) -> Dict[str, float]:
        """Apply risk-asset bounds without re-expanding capped positions.

        BIL/CASH_USD are exempt cash buckets. Risky allocations are never
        re-expanded after volatility scaling or capping; residual capital is
        routed to cash.
        """
        if not weights:
            return {self._cash_bucket(): 1.0}
        if not all(np.isfinite(v) for v in weights.values()):
            raise ValueError("Weights must contain only finite values.")

        positive = {
            ticker: max(float(weight), 0.0) for ticker, weight in weights.items()
        }
        total = sum(positive.values())
        if total <= 0.0:
            return {self._cash_bucket(): 1.0}

        if total > 1.0 + 1e-9:
            positive = {ticker: value / total for ticker, value in positive.items()}

        allocated: Dict[str, float] = {}
        cash_target = 0.0
        for ticker, weight in positive.items():
            if self._is_cash(ticker):
                cash_target += weight
                continue
            if weight < self.config.min_asset_weight - 1e-12:
                continue
            allocated[ticker] = min(weight, self.config.max_asset_weight)

        cash = self._cash_bucket()
        residual = 1.0 - sum(allocated.values())
        allocated[cash] = max(residual, cash_target, 0.0)
        total_allocated = sum(allocated.values())
        if total_allocated < 1.0 - 1e-9:
            allocated[cash] += 1.0 - total_allocated
        elif total_allocated > 1.0 + 1e-9:
            allocated[cash] = max(0.0, allocated[cash] - (total_allocated - 1.0))
        return {ticker: weight for ticker, weight in allocated.items() if weight > 1e-12}

    def pre_trade_check(self, weights: Dict[str, float]) -> Tuple[bool, str]:
        if not all(np.isfinite(v) for v in weights.values()):
            return False, "Weights contain NaN or infinite values."
        if any(v < -1e-9 for v in weights.values()):
            return False, "Negative weights not allowed in v2.1 long-only system."

        total = sum(weights.values())
        if abs(total - 1.0) > 1e-6:
            return False, f"Weights do not sum close to 1.0: {total:.4f}"

        for ticker, weight in weights.items():
            is_cash_equivalent = self._is_cash(ticker)
            if not is_cash_equivalent and weight > self.config.max_asset_weight + 1e-9:
                return (
                    False,
                    f"{ticker} weight {weight:.4f} exceeds max_asset_weight "
                    f"{self.config.max_asset_weight:.4f}.",
                )
            if (
                not is_cash_equivalent
                and 1e-9 < weight < self.config.min_asset_weight - 1e-9
            ):
                return (
                    False,
                    f"{ticker} weight {weight:.4f} is below min_asset_weight "
                    f"{self.config.min_asset_weight:.4f}.",
                )
        return True, "OK"
