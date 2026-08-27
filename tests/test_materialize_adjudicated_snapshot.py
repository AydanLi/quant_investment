from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import select

from config.settings import Config
from data.models import (
    DataQualityDecision,
    DataQualityDisposition,
    DataQualityReport,
    DataQualityStatus,
    ProviderPayload,
    QualitySeverity,
)
from data.quality import assess_market_data_quality
from scripts.materialize_adjudicated_snapshot import (
    materialize_adjudicated_snapshot,
)
from storage.db import create_all, create_db_engine
from storage.repositories.trusted_data import TrustedMarketDataRepository
from storage.schema import dataset_snapshots


def _bars(values: list[float]) -> pd.DataFrame:
    close = pd.Series(values, index=pd.to_datetime(["2024-01-02", "2024-01-03"]))
    return pd.DataFrame(
        {
            "Open": close,
            "High": close,
            "Low": close,
            "Close": close,
            "Volume": 1_000_000.0,
        }
    )


def test_materialization_creates_derived_snapshot_without_refetch_or_mutation():
    engine = create_db_engine("sqlite:///:memory:")
    create_all(engine)
    repository = TrustedMarketDataRepository(engine=engine)
    primary = ProviderPayload(
        bars={"SPY": _bars([100.0, 101.0]), "^VIX": _bars([15.0, 16.0])},
        actions=(),
        metadata={"SPY": {"source": "tiingo"}, "^VIX": {"source": "cboe"}},
        source="tiingo+cboe",
    )
    secondary = ProviderPayload(
        bars={"SPY": _bars([100.0, 100.0]), "^VIX": _bars([15.0, 16.0])},
        actions=(),
        metadata={"SPY": {"source": "yahoo"}, "^VIX": {"source": "fred_vixcls"}},
        source="yahoo+fred_vixcls",
    )
    config = Config(universe=["SPY"], start_date="2024-01-01")
    report = assess_market_data_quality(
        primary,
        secondary,
        required_tickers=["SPY", "^VIX"],
        config=config,
        as_of=pd.Timestamp("2024-01-03 21:00", tz="America/New_York"),
    )
    assert report.status == DataQualityStatus.BLOCKED
    source_id = repository.create_snapshot(
        report,
        as_of="2024-01-03T21:00:00-05:00",
        start_date="2024-01-01",
        end_date=None,
        bars=primary.bars,
        actions=(),
        source_by_ticker={"SPY": "tiingo", "^VIX": "cboe"},
        secondary_payload=secondary,
    )
    issue = next(
        item for item in report.issues if item.severity == QualitySeverity.BLOCK
    )
    repository.save_quality_decision(
        DataQualityDecision(
            source_snapshot_id=source_id,
            issue_fingerprint=issue.fingerprint,
            issue_code=issue.code,
            ticker=str(issue.ticker),
            start_date=str(issue.session),
            end_date=str(issue.session),
            raw_data_hash=str(report.raw_data_hash),
            disposition=DataQualityDisposition.ACCEPTED_EXCEPTION,
            normalization={"kind": "issue_exception"},
            evidence={"uri": "https://example.test/reviewed-source"},
            reason="Reviewed fixture discrepancy.",
            decided_by="test-operator",
            decided_at=datetime.now(timezone.utc),
        )
    )

    derived_id, derived = materialize_adjudicated_snapshot(
        repository, source_id, config=config
    )

    assert derived_id != source_id
    assert derived.status == DataQualityStatus.TRUSTED_WITH_EXCEPTIONS
    assert derived.source_snapshot_id == source_id
    assert derived.decision_set_hash
    with engine.connect() as connection:
        rows = connection.execute(
            select(dataset_snapshots).order_by(dataset_snapshots.c.id)
        ).mappings().all()
    assert rows[0]["status"] == "BLOCKED"
    assert rows[0]["quality_json"]["source_snapshot_id"] is None
    assert rows[1]["quality_json"]["source_snapshot_id"] == source_id
