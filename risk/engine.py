from __future__ import annotations

from typing import Dict, Tuple

import numpy as np
import pandas as pd

from config.settings import Config


class RiskEngine:
    def __init__(self, config: Config):
        self.config = config

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

        hist = returns[tickers].loc[:date].tail(60).dropna(how="all")
        if len(hist) < 20:
            return raw_weights

        w = np.array([raw_weights[t] for t in tickers])
        cov = hist.cov().values * 252
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
        clipped = {
            k: min(max(v, self.config.min_asset_weight), self.config.max_asset_weight)
            for k, v in weights.items()
        }
        return self.normalize_weights(clipped)

        return True, "OK"