from __future__ import annotations

import pandas as pd
from sqlalchemy import select

from config.settings import Config
from data.models import DataQualityReport, DataQualityStatus, ProviderPayload
from scripts.requalify_snapshot import requalify_snapshot
from storage.db import create_all, create_db_engine
from storage.repositories.trusted_data import TrustedMarketDataRepository
from storage.schema import dataset_snapshots


def _bars(values):
    close = pd.Series(
        values,
        index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
        dtype=float,
    )
    return pd.DataFrame(
        {
            "Open": close,
            "High": close,
            "Low": close,
            "Close": close,
            "Volume": 1_000_000.0,
        }
    )


def test_requalification_creates_new_raw_hash_without_mutating_source():
    engine = create_db_engine("sqlite:///:memory:")
    create_all(engine)
    repository = TrustedMarketDataRepository(engine=engine)
    bars = {"SPY": _bars([100.0, 101.0]), "^VIX": _bars([15.0, 16.0])}
    legacy_report = DataQualityReport(
        status=DataQualityStatus.BLOCKED,
        primary_source="tiingo+cboe",
        secondary_source="yahoo+fred_vixcls",
        expected_session="2024-01-03",
        latest_session="2024-01-03",
        stale_sessions=0,
        content_hash="a" * 64,
    )
    secondary = ProviderPayload(
        bars=bars,
        actions=(),
        metadata={ticker: {"source": "yahoo"} for ticker in bars},
        source="yahoo+fred_vixcls",
    )
    source_id = repository.create_snapshot(
        legacy_report,
        as_of="2024-01-03T21:00:00-05:00",
        start_date="2024-01-01",
        end_date=None,
        bars=bars,
        actions=(),
        source_by_ticker={"SPY": "tiingo", "^VIX": "cboe"},
        secondary_payload=secondary,
    )

    new_id, report = requalify_snapshot(
        repository,
        source_id,
        config=Config(universe=["SPY"], start_date="2024-01-01"),
    )

    assert new_id != source_id
    assert report.raw_data_hash
    with engine.connect() as connection:
        rows = connection.execute(
            select(dataset_snapshots).order_by(dataset_snapshots.c.id)
        ).mappings().all()
    assert rows[0]["content_hash"] == "a" * 64
    assert rows[0]["status"] == "BLOCKED"
    assert rows[1]["quality_json"]["raw_data_hash"] == report.raw_data_hash
    assert rows[1]["quality_json"]["source_snapshot_id"] is None

