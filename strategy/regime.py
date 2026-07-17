from __future__ import annotations

import pandas as pd

from config.settings import Config


class RegimeDetector:
    def __init__(self, config: Config):
        self.config = config

    def classify(self, date: pd.Timestamp, prices: pd.DataFrame, features: dict) -> str:
        benchmark = self.config.benchmark
        fear = self.config.fear_gauge

        if benchmark not in prices.columns or fear not in prices.columns:
            raise ValueError("Regime classification requires benchmark and VIX data.")

        try:
            benchmark_price = prices.at[date, benchmark]
            vix = prices.at[date, fear]
            ma_200 = features["ma_200"].at[date, benchmark]
            dd_200 = features["drawdown_200"].at[date, benchmark]
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"Regime inputs are incomplete for {pd.Timestamp(date).date()}."
            ) from exc

        if pd.isna(benchmark_price) or pd.isna(vix) or pd.isna(ma_200) or pd.isna(dd_200):
            raise ValueError(
                f"Regime inputs contain missing values for {pd.Timestamp(date).date()}."
            )

        if vix >= self.config.vix_risk_off_threshold or dd_200 <= self.config.max_allowed_drawdown_from_200d:
            return "risk_off"
        if benchmark_price > ma_200 and vix < self.config.vix_high_threshold:
            return "bull_trend"
        if benchmark_price <= ma_200 and vix >= self.config.vix_high_threshold:
            return "bear_high_vol"
        return "neutral"
