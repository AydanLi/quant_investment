from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from config.settings import Config
from data.features import FeatureEngineer
from storage.db import create_all, create_db_engine
from storage.repositories import MarketDataRepository


def test_returns_do_not_fill_missing_prices_and_resume_after_valid_pair():
    index = pd.date_range("2026-01-02", periods=4, freq="B")
    prices = pd.DataFrame(
        {"SPY": [100.0, None, 110.0, 121.0]},
        index=index,
    )

    returns = FeatureEngineer({}, Config()).make_returns_frame(prices)

    assert pd.isna(returns.loc[index[1], "SPY"])
    assert pd.isna(returns.loc[index[2], "SPY"])
    assert returns.loc[index[3], "SPY"] == pytest.approx(0.10)


def test_market_data_timestamp_is_created_in_utc_and_serialized_consistently():
    engine = create_db_engine("sqlite:///:memory:")
    create_all(engine)
    repository = MarketDataRepository(engine=engine)
    fixed_utc = datetime(2026, 7, 15, 22, 30, 45, tzinfo=timezone.utc)
    frame = pd.DataFrame(
        {
            "Open": [100.0],
            "High": [101.0],
            "Low": [99.0],
            "Close": [100.5],
            "Volume": [1000],
        },
        index=pd.to_datetime(["2026-07-15"]),
    )

    with patch("storage.repositories.market_data.datetime") as datetime_mock:
        datetime_mock.now.return_value = fixed_utc
        repository.upsert_prices({"SPY": frame})

    datetime_mock.now.assert_called_once_with(timezone.utc)
    stored = repository.get_prices(["SPY"])["fetched_at"].iloc[0]
    assert stored == pd.Timestamp(fixed_utc.replace(tzinfo=None))
