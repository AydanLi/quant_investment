"""Offline tests for the MarketDataLoader cache.

No network: the download step is stubbed so these exercise the cache-serve and
cache-bypass paths deterministically.
"""
from __future__ import annotations

import pandas as pd

from config.settings import Config
from data.loader import MarketDataLoader
from storage.db import create_db_engine, create_all
from storage.repositories import MarketDataRepository


def _small_config() -> Config:
    # Tiny universe so the cache only needs a few tickers seeded.
    return Config(universe=["SPY", "QQQ"], benchmark="SPY", fear_gauge="^VIX")


def _seed_prices() -> dict:
    idx = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    frame = lambda base: pd.DataFrame(  # noqa: E731
        {
            "Open": [base, base + 1, base + 2],
            "High": [base, base + 1, base + 2],
            "Low": [base, base + 1, base + 2],
            "Close": [base, base + 1, base + 2],
            "Volume": [100, 110, 120],
        },
        index=idx,
    )
    return {"SPY": frame(400.0), "QQQ": frame(350.0), "^VIX": frame(15.0)}


def _repo_with_seed():
    engine = create_db_engine("sqlite:///:memory:")
    create_all(engine)
    repo = MarketDataRepository(engine=engine)
    repo.upsert_prices(_seed_prices())
    return repo


def test_load_serves_from_cache_without_download():
    repo = _repo_with_seed()
    loader = MarketDataLoader(_small_config(), market_data_repo=repo)
    # Stub the network entirely: no new bars from "download".
    loader._download_and_parse = lambda *a, **k: {}

    result = loader.load()

    assert set(result.keys()) == {"SPY", "QQQ", "^VIX"}
    spy = result["SPY"]
    assert list(spy.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert len(spy) == 3
    assert isinstance(spy.index, pd.DatetimeIndex)
    assert spy["Close"].iloc[-1] == 402.0


def test_cache_disabled_falls_back_to_download():
    expected = _seed_prices()
    loader = MarketDataLoader(_small_config(), use_cache=False)
    assert loader.repo is None
    loader._download_and_parse = lambda *a, **k: expected

    result = loader.load()
    assert set(result.keys()) == {"SPY", "QQQ", "^VIX"}


def test_force_refresh_redownloads_and_caches():
    repo = _repo_with_seed()
    loader = MarketDataLoader(_small_config(), market_data_repo=repo, force_refresh=True)
    captured = {}

    def fake_download(tickers, start, end, strict=True):
        captured["called"] = True
        return _seed_prices()

    loader._download_and_parse = fake_download
    result = loader.load()
    assert captured.get("called") is True  # forced download happened
    assert set(result.keys()) == {"SPY", "QQQ", "^VIX"}


if __name__ == "__main__":
    test_load_serves_from_cache_without_download()
    test_cache_disabled_falls_back_to_download()
    test_force_refresh_redownloads_and_caches()
    print("all loader cache tests passed")
