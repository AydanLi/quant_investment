from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd
import yfinance as yf

from config.settings import Config

_OHLCV = ["Open", "High", "Low", "Close", "Volume"]
_CACHE_TO_TITLE = {
    "open": "Open",
    "high": "High",
    "low": "Low",
    "close": "Close",
    "volume": "Volume",
}


class MarketDataLoader:
    """Loads OHLCV data, backed by a local cache.

    On first use the requested range is downloaded from yfinance and written to
    the ``market_data`` table; subsequent loads are served from the cache, with
    only the recent tail re-downloaded to stay fresh. This makes backtests
    reproducible and avoids hammering the upstream API.

    Caching is best-effort: any cache read/write failure falls back to a plain
    download, so the pipeline never breaks because of the cache. Pass
    ``use_cache=False`` to bypass it entirely, or ``force_refresh=True`` to
    re-download the full range and overwrite the cache.
    """

    def __init__(
        self,
        config: Config,
        market_data_repo=None,
        use_cache: bool = True,
        force_refresh: bool = False,
    ):
        self.config = config
        self.force_refresh = force_refresh
        self.repo = None
        if use_cache:
            if market_data_repo is not None:
                self.repo = market_data_repo
            else:
                # Lazy import keeps the data layer importable without storage.
                try:
                    from storage.db import create_all
                    from storage.repositories import MarketDataRepository

                    repo = MarketDataRepository(db_url=config.db_url)
                    # Ensure the cache table exists now: the loader runs before
                    # the app's store.init_db(), so on a fresh database the
                    # market_data table would otherwise be missing and every
                    # cache write would silently no-op.
                    create_all(repo.engine)
                    self.repo = repo
                except Exception:
                    self.repo = None

    # -- public API ---------------------------------------------------------- #
    def load(self) -> Dict[str, pd.DataFrame]:
        tickers = sorted(
            set(self.config.universe + [self.config.benchmark, self.config.fear_gauge])
        )

        if self.repo is None:
            return self._download_and_parse(tickers, self.config.start_date, self.config.end_date)

        # Inspect what the cache already holds for these tickers.
        try:
            coverages = {t: self.repo.coverage(t) for t in tickers}
        except Exception:
            # Cache unreadable (e.g. table not created yet) -> plain download.
            return self._download_parse_and_cache(
                tickers, self.config.start_date, self.config.end_date
            )

        fully_cached = all(cov[0] is not None for cov in coverages.values())

        if self.force_refresh or not fully_cached:
            # Missing a ticker (or forced): refresh the whole range.
            return self._download_parse_and_cache(
                tickers, self.config.start_date, self.config.end_date
            )

        # Incremental top-up: download only from the laggard's last cached bar.
        last_cached = min(cov[1] for cov in coverages.values())
        downloaded = self._download_and_parse(tickers, last_cached, self.config.end_date, strict=False)
        if downloaded:
            self._cache_write(downloaded)

        served = self._frames_from_cache(tickers, self.config.start_date, self.config.end_date)
        if not served:
            # Cache somehow yielded nothing usable -> fall back to a full download.
            return self._download_parse_and_cache(
                tickers, self.config.start_date, self.config.end_date
            )
        return served

    # -- download ------------------------------------------------------------ #
    def _download_and_parse(
        self,
        tickers: List[str],
        start: Optional[str],
        end: Optional[str],
        strict: bool = True,
    ) -> Dict[str, pd.DataFrame]:
        data = yf.download(
            tickers=tickers,
            start=start,
            end=end,
            auto_adjust=True,
            progress=False,
            group_by="ticker",
            threads=True,
        )

        if data.empty:
            if strict:
                raise ValueError(
                    "No market data returned. Check ticker list, dates, or network connection."
                )
            return {}

        result: Dict[str, pd.DataFrame] = {}
        for t in tickers:
            try:
                if t in data.columns.get_level_values(0):
                    df = data[t].copy()
                else:
                    cols = [c for c in data.columns if isinstance(c, tuple) and c[0] == t]
                    if cols:
                        df = data[cols].copy()
                        df.columns = [c[1] for c in cols]
                    else:
                        continue

                df = df.rename(columns=str.title)
                keep_cols = [c for c in _OHLCV if c in df.columns]
                df = df[keep_cols].dropna(how="all")
                if not df.empty:
                    result[t] = df
            except Exception:
                continue

        if not result and strict:
            raise ValueError("Failed to parse downloaded market data.")

        return result

    def _download_parse_and_cache(
        self, tickers: List[str], start: Optional[str], end: Optional[str]
    ) -> Dict[str, pd.DataFrame]:
        result = self._download_and_parse(tickers, start, end)
        self._cache_write(result)
        return result

    # -- cache helpers ------------------------------------------------------- #
    def _cache_write(self, frames: Dict[str, pd.DataFrame]) -> None:
        if self.repo is None or not frames:
            return
        try:
            self.repo.upsert_prices(frames, auto_adjusted=True, source="yfinance")
        except Exception:
            # Caching is an optimization; never let it break the load.
            pass

    def _frames_from_cache(
        self, tickers: List[str], start: Optional[str], end: Optional[str]
    ) -> Dict[str, pd.DataFrame]:
        try:
            df = self.repo.get_prices(tickers, start=start, end=end)
        except Exception:
            return {}
        if df.empty:
            return {}

        result: Dict[str, pd.DataFrame] = {}
        for ticker, group in df.groupby("ticker"):
            frame = group[["date", "open", "high", "low", "close", "volume"]].copy()
            frame["date"] = pd.to_datetime(frame["date"])
            frame = frame.set_index("date").sort_index()
            frame = frame.rename(columns=_CACHE_TO_TITLE)
            frame = frame[[c for c in _OHLCV if c in frame.columns]].dropna(how="all")
            if not frame.empty:
                result[str(ticker)] = frame
        return result
