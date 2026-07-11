from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd
import yfinance as yf

from config.settings import Config

_OHLCV = ["Open", "High", "Low", "Close", "Volume"]
_START_BOUNDARY_TOLERANCE_DAYS = 4
_END_FRESHNESS_TOLERANCE_DAYS = 7
_MAX_CONSECUTIVE_MISSING_SESSIONS = 2
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
    download. Completeness is strict: all requested tickers must cover the
    requested boundaries and must not contain an obvious multi-session gap.
    Pass ``use_cache=False`` to bypass the cache entirely, or
    ``force_refresh=True`` to re-download the full range and overwrite it.
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
            result = self._download_and_parse(
                tickers, self.config.start_date, self.config.end_date
            )
            self._assert_complete(
                result, tickers, self.config.start_date, self.config.end_date
            )
            return result

        # Inspect what the cache already holds for these tickers.
        try:
            coverages = {t: self.repo.coverage(t) for t in tickers}
        except Exception:
            # Cache unreadable (e.g. table not created yet) -> plain download.
            return self._download_parse_and_cache(
                tickers, self.config.start_date, self.config.end_date
            )

        has_all_tickers = all(
            cov[0] is not None and cov[1] is not None for cov in coverages.values()
        )
        covers_requested_start = has_all_tickers and all(
            self._covers_start(cov[0], self.config.start_date)
            for cov in coverages.values()
        )

        if self.force_refresh or not has_all_tickers or not covers_requested_start:
            # Missing/truncated ticker history (or forced): refresh the full range.
            return self._download_parse_and_cache(
                tickers, self.config.start_date, self.config.end_date
            )

        # Incremental top-up: download only from the laggard's last cached bar.
        last_cached = min(cov[1] for cov in coverages.values())
        downloaded = self._download_and_parse(tickers, last_cached, self.config.end_date, strict=False)
        if downloaded:
            self._cache_write(downloaded)

        served = self._frames_from_cache(
            tickers, self.config.start_date, self.config.end_date
        )
        if self._coverage_issues(
            served, tickers, self.config.start_date, self.config.end_date
        ):
            # A stale tail, missing ticker, or obvious internal gap requires a
            # full refresh. The strict validation in this path raises if the
            # upstream download is still incomplete.
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
        self._assert_complete(result, tickers, start, end)
        self._cache_write(result)
        return result

    # -- cache helpers ------------------------------------------------------- #
    @staticmethod
    def _normalized_timestamp(value: str) -> pd.Timestamp:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is not None:
            timestamp = timestamp.tz_convert(None)
        return timestamp.normalize()

    @classmethod
    def _normalized_index(cls, frame: pd.DataFrame) -> pd.DatetimeIndex:
        index = pd.DatetimeIndex(pd.to_datetime(frame.index))
        if index.tz is not None:
            index = index.tz_convert(None)
        return index.normalize().unique().sort_values()

    @classmethod
    def _covers_start(cls, cached_start: str, requested_start: Optional[str]) -> bool:
        if requested_start is None:
            return True
        actual = cls._normalized_timestamp(cached_start)
        requested = cls._normalized_timestamp(requested_start)
        tolerance = pd.Timedelta(days=_START_BOUNDARY_TOLERANCE_DAYS)
        return actual <= requested + tolerance

    def _coverage_issues(
        self,
        frames: Dict[str, pd.DataFrame],
        tickers: List[str],
        start: Optional[str],
        end: Optional[str],
    ) -> List[str]:
        issues: List[str] = []
        indexes: Dict[str, pd.DatetimeIndex] = {}
        start_boundary = (
            self._normalized_timestamp(start) if start is not None else None
        )
        today = pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()
        end_target = self._normalized_timestamp(end) if end is not None else today
        start_tolerance = pd.Timedelta(days=_START_BOUNDARY_TOLERANCE_DAYS)
        end_tolerance = pd.Timedelta(days=_END_FRESHNESS_TOLERANCE_DAYS)

        for ticker in tickers:
            frame = frames.get(ticker)
            if frame is None or frame.empty:
                issues.append(f"{ticker}: missing ticker data")
                continue
            if "Close" not in frame.columns:
                issues.append(f"{ticker}: missing Close column")
                continue

            index = self._normalized_index(frame)
            if index.empty:
                issues.append(f"{ticker}: empty date index")
                continue
            indexes[ticker] = index

            if (
                start_boundary is not None
                and index[0] > start_boundary + start_tolerance
            ):
                issues.append(
                    f"{ticker}: starts at {index[0].date()}, requested {start_boundary.date()}"
                )
            if index[-1] < end_target - end_tolerance:
                issues.append(
                    f"{ticker}: ends at {index[-1].date()}, target {end_target.date()}"
                )

        reference = indexes.get(self.config.benchmark)
        if reference is None or reference.empty:
            return issues
        if start_boundary is not None:
            reference = reference[reference >= start_boundary]
        if end is not None:
            reference = reference[reference < end_target]

        for ticker, index in indexes.items():
            if ticker == self.config.benchmark:
                continue
            available = set(index)
            max_missing_run = 0
            missing_run = 0
            for date in reference:
                if date in available:
                    missing_run = 0
                else:
                    missing_run += 1
                    max_missing_run = max(max_missing_run, missing_run)
            if max_missing_run > _MAX_CONSECUTIVE_MISSING_SESSIONS:
                issues.append(
                    f"{ticker}: {max_missing_run} consecutive benchmark sessions missing"
                )

        return issues

    def _assert_complete(
        self,
        frames: Dict[str, pd.DataFrame],
        tickers: List[str],
        start: Optional[str],
        end: Optional[str],
    ) -> None:
        issues = self._coverage_issues(frames, tickers, start, end)
        if issues:
            raise ValueError("Incomplete market data: " + "; ".join(issues))

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
