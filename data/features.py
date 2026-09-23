from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd

from config.settings import Config
from config.universe import CASH_ETF, EligibilityRules
from data.calendar import NyseCalendar
from data.models import CorporateAction
from data.adjustments import locally_adjust_ohlcv


class FeatureEngineer:
    def __init__(self, data: Dict[str, pd.DataFrame], config: Config):
        self.data = data
        self.config = config

    def make_price_frame(self) -> pd.DataFrame:
        prices = {}
        for ticker, df in self.data.items():
            if "Adjusted Close" in df:
                prices[ticker] = df["Adjusted Close"]
            elif ticker == self.config.fear_gauge and "Close" in df:
                prices[ticker] = df["Close"]
            elif "Close" in df and "corporate_actions" in df.attrs:
                prices[ticker] = locally_adjust_ohlcv(df, df.attrs["corporate_actions"])["Adjusted Close"]
            else:
                raise ValueError(f"{ticker} signal prices require total-return data or an explicit corporate-action history.")
        price_df = self._align_calendar(pd.DataFrame(prices).sort_index())
        if price_df.empty:
            raise ValueError("Price frame is empty. Cannot continue.")
        price_df.attrs["price_basis"] = "total_return"
        return price_df

    def make_open_frame(self) -> pd.DataFrame:
        prices = {}
        for ticker, df in self.data.items():
            if "Open" in df.columns:
                prices[ticker] = df["Open"]
        result = self._align_calendar(pd.DataFrame(prices).sort_index())
        if result.empty:
            raise ValueError("Open-price frame is empty. Cannot continue.")
        result.attrs["price_basis"] = "raw"
        return result

    def make_raw_close_frame(self) -> pd.DataFrame:
        result = self._align_calendar(pd.DataFrame({
            ticker: frame["Close"] for ticker, frame in self.data.items() if "Close" in frame
        }).sort_index())
        if result.empty:
            raise ValueError("Raw Close-price frame is empty. Cannot continue.")
        result.attrs["price_basis"] = "raw"
        return result

    def corporate_actions(self) -> tuple[CorporateAction, ...]:
        actions: dict[str, CorporateAction] = {}
        for frame in self.data.values():
            for raw in frame.attrs.get("corporate_actions", ()):
                action = raw.normalized()
                previous = actions.get(action.action_key)
                if previous is not None and previous.revision_hash != action.revision_hash:
                    raise ValueError(f"Conflicting corporate action revision: {action.action_key}.")
                actions[action.action_key] = action
        return tuple(actions.values())

    def _align_calendar(self, frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            return frame
        benchmark = self.data.get(self.config.benchmark)
        index = benchmark.index if benchmark is not None and not benchmark.empty else frame.index
        sessions = NyseCalendar().sessions(index.min(), index.max())
        return frame.reindex(sessions)

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
        features["ma200_deviation"] = prices / features["ma_200"] - 1.0
        # Historical report compatibility; this is MA deviation, not drawdown.
        features["drawdown_200"] = features["ma200_deviation"]
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
