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

    @staticmethod
    def normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
        total = sum(max(v, 0.0) for v in weights.values())
        if total <= 0:
            return {k: 0.0 for k in weights}
        return {k: max(v, 0.0) / total for k, v in weights.items()}

    def scale_to_target_vol(self, date: pd.Timestamp, raw_weights: Dict[str, float], returns: pd.DataFrame) -> Dict[str, float]:
        if not raw_weights:
            return raw_weights

        tickers = [t for t in raw_weights if t in returns.columns]
        if not tickers:
            return raw_weights

        history = returns[tickers].loc[:date]
        if self.config.risk_model == "dynamic_factor":
            try:
                estimate = self.dynamic_factor_model.estimate(history)
            except ValueError:
                return raw_weights
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
        scaled = {k: v * scale for k, v in raw_weights.items()}

        residual = 1.0 - sum(scaled.values())
        if residual > 0 and "BIL" in self.config.universe:
            scaled["BIL"] = scaled.get("BIL", 0.0) + residual

        return scaled

    def enforce_weight_limits(self, weights: Dict[str, float]) -> Dict[str, float]:
        """Apply risk-asset bounds without re-expanding capped positions.

        BIL is treated as the cash-equivalent bucket and is exempt from the
        risky-asset cap.  Any allocation that cannot remain in risky assets
        without exceeding ``max_asset_weight`` is moved to BIL.  If BIL is not
        available, an infeasible target raises instead of silently violating
        the configured limit.
        """
        if not weights:
            if "BIL" in self.config.universe:
                return {"BIL": 1.0}
            raise ValueError("Cannot build a fully invested portfolio from zero weights.")
        if not all(np.isfinite(v) for v in weights.values()):
            raise ValueError("Weights must contain only finite values.")

        positive = {
            ticker: max(float(weight), 0.0) for ticker, weight in weights.items()
        }
        total = sum(positive.values())
        if total <= 0.0:
            if "BIL" in self.config.universe:
                return {"BIL": 1.0}
            raise ValueError("Cannot build a fully invested portfolio from zero weights.")

        normalized = {ticker: weight / total for ticker, weight in positive.items()}
        has_cash_equivalent = "BIL" in self.config.universe
        cash_target = normalized.get("BIL", 0.0) if has_cash_equivalent else 0.0
        desired_risk = {
            ticker: weight
            for ticker, weight in normalized.items()
            if ticker != "BIL" and weight > 0.0
        }

        if not desired_risk:
            if has_cash_equivalent:
                return {"BIL": 1.0}
            raise ValueError("Cannot build a fully invested portfolio from zero weights.")

        risk_budget = 1.0 - cash_target
        min_weight = self.config.min_asset_weight
        max_weight = self.config.max_asset_weight
        minimum_required = len(desired_risk) * min_weight
        if minimum_required > risk_budget + 1e-9:
            raise ValueError(
                "Risk target is infeasible: active positions require more than "
                "the available risk budget at min_asset_weight."
            )

        allocatable_risk = min(risk_budget, len(desired_risk) * max_weight)
        allocated = {ticker: min_weight for ticker in desired_risk}
        remaining = allocatable_risk - minimum_required
        active = set(desired_risk)

        while remaining > 1e-12 and active:
            preference_total = sum(desired_risk[ticker] for ticker in active)
            if preference_total <= 0.0:
                proposed = {ticker: remaining / len(active) for ticker in active}
            else:
                proposed = {
                    ticker: remaining * desired_risk[ticker] / preference_total
                    for ticker in active
                }

            saturated = [
                ticker
                for ticker in active
                if proposed[ticker] >= max_weight - allocated[ticker] - 1e-12
            ]
            if not saturated:
                for ticker in active:
                    allocated[ticker] += proposed[ticker]
                remaining = 0.0
                break

            for ticker in saturated:
                capacity = max_weight - allocated[ticker]
                allocated[ticker] = max_weight
                remaining -= capacity
                active.remove(ticker)

        unallocated = 1.0 - sum(allocated.values())
        if unallocated > 1e-9:
            if not has_cash_equivalent:
                raise ValueError(
                    "Risk target is infeasible without BIL: the active assets "
                    "cannot absorb 100% within max_asset_weight."
                )
            allocated["BIL"] = unallocated
        elif has_cash_equivalent and cash_target > 0.0:
            allocated["BIL"] = max(unallocated, 0.0)

        return allocated

    def pre_trade_check(self, weights: Dict[str, float]) -> Tuple[bool, str]:
        if not all(np.isfinite(v) for v in weights.values()):
            return False, "Weights contain NaN or infinite values."
        if any(v < -1e-9 for v in weights.values()):
            return False, "Negative weights not allowed in v2.1 long-only system."

        total = sum(weights.values())
        if abs(total - 1.0) > 1e-6:
            return False, f"Weights do not sum close to 1.0: {total:.4f}"

        for ticker, weight in weights.items():
            is_cash_equivalent = ticker == "BIL" and "BIL" in self.config.universe
            if not is_cash_equivalent and weight > self.config.max_asset_weight + 1e-9:
                return (
                    False,
                    f"{ticker} weight {weight:.4f} exceeds max_asset_weight "
                    f"{self.config.max_asset_weight:.4f}.",
                )
        return True, "OK"
