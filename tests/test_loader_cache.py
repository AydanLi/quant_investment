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
    return Config(
        universe=["SPY", "QQQ"],
        benchmark="SPY",
        fear_gauge="^VIX",
        start_date="2024-01-01",
        end_date="2024-01-05",
    )


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


def test_truncated_cache_refreshes_from_requested_start():
    repo = _repo_with_seed()
    config = Config(
        universe=["SPY", "QQQ"],
        benchmark="SPY",
        fear_gauge="^VIX",
        start_date="2018-01-01",
        end_date="2018-01-05",
    )
    loader = MarketDataLoader(config, market_data_repo=repo)
    expected = _seed_prices()
    historical_index = pd.to_datetime(["2018-01-02", "2018-01-03", "2018-01-04"])
    expected = {
        ticker: frame.set_axis(historical_index, axis="index")
        for ticker, frame in expected.items()
    }
    captured = {}

    def fake_download(tickers, start, end, strict=True):
        captured["start"] = start
        captured["end"] = end
        return expected

    loader._download_and_parse = fake_download
    result = loader.load()

    assert captured == {"start": "2018-01-01", "end": "2018-01-05"}
    assert result["SPY"].index.min() == pd.Timestamp("2018-01-02")


def test_incomplete_download_raises_instead_of_silently_serving_partial_data():
    loader = MarketDataLoader(_small_config(), use_cache=False)
    partial = _seed_prices()
    partial.pop("^VIX")
    loader._download_and_parse = lambda *a, **k: partial

    try:
        loader.load()
    except ValueError as exc:
        assert "^VIX: missing ticker data" in str(exc)
    else:
        raise AssertionError("Expected incomplete market data to raise ValueError")


def test_open_ended_request_rejects_stale_download():
    config = Config(
        universe=["SPY", "QQQ"],
        benchmark="SPY",
        fear_gauge="^VIX",
        start_date="2024-01-01",
        end_date=None,
    )
    loader = MarketDataLoader(config, use_cache=False)
    loader._download_and_parse = lambda *a, **k: _seed_prices()

    try:
        loader.load()
    except ValueError as exc:
        assert "target" in str(exc)
        assert "ends at 2024-01-04" in str(exc)
    else:
        raise AssertionError("Expected stale open-ended data to raise ValueError")


def test_obvious_internal_gap_triggers_full_refresh():
    complete_dates = pd.bdate_range("2024-01-02", "2024-01-12")

    def frame(base, dates):
        return pd.DataFrame(
            {
                "Open": [base] * len(dates),
                "High": [base] * len(dates),
                "Low": [base] * len(dates),
                "Close": [base] * len(dates),
                "Volume": [100] * len(dates),
            },
            index=dates,
        )

    complete = {
        "SPY": frame(400.0, complete_dates),
        "QQQ": frame(350.0, complete_dates),
        "^VIX": frame(15.0, complete_dates),
    }
    cached = dict(complete)
    cached["QQQ"] = complete["QQQ"].drop(complete_dates[2:5])

    engine = create_db_engine("sqlite:///:memory:")
    create_all(engine)
    repo = MarketDataRepository(engine=engine)
    repo.upsert_prices(cached)
    config = Config(
        universe=["SPY", "QQQ"],
        benchmark="SPY",
        fear_gauge="^VIX",
        start_date="2024-01-01",
        end_date="2024-01-13",
    )
    loader = MarketDataLoader(config, market_data_repo=repo)
    calls = []

    def fake_download(tickers, start, end, strict=True):
        calls.append(start)
        if start == "2024-01-12":
            return {}
        return complete

    loader._download_and_parse = fake_download
    result = loader.load()

    assert calls == ["2024-01-12", "2024-01-01"]
    assert len(result["QQQ"]) == len(complete_dates)


if __name__ == "__main__":
    test_load_serves_from_cache_without_download()
    test_cache_disabled_falls_back_to_download()
    test_force_refresh_redownloads_and_caches()
    test_truncated_cache_refreshes_from_requested_start()
    test_incomplete_download_raises_instead_of_silently_serving_partial_data()
    test_open_ended_request_rejects_stale_download()
    test_obvious_internal_gap_triggers_full_refresh()
    print("all loader cache tests passed")
