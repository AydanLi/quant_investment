from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd

from config.settings import Config


class MomentumRotationStrategy:
    def __init__(self, config: Config):
        self.config = config

    @staticmethod
    def normalize_weights(weights: Dict[str, float]) -> Dict[str, float]:
        total = sum(max(v, 0.0) for v in weights.values())
        if total <= 0:
            return {k: 0.0 for k in weights}
        return {k: max(v, 0.0) / total for k, v in weights.items()}

    def score_assets(self, date: pd.Timestamp, prices: pd.DataFrame, features: dict) -> pd.Series:
        tickers = [t for t in self.config.universe if t in prices.columns]

        mom_20 = features["mom_20"].loc[date, tickers]
        mom_60 = features["mom_60"].loc[date, tickers]
        mom_120 = features["mom_120"].loc[date, tickers]
        vol_20 = features["vol_20"].loc[date, tickers]

        inv_vol = 1.0 / vol_20.replace(0, np.nan)
        inv_vol = inv_vol.replace([np.inf, -np.inf], np.nan)

        def rank_norm(s: pd.Series) -> pd.Series:
            return s.rank(pct=True).fillna(0.0)

        score = (
            self.config.weight_mom_20 * rank_norm(mom_20)
            + self.config.weight_mom_60 * rank_norm(mom_60)
            + self.config.weight_mom_120 * rank_norm(mom_120)
            + self.config.weight_low_vol * rank_norm(inv_vol)
        )

        score = score.where(mom_60 >= self.config.min_momentum_threshold, other=0.0)
        return score.sort_values(ascending=False)

    def target_weights(self, date: pd.Timestamp, regime: str, prices: pd.DataFrame, features: dict) -> Dict[str, float]:
        scores = self.score_assets(date, prices, features)
        selected = scores.head(self.config.top_n)

        if selected.empty or selected.max() <= 0:
            return {"BIL": 1.0} if "BIL" in self.config.universe else {self.config.universe[0]: 1.0}

        weights = self.normalize_weights(selected.to_dict())

        if regime == "risk_off":
            cash_weight = self.config.risk_off_cash_weight if "BIL" in self.config.universe else 0.0
            scaled = {k: v * (1.0 - cash_weight) for k, v in weights.items()}
            if cash_weight > 0:
                scaled["BIL"] = scaled.get("BIL", 0.0) + cash_weight
            weights = scaled
        elif regime == "bear_high_vol":
            top_items = dict(list(weights.items())[:2])
            top_items = self.normalize_weights(top_items)
            if "BIL" in self.config.universe:
                top_items = {k: v * 0.7 for k, v in top_items.items()}
                top_items["BIL"] = top_items.get("BIL", 0.0) + 0.3
            weights = top_items

        return weights