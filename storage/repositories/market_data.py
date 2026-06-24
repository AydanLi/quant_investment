"""Cached OHLCV market data, shared across runs.

Lets the data loader download each (ticker, date) bar once and reuse it,
making backtests reproducible and sparing the upstream API. Keyed on
(ticker, date); re-fetching the same bar updates it in place via a
dialect-portable upsert.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping, Optional

import pandas as pd
from sqlalchemy import func, select

from storage.repositories.base import BaseRepository, upsert
from storage.schema import market_data

# Loader columns (Title-case) -> schema columns.
_OHLCV_MAP = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Volume": "volume",
}
_UPDATE_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "auto_adjusted",
    "source",
    "fetched_at",
)


def _date_str(value: Any) -> str:
    return str(value.date()) if hasattr(value, "date") else str(value)


def _opt_float(value: Any) -> Optional[float]:
    return float(value) if value is not None and pd.notna(value) else None


class MarketDataRepository(BaseRepository):
    def upsert_prices(
        self,
        prices: Mapping[str, pd.DataFrame],
        auto_adjusted: bool = True,
        source: str = "yfinance",
    ) -> int:
        """Upsert a ``{ticker: OHLCV DataFrame}`` mapping (the loader's format).

        Each frame is indexed by date with Title-case OHLCV columns. Returns the
        number of (ticker, date) rows written.
        """
        fetched_at = datetime.utcnow()
        rows = []
        for ticker, frame in prices.items():
            if frame is None or frame.empty:
                continue
            for date, row in frame.iterrows():
                record = {
                    "ticker": ticker,
                    "date": _date_str(date),
                    "auto_adjusted": 1 if auto_adjusted else 0,
                    "source": source,
                    "fetched_at": fetched_at,
                }
                for src_col, dst_col in _OHLCV_MAP.items():
                    record[dst_col] = _opt_float(row.get(src_col))
                rows.append(record)

        if not rows:
            return 0

        with self.engine.begin() as conn:
            upsert(
                conn,
                market_data,
                rows,
                index_elements=["ticker", "date"],
                update_columns=_UPDATE_COLUMNS,
            )
        return len(rows)

    def get_prices(
        self,
        tickers: Optional[Iterable[str]] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> pd.DataFrame:
        """Cached bars as a long DataFrame, optionally filtered."""
        stmt = select(market_data)
        if tickers is not None:
            stmt = stmt.where(market_data.c.ticker.in_(list(tickers)))
        if start is not None:
            stmt = stmt.where(market_data.c.date >= start)
        if end is not None:
            stmt = stmt.where(market_data.c.date <= end)
        stmt = stmt.order_by(market_data.c.ticker, market_data.c.date)
        with self.engine.connect() as conn:
            return pd.read_sql(stmt, conn)

    def get_close_frame(
        self,
        tickers: Optional[Iterable[str]] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> pd.DataFrame:
        """Cached closes pivoted to a date x ticker frame."""
        df = self.get_prices(tickers, start, end)
        if df.empty:
            return df
        return df.pivot(index="date", columns="ticker", values="close").sort_index()

    def coverage(self, ticker: str) -> tuple[Optional[str], Optional[str]]:
        """``(min_date, max_date)`` cached for a ticker, or ``(None, None)``.

        Lets the loader decide what date ranges still need downloading.
        """
        stmt = select(
            func.min(market_data.c.date), func.max(market_data.c.date)
        ).where(market_data.c.ticker == ticker)
        with self.engine.connect() as conn:
            row = conn.execute(stmt).one()
        return row[0], row[1]
