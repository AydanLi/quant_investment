from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import select

from config.settings import Config
from data.models import (
    DATA_QUALITY_MODEL_VERSION,
    CorporateAction,
    DataQualityDecision,
    DataQualityDisposition,
    DataQualityIssue,
    DataQualityReport,
    DataQualityStatus,
    ProviderPayload,
    QualitySeverity,
)
from data.quality import apply_data_quality_decisions, assess_market_data_quality
from data.trusted_loader import TrustedMarketDataLoader
from storage.db import create_all, create_db_engine
from storage.repositories.trusted_data import TrustedMarketDataRepository
from storage.schema import dataset_snapshots, universe_versions


NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


def _bars(values, dates):
    close = pd.Series(values, index=pd.to_datetime(dates), dtype=float)
    return pd.DataFrame(
        {
            "Open": close,
            "High": close,
            "Low": close,
            "Close": close,
            "Volume": 1_000_000.0,
        }
    )


def _report():
    issue = DataQualityIssue(
        QualitySeverity.BLOCK,
        "CROSS_SOURCE_CLOSE_MISMATCH",
        "wording one",
        ticker="spy",
        session="2024-01-02",
        value=30.0,
    )
    return DataQualityReport(
        status=DataQualityStatus.BLOCKED,
        primary_source="primary",
        secondary_source="secondary",
        expected_session="2024-01-02",
        latest_session="2024-01-02",
        stale_sessions=0,
        issues=(issue,),
        content_hash="a" * 64,
        raw_data_hash="b" * 64,
        quality_model_version=DATA_QUALITY_MODEL_VERSION,
    )


def _decision(report, *, snapshot_id=1, issue=None, normalization=None):
    issue = issue or report.issues[0]
    return DataQualityDecision(
        source_snapshot_id=snapshot_id,
        issue_fingerprint=issue.fingerprint,
        issue_code=issue.code,
        ticker=issue.ticker,
        start_date=issue.session,
        end_date=issue.session,
        raw_data_hash=report.raw_data_hash,
        disposition=DataQualityDisposition.ACCEPTED_EXCEPTION,
        normalization=normalization or {"kind": "issue_exception"},
        evidence={"uri": "https://example.test/official-record"},
        reason="Documented provider representation difference.",
        decided_by="operator",
        decided_at=NOW,
    )


def test_issue_fingerprint_is_deterministic_and_distinguishes_context():
    first = _report().issues[0]
    same = replace(first, message="different wording")
    different = replace(first, context={"action_type": "split"})

    assert first.fingerprint == same.fingerprint
    assert first.fingerprint != different.fingerprint


def test_decision_application_is_exact_and_raw_data_changes_invalidate_it():
    report = _report()
    decision = _decision(report)

    resolved = apply_data_quality_decisions(
        report, [decision], source_snapshot_id=1
    )

    assert resolved.status == DataQualityStatus.TRUSTED_WITH_EXCEPTIONS
    assert resolved.actionable is True
    assert resolved.source_snapshot_id == 1
    assert resolved.decision_set_hash
    assert resolved.adjudicated_issue_fingerprints == (
        report.issues[0].fingerprint,
    )
    with pytest.raises(ValueError, match="stale"):
        apply_data_quality_decisions(
            replace(report, raw_data_hash="c" * 64),
            [decision],
            source_snapshot_id=1,
        )
    with pytest.raises(ValueError, match="all-ticker"):
        replace(decision, ticker="*")


def test_repository_keeps_decisions_immutable_within_one_source_snapshot():
    engine = create_db_engine("sqlite:///:memory:")
    create_all(engine)
    repository = TrustedMarketDataRepository(engine=engine)
    report = _report()
    bars = {"SPY": _bars([100.0], ["2024-01-02"])}
    snapshot_id = repository.create_snapshot(
        report,
        as_of="2024-01-02T21:00:00+00:00",
        start_date="2024-01-02",
        end_date="2024-01-02",
        bars=bars,
        actions=(),
        source_by_ticker={"SPY": "primary"},
    )
    decision = _decision(report, snapshot_id=snapshot_id)

    first_id = repository.save_quality_decision(decision)

    assert repository.save_quality_decision(decision) == first_id
    assert repository.quality_decisions(snapshot_id) == (decision,)
    with pytest.raises(ValueError, match="immutable"):
        repository.save_quality_decision(replace(decision, reason="Changed later."))
    with pytest.raises(TypeError):
        decision.evidence["uri"] = "https://example.test/changed"

    revised_report = replace(
        report,
        content_hash="c" * 64,
        raw_data_hash="d" * 64,
    )
    revised_snapshot_id = repository.create_snapshot(
        revised_report,
        as_of="2024-01-03T21:00:00+00:00",
        start_date="2024-01-02",
        end_date="2024-01-02",
        bars=bars,
        actions=(),
        source_by_ticker={"SPY": "primary"},
    )
    revised_decision = replace(
        decision,
        source_snapshot_id=revised_snapshot_id,
        raw_data_hash=revised_report.raw_data_hash,
    )
    assert repository.save_quality_decision(revised_decision) != first_id
    assert repository.quality_decisions(revised_snapshot_id) == (revised_decision,)


def test_bounded_corporate_action_normalization_resolves_only_derivative_closes():
    dates = ["2016-09-15", "2016-09-16", "2016-09-19", "2016-09-20"]
    primary = ProviderPayload(
        bars={"XLF": _bars([23.62, 23.62, 19.31, 19.40], dates)},
        actions=(
            CorporateAction(
                "XLF",
                "2016-09-19",
                "dividend",
                cash_amount=4.44014886,
                source="tiingo",
            ),
        ),
        metadata={"XLF": {"price_split_basis": "as_traded"}},
        source="tiingo",
    )
    secondary = ProviderPayload(
        bars={
            "XLF": _bars(
                [23.62 / 1.231, 23.62 / 1.231, 19.31, 19.40], dates
            )
        },
        actions=(
            CorporateAction(
                "XLF", "2016-09-19", "split", split_factor=1.231, source="yahoo"
            ),
        ),
        metadata={"XLF": {"price_split_basis": "current_share_basis"}},
        source="yahoo",
    )
    config = Config(
        universe=["XLF"], source_warning_bps=5.0, source_block_bps=20.0
    )
    report = assess_market_data_quality(
        primary,
        secondary,
        required_tickers=["XLF"],
        config=config,
        as_of=pd.Timestamp("2016-09-20 21:00", tz="America/New_York"),
    )
    action_issues = [
        issue
        for issue in report.issues
        if issue.severity == QualitySeverity.BLOCK
        and issue.code == "CORPORATE_ACTION_MISMATCH"
    ]
    transform_issue = next(
        issue for issue in action_issues if issue.context["action_type"] == "dividend"
    )
    transform = DataQualityDecision(
        source_snapshot_id=7,
        issue_fingerprint=transform_issue.fingerprint,
        issue_code=transform_issue.code,
        ticker="XLF",
        start_date="2016-09-15",
        end_date="2016-09-19",
        raw_data_hash=report.raw_data_hash,
        disposition=DataQualityDisposition.ACCEPTED_EXCEPTION,
        normalization={
            "kind": "bounded_close_factor",
            "role": "primary",
            "operation": "divide",
            "factor": 1.231,
            "start_date": "2016-09-15",
            "end_date": "2016-09-16",
        },
        evidence={"uri": "https://example.test/official-corporate-action"},
        reason="Official in-kind distribution representation.",
        decided_by="operator",
        decided_at=NOW,
    )
    remaining_action = next(issue for issue in action_issues if issue != transform_issue)
    companion = _decision(
        report,
        snapshot_id=7,
        issue=remaining_action,
    )

    resolved = apply_data_quality_decisions(
        report,
        [transform, companion],
        source_snapshot_id=7,
        primary=primary,
        secondary=secondary,
        config=config,
    )

    blocking = {
        issue.fingerprint
        for issue in report.issues
        if issue.severity == QualitySeverity.BLOCK
    }
    # A bounded Close decision cannot also approve the newly verified Open
    # series. Its separate disagreement requires its own explicit evidence.
    assert resolved.status == DataQualityStatus.BLOCKED
    open_blocking = {issue.fingerprint for issue in report.issues if issue.code == "CROSS_SOURCE_OPEN_MISMATCH" and issue.severity == QualitySeverity.BLOCK}
    assert open_blocking
    assert set(resolved.adjudicated_issue_fingerprints) == blocking - open_blocking


def test_loader_fails_closed_then_uses_snapshot_bound_decisions_and_saves_draft():
    class _Provider:
        def __init__(self, name, spy_values):
            self.name = name
            self.spy_values = spy_values

        def fetch(self, tickers, start, end):
            dates = ["2024-01-02", "2024-01-03"]
            return ProviderPayload(
                bars={
                    "SPY": _bars(self.spy_values, dates),
                    "^VIX": _bars([15.0, 15.0], dates),
                },
                actions=(),
                metadata={"SPY": {}, "^VIX": {}},
                source=self.name,
            )

    engine = create_db_engine("sqlite:///:memory:")
    create_all(engine)
    repository = TrustedMarketDataRepository(engine=engine)
    config = Config(universe=["SPY"], start_date="2024-01-01")

    def loader():
        return TrustedMarketDataLoader(
            config,
            primary_provider=_Provider("primary", [100.0, 100.0]),
            secondary_provider=_Provider("secondary", [99.7, 99.7]),
            repository=repository,
            as_of=pd.Timestamp("2024-01-03 21:00", tz="America/New_York"),
        )

    first = loader()
    with pytest.raises(ValueError, match="not actionable"):
        first.load()
    assert first.load(require_actionable=False)
    source_snapshot_id = first.dataset_snapshot_id
    for issue in first.quality_report.issues:
        if issue.severity == QualitySeverity.BLOCK:
            repository.save_quality_decision(
                _decision(first.quality_report, snapshot_id=source_snapshot_id, issue=issue)
            )

    second = loader()
    data = second.load()

    assert data["SPY"].shape[0] == 2
    assert second.quality_report.status == DataQualityStatus.TRUSTED_WITH_EXCEPTIONS
    assert second.dataset_snapshot_id != source_snapshot_id
    with engine.connect() as connection:
        statuses = connection.execute(
            select(dataset_snapshots.c.id, dataset_snapshots.c.status)
        ).all()
        universe_status = connection.execute(
            select(universe_versions.c.status).where(
                universe_versions.c.version == config.universe_version
            )
        ).scalar_one()
    assert dict(statuses)[source_snapshot_id] == "BLOCKED"
    assert dict(statuses)[second.dataset_snapshot_id] == "TRUSTED_WITH_EXCEPTIONS"
    assert universe_status == "draft"
