from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd

from config.settings import Config


class FeatureEngineer:
    def __init__(self, data: Dict[str, pd.DataFrame], config: Config):
        self.data = data
        self.config = config

    def make_price_frame(self) -> pd.DataFrame:
        prices = {}
        for ticker, df in self.data.items():
            if "Close" in df.columns:
                prices[ticker] = df["Close"]
        price_df = pd.DataFrame(prices).sort_index().dropna(how="all")
        if price_df.empty:
            raise ValueError("Price frame is empty. Cannot continue.")
        return price_df

    def make_returns_frame(self, prices: pd.DataFrame) -> pd.DataFrame:
        return prices.pct_change().replace([np.inf, -np.inf], np.nan)

    def compute_features(self, prices: pd.DataFrame, returns: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        features: Dict[str, pd.DataFrame] = {}
        features["mom_20"] = prices / prices.shift(20) - 1.0
        features["mom_60"] = prices / prices.shift(60) - 1.0
        features["mom_120"] = prices / prices.shift(120) - 1.0
        features["vol_20"] = returns.rolling(20).std() * np.sqrt(252)
        features["ma_50"] = prices.rolling(50).mean()
        features["ma_200"] = prices.rolling(200).mean()
        features["drawdown_200"] = prices / features["ma_200"] - 1.0
        return features