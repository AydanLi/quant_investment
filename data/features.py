from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd

from config.settings import Config
from config.universe import CASH_ETF, EligibilityRules


class FeatureEngineer:
    def __init__(self, data: Dict[str, pd.DataFrame], config: Config):
        self.data = data
        self.config = config

    def make_price_frame(self) -> pd.DataFrame:
        prices = {}
        for ticker, df in self.data.items():
            column = "Adjusted Close" if "Adjusted Close" in df.columns else "Close"
            if column in df.columns:
                prices[ticker] = df[column]
        price_df = pd.DataFrame(prices).sort_index().dropna(how="all")
        if self.config.benchmark in price_df:
            benchmark_sessions = price_df[self.config.benchmark].dropna().index
            price_df = price_df.reindex(benchmark_sessions)
        if price_df.empty:
            raise ValueError("Price frame is empty. Cannot continue.")
        return price_df

    def make_open_frame(self) -> pd.DataFrame:
        prices = {}
        for ticker, df in self.data.items():
            if "Adjusted Open" in df.columns:
                prices[ticker] = df["Adjusted Open"]
            elif "Open" in df.columns:
                prices[ticker] = df["Open"]
            elif "Adjusted Close" in df.columns:
                prices[ticker] = df["Adjusted Close"]
            elif "Close" in df.columns:
                prices[ticker] = df["Close"]
        result = pd.DataFrame(prices).sort_index()
        if self.config.benchmark in result:
            result = result.reindex(result[self.config.benchmark].dropna().index)
        if result.empty:
            raise ValueError("Open-price frame is empty. Cannot continue.")
        return result

    def make_median_dollar_volume_frame(self, window: int = 60) -> pd.DataFrame:
        values = {}
        for ticker, frame in self.data.items():
            if "Close" in frame and "Volume" in frame:
                values[ticker] = (
                    frame["Close"].astype(float) * frame["Volume"].astype(float)
                ).rolling(window, min_periods=window).median()
        result = pd.DataFrame(values).sort_index()
        if self.config.benchmark in result:
            result = result.reindex(result[self.config.benchmark].index)
        return result

    def make_returns_frame(self, prices: pd.DataFrame) -> pd.DataFrame:
        return prices.pct_change(fill_method=None).replace(
            [np.inf, -np.inf], np.nan
        )

    def compute_features(self, prices: pd.DataFrame, returns: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        features: Dict[str, pd.DataFrame] = {}
        features["mom_20"] = prices / prices.shift(20) - 1.0
        features["mom_60"] = prices / prices.shift(60) - 1.0
        features["mom_120"] = prices / prices.shift(120) - 1.0
        features["vol_20"] = returns.rolling(20).std() * np.sqrt(252)
        features["ma_50"] = prices.rolling(50).mean()
        features["ma_200"] = prices.rolling(200).mean()
        features["drawdown_200"] = prices / features["ma_200"] - 1.0
        features["universe_eligible"] = self._universe_eligibility(prices)
        return features

    def _universe_eligibility(self, prices: pd.DataFrame) -> pd.DataFrame:
        rules = EligibilityRules()
        result = pd.DataFrame(False, index=prices.index, columns=prices.columns)
        for ticker in prices.columns:
            valid_price = prices[ticker].notna()
            if ticker == CASH_ETF:
                result[ticker] = valid_price
                continue
            raw = self.data.get(ticker)
            if raw is None or "Close" not in raw or "Volume" not in raw:
                continue
            close = raw["Close"].astype(float).reindex(prices.index)
            volume = raw["Volume"].astype(float).reindex(prices.index)
            observed = close.notna() & volume.notna()
            observation_count = observed.astype(int).cumsum()
            observed_positions = pd.Series(
                range(len(prices.index)), index=prices.index, dtype=float
            ).where(observed)
            first_position = observed_positions.ffill().where(
                observation_count == 1
            ).ffill()
            expected_count = (
                pd.Series(range(len(prices.index)), index=prices.index, dtype=float)
                - first_position
                + 1.0
            )
            completeness = observation_count / expected_count
            median_dollar_volume = (close * volume).rolling(
                rules.liquidity_window_sessions,
                min_periods=rules.liquidity_window_sessions,
            ).median()
            raw_eligible = (
                (observation_count >= rules.minimum_history_sessions)
                & (median_dollar_volume >= rules.minimum_median_dollar_volume)
                & (close >= rules.minimum_price)
                & (completeness >= rules.minimum_data_completeness)
            )

            # Quarterly changes use information available before the quarter's
            # first session and remain fixed through that quarter.
            prior_session_eligibility = raw_eligible.shift(1, fill_value=False)
            quarter = prices.index.to_period("Q")
            quarterly = prior_session_eligibility.groupby(quarter).transform("first")
            result[ticker] = quarterly.astype(bool)
        return result
