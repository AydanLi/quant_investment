from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest
from sqlalchemy import select

from config.settings import Config
from data.adjustments import locally_adjust_ohlcv
from data.calendar import NyseCalendar
from data.features import FeatureEngineer
from data.models import (
    CorporateAction,
    DataQualityReport,
    DataQualityStatus,
    ProviderPayload,
)
from data.providers import (
    ProviderError,
    ProviderRateLimitError,
    TiingoMarketDataProvider,
)
from data.quality import assess_market_data_quality, dataset_content_hash
from data.trusted_loader import TrustedMarketDataLoader
from storage.db import create_all, create_db_engine
from storage.repositories.trusted_data import TrustedMarketDataRepository
from storage.schema import data_revisions
from scripts.validate_dynamic_factor_model import load_snapshot_market_data


def _bars(values: list[float], dates: list[str]) -> pd.DataFrame:
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


def _payload(close: float, *, source: str, action=()) -> ProviderPayload:
    frame = _bars([close, close], ["2024-01-02", "2024-01-03"])
    return ProviderPayload(
        bars={"SPY": frame},
        actions=tuple(action),
        metadata={"SPY": {}},
        source=source,
    )


def test_dividend_adjustment_is_recomputed_across_cache_boundary():
    raw = _bars([100.0, 99.0], ["2024-01-02", "2024-01-03"])
    action = CorporateAction("SPY", pd.Timestamp("2024-01-03"), "dividend", cash_amount=1.0)

    adjusted = locally_adjust_ohlcv(raw, [action])

    assert adjusted.loc["2024-01-02", "Adjusted Close"] == 99.0
    assert adjusted["Adjusted Close"].pct_change().iloc[-1] == 0.0


def test_split_adjustment_removes_mechanical_price_drop():
    raw = _bars([100.0, 50.0], ["2024-01-02", "2024-01-03"])
    action = CorporateAction("SPY", pd.Timestamp("2024-01-03"), "split", split_factor=2.0)

    adjusted = locally_adjust_ohlcv(raw, [action])

    assert adjusted.loc["2024-01-02", "Adjusted Close"] == 50.0
    assert adjusted["Adjusted Close"].pct_change().iloc[-1] == 0.0


def test_dual_source_warning_is_actionable_but_block_threshold_is_not():
    config = Config(universe=["SPY"], source_warning_bps=5.0, source_block_bps=20.0)
    as_of = pd.Timestamp("2024-01-03 21:00", tz="America/New_York")
    primary = _payload(100.0, source="primary")
    warning_secondary = _payload(99.90, source="secondary")
    blocked_secondary = _payload(99.70, source="secondary")

    warning = assess_market_data_quality(
        primary,
        warning_secondary,
        required_tickers=["SPY"],
        config=config,
        as_of=as_of,
    )
    blocked = assess_market_data_quality(
        primary,
        blocked_secondary,
        required_tickers=["SPY"],
        config=config,
        as_of=as_of,
    )

    assert warning.status == DataQualityStatus.WARNING
    assert warning.actionable is True
    assert blocked.status == DataQualityStatus.BLOCKED
    assert blocked.actionable is False
    assert warning.content_hash != blocked.content_hash


def test_corporate_action_disagreement_blocks_dataset():
    config = Config(universe=["SPY"])
    as_of = pd.Timestamp("2024-01-03 21:00", tz="America/New_York")
    primary_action = CorporateAction("SPY", pd.Timestamp("2024-01-03"), "dividend", cash_amount=1.0, source="primary")
    secondary_action = replace(primary_action, cash_amount=0.9, source="secondary")

    report = assess_market_data_quality(
        _payload(100.0, source="primary", action=[primary_action]),
        _payload(100.0, source="secondary", action=[secondary_action]),
        required_tickers=["SPY"],
        config=config,
        as_of=as_of,
    )

    assert report.status == DataQualityStatus.BLOCKED
    assert any(issue.code == "CORPORATE_ACTION_VALUE_MISMATCH" for issue in report.issues)


def test_vendor_decimal_rounding_is_not_treated_as_action_conflict():
    config = Config(universe=["SPY"])
    as_of = pd.Timestamp("2024-01-03 21:00", tz="America/New_York")
    primary_action = CorporateAction("SPY", pd.Timestamp("2024-01-03"), "dividend", cash_amount=1.594937, source="primary")
    secondary_action = replace(primary_action, cash_amount=1.595, source="secondary")

    report = assess_market_data_quality(
        _payload(100.0, source="primary", action=[primary_action]),
        _payload(100.0, source="secondary", action=[secondary_action]),
        required_tickers=["SPY"],
        config=config,
        as_of=as_of,
    )

    assert not any(issue.code == "CORPORATE_ACTION_VALUE_MISMATCH" for issue in report.issues)


def test_one_stale_session_is_diagnostic_and_two_sessions_are_blocked():
    config = Config(universe=["SPY"])
    one_day_frame = _bars([100.0, 101.0], ["2024-01-05", "2024-01-08"])
    two_day_frame = _bars([100.0], ["2024-01-05"])
    one_day = ProviderPayload(
        bars={"SPY": one_day_frame}, actions=(), metadata={"SPY": {}}, source="primary"
    )
    one_day_check = replace(one_day, source="secondary")
    two_day = ProviderPayload(
        bars={"SPY": two_day_frame}, actions=(), metadata={"SPY": {}}, source="primary"
    )
    two_day_check = replace(two_day, source="secondary")

    diagnostic = assess_market_data_quality(
        one_day,
        one_day_check,
        required_tickers=["SPY"],
        config=config,
        as_of=pd.Timestamp("2024-01-09 21:00", tz="America/New_York"),
    )
    blocked = assess_market_data_quality(
        two_day,
        two_day_check,
        required_tickers=["SPY"],
        config=config,
        as_of=pd.Timestamp("2024-01-09 21:00", tz="America/New_York"),
    )

    assert diagnostic.status == DataQualityStatus.WARNING
    assert diagnostic.stale_sessions == 1
    assert diagnostic.actionable is False
    assert blocked.status == DataQualityStatus.BLOCKED
    assert blocked.stale_sessions == 2


class _Response:
    status_code = 200

    def __init__(self, payload, headers=None):
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


class _RecordingSession:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/prices"):
            return _Response(
                [
                    {
                        "date": "2024-01-02T00:00:00.000Z",
                        "open": 100.0,
                        "high": 101.0,
                        "low": 99.0,
                        "close": 100.5,
                        "volume": 1_000_000,
                    }
                ]
            )
        return _Response({"ticker": "SPY", "startDate": "1993-01-29"})


def test_tiingo_token_is_sent_only_in_authorization_header():
    session = _RecordingSession()
    provider = TiingoMarketDataProvider(token="test-secret", session=session)

    provider.fetch_bars(["SPY"], "2024-01-01", "2024-01-03")
    provider.fetch_metadata(["SPY"])

    assert len(session.calls) == 2
    for _, kwargs in session.calls:
        assert "token" not in kwargs.get("params", {})
        assert kwargs["headers"] == {"Authorization": "Token test-secret"}


def test_tiingo_rate_limit_is_explicit_and_does_not_expose_token():
    class _RateLimitedSession:
        def get(self, url, **kwargs):
            return type(
                "RateLimitedResponse",
                (),
                {"status_code": 429, "headers": {"Retry-After": "3600"}},
            )()

    provider = TiingoMarketDataProvider(
        token="test-secret",
        session=_RateLimitedSession(),
    )

    with pytest.raises(ProviderRateLimitError, match="rate limit") as exc_info:
        provider.fetch_bars(["SPY"], "2024-01-01", "2024-01-03")

    assert exc_info.value.retry_after == "3600"
    assert "test-secret" not in str(exc_info.value)


def test_missing_tiingo_credential_never_silently_promotes_yahoo(monkeypatch):
    monkeypatch.delenv("TIINGO_API_TOKEN", raising=False)

    with pytest.raises(ProviderError, match="TIINGO_API_TOKEN"):
        TrustedMarketDataLoader(Config(universe=["SPY"]), persist=False)


def test_trusted_loader_reuses_one_snapshot_without_refetching():
    class _CountingProvider:
        name = "fixture"

        def __init__(self):
            self.calls = 0

        def fetch(self, tickers, start, end):
            self.calls += 1
            spy = _bars([100.0, 101.0], ["2024-01-02", "2024-01-03"])
            vix = _bars([15.0, 16.0], ["2024-01-02", "2024-01-03"])
            return ProviderPayload(
                bars={"SPY": spy, "^VIX": vix},
                actions=(),
                metadata={"SPY": {}, "^VIX": {}},
                source=self.name,
            )

    provider = _CountingProvider()
    loader = TrustedMarketDataLoader(
        Config(universe=["SPY"], start_date="2024-01-01"),
        primary_provider=provider,
        secondary_provider=None,
        persist=False,
        as_of=pd.Timestamp("2024-01-03 21:00", tz="America/New_York"),
    )

    first = loader.load()
    first["SPY"].iloc[0, 0] = -1.0
    second = loader.load()

    assert provider.calls == 1
    assert second["SPY"].iloc[0, 0] != -1.0


def test_vix_is_not_misclassified_by_the_etf_extreme_return_gate():
    frame = _bars([10.0, 15.0], ["2024-01-02", "2024-01-03"])
    primary = ProviderPayload(
        bars={"^VIX": frame}, actions=(), metadata={"^VIX": {}}, source="cboe"
    )
    secondary = replace(primary, source="yahoo")

    report = assess_market_data_quality(
        primary,
        secondary,
        required_tickers=["^VIX"],
        config=Config(universe=["SPY"]),
        as_of=pd.Timestamp("2024-01-03 21:00", tz="America/New_York"),
    )

    assert not any("EXTREME_RETURN" in issue.code for issue in report.issues)
    assert report.status == DataQualityStatus.TRUSTED


def test_nyse_calendar_and_feature_frame_exclude_non_benchmark_dates():
    calendar = NyseCalendar()
    assert calendar.is_session("2024-07-04") is False
    spy = _bars([100.0, 101.0], ["2024-07-03", "2024-07-05"])
    vix = _bars([12.0, 13.0, 14.0], ["2024-07-03", "2024-07-04", "2024-07-05"])
    prices = FeatureEngineer(
        {"SPY": spy, "^VIX": vix}, Config(universe=["SPY"])
    ).make_price_frame()

    assert pd.Timestamp("2024-07-04") not in prices.index


def test_snapshot_hash_is_order_independent_and_revisions_are_audited():
    first = _bars([100.0], ["2024-01-02"])
    second = _bars([200.0], ["2024-01-02"])
    assert dataset_content_hash({"SPY": first, "QQQ": second}, ()) == dataset_content_hash(
        {"QQQ": second, "SPY": first}, ()
    )

    engine = create_db_engine("sqlite:///:memory:")
    create_all(engine)
    repository = TrustedMarketDataRepository(engine=engine)
    repository.upsert_raw_bars({"SPY": first}, source="test")
    changed = first.copy()
    changed.loc[:, "Close"] = 101.0
    repository.upsert_raw_bars({"SPY": changed}, source="test")
    with engine.connect() as connection:
        revisions = connection.execute(select(data_revisions)).mappings().all()

    assert len(revisions) == 1
    assert revisions[0]["field"] == "close"
    assert revisions[0]["old_value"] == 100.0
    assert revisions[0]["new_value"] == 101.0


def test_dataset_snapshot_payload_is_immutable_and_reconstructable():
    engine = create_db_engine("sqlite:///:memory:")
    create_all(engine)
    repository = TrustedMarketDataRepository(engine=engine)
    bars = {"SPY": _bars([100.0, 101.0], ["2024-01-02", "2024-01-03"])}
    action = CorporateAction(
        "SPY", pd.Timestamp("2024-01-03"), "dividend", cash_amount=0.5, source="test"
    )
    content_hash = dataset_content_hash(bars, [action], source="test")
    report = DataQualityReport(
        status=DataQualityStatus.TRUSTED,
        primary_source="test",
        secondary_source="check",
        expected_session="2024-01-03",
        latest_session="2024-01-03",
        stale_sessions=0,
        content_hash=content_hash,
    )
    snapshot_id = repository.create_snapshot(
        report,
        as_of="2024-01-03T21:00:00-05:00",
        start_date="2024-01-02",
        end_date="2024-01-03",
        bars=bars,
        actions=[action],
        source_by_ticker={"SPY": "test"},
        secondary_payload=ProviderPayload(
            bars={"SPY": bars["SPY"].copy()},
            actions=(replace(action, source="check"),),
            metadata={"SPY": {"source": "check"}},
            source="check",
        ),
    )
    changed = bars["SPY"].copy()
    changed.loc[:, "Close"] = 999.0
    repository.upsert_raw_bars({"SPY": changed}, source="test")

    restored = repository.load_snapshot(snapshot_id)
    sources = repository.load_snapshot_sources(snapshot_id)

    assert restored.bars["SPY"].loc["2024-01-03", "Close"] == 101.0
    assert len(restored.actions) == 1
    assert set(sources) == {"primary", "secondary"}
    assert sources["secondary"].bars["SPY"].loc["2024-01-03", "Close"] == 101.0
    assert sources["secondary"].actions[0].source == "check"
    assert dataset_content_hash(
        restored.bars, restored.actions, source=restored.source
    ) == content_hash


def test_model_admission_rejects_a_stale_immutable_snapshot(tmp_path):
    database = tmp_path / "stale-snapshot.db"
    engine = create_db_engine(f"sqlite:///{database.as_posix()}")
    create_all(engine)
    repository = TrustedMarketDataRepository(engine=engine)
    bars = {"SPY": _bars([100.0], ["2024-01-02"])}
    report = DataQualityReport(
        status=DataQualityStatus.WARNING,
        primary_source="test",
        secondary_source="check",
        expected_session="2024-01-03",
        latest_session="2024-01-02",
        stale_sessions=1,
        content_hash=dataset_content_hash(bars, (), source="stale-test"),
    )
    snapshot_id = repository.create_snapshot(
        report,
        as_of="2024-01-03T21:00:00-05:00",
        start_date="2024-01-02",
        end_date="2024-01-02",
        bars=bars,
        actions=(),
        source_by_ticker={"SPY": "test"},
    )

    with pytest.raises(ValueError, match="zero-staleness"):
        load_snapshot_market_data(
            database=str(database),
            snapshot_id=snapshot_id,
            require_actionable=True,
        )
