from __future__ import annotations

from abc import ABC, abstractmethod
from io import StringIO
import os
from typing import Mapping, Sequence

import pandas as pd
import requests
import yfinance as yf

from data.models import CorporateAction, ProviderPayload


_OHLCV_RENAME = {
    "open": "Open",
    "high": "High",
    "low": "Low",
    "close": "Close",
    "volume": "Volume",
}


class ProviderError(RuntimeError):
    pass


class ProviderRateLimitError(ProviderError):
    """Raised when a provider refuses a request because its quota is exhausted."""

    def __init__(self, provider: str, retry_after: str | None = None) -> None:
        self.provider = provider
        self.retry_after = retry_after
        suffix = f" Retry-After={retry_after}." if retry_after else ""
        super().__init__(
            f"{provider} rate limit reached; retry after the provider quota resets.{suffix}"
        )


class MarketDataProvider(ABC):
    name: str

    @abstractmethod
    def fetch_bars(
        self,
        tickers: Sequence[str],
        start: str | None,
        end: str | None,
    ) -> Mapping[str, pd.DataFrame]:
        raise NotImplementedError

    @abstractmethod
    def fetch_actions(
        self,
        tickers: Sequence[str],
        start: str | None,
        end: str | None,
    ) -> tuple[CorporateAction, ...]:
        raise NotImplementedError

    def fetch_metadata(
        self,
        tickers: Sequence[str],
    ) -> Mapping[str, Mapping[str, object]]:
        return {ticker: {} for ticker in tickers}

    def fetch(
        self,
        tickers: Sequence[str],
        start: str | None,
        end: str | None,
    ) -> ProviderPayload:
        return ProviderPayload(
            bars=self.fetch_bars(tickers, start, end),
            actions=self.fetch_actions(tickers, start, end),
            metadata=self.fetch_metadata(tickers),
            source=self.name,
        )


class YahooMarketDataProvider(MarketDataProvider):
    name = "yahoo"

    @staticmethod
    def _extract_ticker_frame(data: pd.DataFrame, ticker: str) -> pd.DataFrame:
        if data.empty:
            return pd.DataFrame()
        if isinstance(data.columns, pd.MultiIndex):
            first = data.columns.get_level_values(0)
            second = data.columns.get_level_values(1)
            if ticker in first:
                frame = data[ticker].copy()
            elif ticker in second:
                frame = data.xs(ticker, axis=1, level=1).copy()
            else:
                return pd.DataFrame()
        else:
            frame = data.copy()
        frame = frame.rename(columns=lambda value: str(value).title())
        columns = [name for name in ("Open", "High", "Low", "Close", "Volume") if name in frame]
        frame = frame[columns].dropna(how="all")
        if not frame.empty:
            frame.index = pd.DatetimeIndex(frame.index).tz_localize(None).normalize()
        return frame

    def fetch_bars(self, tickers, start, end):  # noqa: ANN001
        data = yf.download(
            tickers=list(tickers),
            start=start,
            end=end,
            auto_adjust=False,
            actions=False,
            progress=False,
            group_by="ticker",
            threads=True,
        )
        result = {
            ticker: self._extract_ticker_frame(data, ticker)
            for ticker in tickers
        }
        return {ticker: frame for ticker, frame in result.items() if not frame.empty}

    def fetch_actions(self, tickers, start, end):  # noqa: ANN001
        actions: list[CorporateAction] = []
        for ticker in tickers:
            if ticker.startswith("^"):
                continue
            try:
                history = yf.Ticker(ticker).history(
                    start=start,
                    end=end,
                    auto_adjust=False,
                    actions=True,
                )
            except Exception as exc:  # provider boundary
                raise ProviderError(f"Yahoo actions failed for {ticker}: {exc}") from exc
            if history.empty:
                continue
            for session, value in history.get("Dividends", pd.Series(dtype=float)).items():
                if pd.notna(value) and float(value) != 0.0:
                    actions.append(
                        CorporateAction(ticker, session, "dividend", cash_amount=float(value), source=self.name)
                    )
            for session, value in history.get("Stock Splits", pd.Series(dtype=float)).items():
                if pd.notna(value) and float(value) not in (0.0, 1.0):
                    actions.append(
                        CorporateAction(ticker, session, "split", split_factor=float(value), source=self.name)
                    )
        return tuple(action.normalized() for action in actions)


class TiingoMarketDataProvider(MarketDataProvider):
    name = "tiingo"
    base_url = "https://api.tiingo.com/tiingo/daily"

    def __init__(
        self,
        token: str | None = None,
        session: requests.Session | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.token = token or os.getenv("TIINGO_API_TOKEN")
        if not self.token:
            raise ProviderError("TIINGO_API_TOKEN is required for trusted market data.")
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds
        self._payload_cache: dict[tuple[str, str | None, str | None], list[dict]] = {}

    def _prices(self, ticker: str, start: str | None, end: str | None) -> list[dict]:
        key = (ticker, start, end)
        if key in self._payload_cache:
            return self._payload_cache[key]
        params = {"resampleFreq": "daily", "format": "json"}
        if start:
            params["startDate"] = start
        if end:
            params["endDate"] = end
        response = self.session.get(
            f"{self.base_url}/{ticker}/prices",
            params=params,
            headers={"Authorization": f"Token {self.token}"},
            timeout=self.timeout_seconds,
        )
        if response.status_code == 429:
            raise ProviderRateLimitError(
                self.name,
                retry_after=response.headers.get("Retry-After"),
            )
        if response.status_code != 200:
            raise ProviderError(f"Tiingo prices failed for {ticker}: HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, list):
            raise ProviderError(f"Tiingo returned an invalid price payload for {ticker}.")
        self._payload_cache[key] = payload
        return payload

    def fetch_bars(self, tickers, start, end):  # noqa: ANN001
        result: dict[str, pd.DataFrame] = {}
        for ticker in tickers:
            payload = self._prices(ticker, start, end)
            if not payload:
                continue
            frame = pd.DataFrame(payload)
            if "date" not in frame:
                continue
            frame["date"] = pd.to_datetime(frame["date"], utc=True).dt.tz_localize(None).dt.normalize()
            frame = frame.set_index("date").sort_index().rename(columns=_OHLCV_RENAME)
            columns = [name for name in ("Open", "High", "Low", "Close", "Volume") if name in frame]
            result[ticker] = frame[columns].apply(pd.to_numeric, errors="coerce").dropna(how="all")
        return result

    def fetch_actions(self, tickers, start, end):  # noqa: ANN001
        actions: list[CorporateAction] = []
        for ticker in tickers:
            if ticker.startswith("^"):
                continue
            for row in self._prices(ticker, start, end):
                session = pd.Timestamp(row["date"])
                dividend = float(row.get("divCash") or 0.0)
                split = float(row.get("splitFactor") or 1.0)
                if dividend != 0.0:
                    actions.append(
                        CorporateAction(ticker, session, "dividend", cash_amount=dividend, source=self.name)
                    )
                if split not in (0.0, 1.0):
                    actions.append(
                        CorporateAction(ticker, session, "split", split_factor=split, source=self.name)
                    )
        return tuple(action.normalized() for action in actions)

    def fetch_metadata(self, tickers):  # noqa: ANN001
        result: dict[str, Mapping[str, object]] = {}
        for ticker in tickers:
            response = self.session.get(
                f"{self.base_url}/{ticker}",
                headers={"Authorization": f"Token {self.token}"},
                timeout=self.timeout_seconds,
            )
            if response.status_code == 429:
                raise ProviderRateLimitError(
                    self.name,
                    retry_after=response.headers.get("Retry-After"),
                )
            if response.status_code != 200:
                raise ProviderError(f"Tiingo metadata failed for {ticker}: HTTP {response.status_code}")
            payload = response.json()
            result[ticker] = payload if isinstance(payload, dict) else {}
        return result


class CboeVixProvider:
    name = "cboe"
    history_url = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"

    def __init__(self, session: requests.Session | None = None, timeout_seconds: float = 30.0) -> None:
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds

    def fetch(self, start: str | None = None, end: str | None = None) -> pd.DataFrame:
        response = self.session.get(self.history_url, timeout=self.timeout_seconds)
        if response.status_code != 200:
            raise ProviderError(f"CBOE VIX history failed: HTTP {response.status_code}")
        frame = pd.read_csv(StringIO(response.text))
        frame.columns = [str(column).strip().title() for column in frame.columns]
        date_column = "Date" if "Date" in frame else frame.columns[0]
        frame[date_column] = pd.to_datetime(frame[date_column])
        frame = frame.set_index(date_column).sort_index()
        frame = frame.rename(columns={"Vix Open": "Open", "Vix High": "High", "Vix Low": "Low", "Vix Close": "Close"})
        if start:
            frame = frame.loc[pd.Timestamp(start):]
        if end:
            frame = frame.loc[:pd.Timestamp(end)]
        frame["Volume"] = 0.0
        return frame[["Open", "High", "Low", "Close", "Volume"]]


class FredRiskFreeProvider:
    name = "fred_dgs3mo"
    csv_url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS3MO"

    def __init__(self, session: requests.Session | None = None, timeout_seconds: float = 30.0) -> None:
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds

    def fetch_daily_returns(self, start: str | None = None, end: str | None = None) -> pd.Series:
        response = self.session.get(self.csv_url, timeout=self.timeout_seconds)
        if response.status_code != 200:
            raise ProviderError(f"FRED DGS3MO failed: HTTP {response.status_code}")
        frame = pd.read_csv(StringIO(response.text))
        frame["DATE"] = pd.to_datetime(frame["DATE"])
        values = pd.to_numeric(frame["DGS3MO"], errors="coerce") / 100.0
        annual_yield = pd.Series(values.to_numpy(), index=frame["DATE"], name="DGS3MO").sort_index().ffill()
        if start:
            annual_yield = annual_yield.loc[pd.Timestamp(start):]
        if end:
            annual_yield = annual_yield.loc[:pd.Timestamp(end)]
        return (1.0 + annual_yield).pow(1.0 / 252.0) - 1.0
