from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from config.settings import Config
from data.calendar import NyseCalendar
from data.models import DataQualityReport, DataQualityStatus
from services.models import SignalStatus
from services.signal_service import SignalService


class _TrustedLoader:
    def __init__(self, data, *, status=DataQualityStatus.TRUSTED, snapshot_id=17, stale_sessions=0):
        self._data = data
        latest = str(next(iter(data.values())).index[-1].date())
        self.quality_report = DataQualityReport(
            status=status,
            primary_source="tiingo+cboe",
            secondary_source="yahoo",
            expected_session=latest,
            latest_session=latest,
            stale_sessions=stale_sessions,
            issues=(),
            content_hash="a" * 64,
        )
        self.dataset_snapshot_id = snapshot_id

    @property
    def actionable(self):
        return self.quality_report.actionable

    def load(self):
        return self._data


def _signal_fixture():
    calendar = NyseCalendar()
    sessions = calendar.sessions("2023-01-01", "2024-12-31")
    values = np.linspace(100.0, 140.0, len(sessions))

    def frame(close):
        close = pd.Series(close, index=sessions)
        return pd.DataFrame(
            {
                "Adjusted Open": close,
                "Adjusted High": close,
                "Adjusted Low": close,
                "Adjusted Close": close,
                "Volume": 1_000_000.0,
            }
        )

    data = {
        "SPY": frame(values),
        "BIL": frame(np.linspace(91.0, 93.0, len(sessions))),
        "^VIX": frame(np.full(len(sessions), 15.0)),
    }
    config = Config(
        universe=["SPY", "BIL"],
        benchmark="SPY",
        strategy_version="SV-FROZEN-001",
        universe_version="UV-001",
    )
    return config, data


def test_month_end_after_cutoff_produces_versioned_actionable_decision():
    config, data = _signal_fixture()
    decision = SignalService(config, loader=_TrustedLoader(data)).generate_decision(
        as_of=pd.Timestamp("2024-12-31 20:31", tz="America/New_York"),
        current_weights={"BIL": 1.0},
        nav=10_000.0,
    )

    assert decision.status == SignalStatus.ACTIONABLE
    assert decision.dataset_snapshot_id == 17
    assert decision.strategy_version == "SV-FROZEN-001"
    assert decision.signal_session == "2024-12-31"
    assert decision.next_rebalance_session == "2025-01-02"
    assert abs(sum(decision.target_weights.values()) - 1.0) < 1e-12


def test_before_cutoff_and_exploratory_frequency_are_diagnostic_only():
    config, data = _signal_fixture()
    before_cutoff = SignalService(config, loader=_TrustedLoader(data)).generate_decision(
        as_of=pd.Timestamp("2024-12-31 19:00", tz="America/New_York")
    )
    exploratory = SignalService(
        replace(config, rebalance_frequency="W"), loader=_TrustedLoader(data)
    ).generate_decision(
        as_of=pd.Timestamp("2024-12-31 20:31", tz="America/New_York")
    )

    assert before_cutoff.status == SignalStatus.DIAGNOSTIC
    assert exploratory.status == SignalStatus.DIAGNOSTIC


def test_untrusted_data_unfrozen_strategy_and_halt_cannot_be_actionable():
    config, data = _signal_fixture()
    blocked = SignalService(
        config,
        loader=_TrustedLoader(data, status=DataQualityStatus.BLOCKED),
    ).generate_decision(
        as_of=pd.Timestamp("2024-12-31 20:31", tz="America/New_York")
    )
    unfrozen = SignalService(
        replace(config, strategy_version="UNFROZEN"), loader=_TrustedLoader(data)
    ).generate_decision(
        as_of=pd.Timestamp("2024-12-31 20:31", tz="America/New_York")
    )
    halted = SignalService(config, loader=_TrustedLoader(data)).generate_decision(
        as_of=pd.Timestamp("2024-12-31 20:31", tz="America/New_York"),
        risk_state="DRAWDOWN_HALTED",
    )

    assert blocked.status == SignalStatus.BLOCKED
    assert unfrozen.status == SignalStatus.BLOCKED
    assert halted.status == SignalStatus.HALTED


def test_one_stale_session_is_diagnostic_but_not_orderable():
    config, data = _signal_fixture()
    decision = SignalService(
        config,
        loader=_TrustedLoader(
            data,
            status=DataQualityStatus.WARNING,
            stale_sessions=1,
        ),
    ).generate_decision(
        as_of=pd.Timestamp("2024-12-31 20:31", tz="America/New_York")
    )

    assert decision.status == SignalStatus.DIAGNOSTIC
    assert decision.actionable is False


def test_blocked_data_with_missing_regime_input_returns_no_liquidation_draft():
    config, data = _signal_fixture()
    data_without_vix = {ticker: frame for ticker, frame in data.items() if ticker != "^VIX"}

    decision = SignalService(
        config,
        loader=_TrustedLoader(
            data_without_vix,
            status=DataQualityStatus.BLOCKED,
        ),
    ).generate_decision(
        as_of=pd.Timestamp("2024-12-31 20:31", tz="America/New_York"),
        current_weights={"SPY": 1.0},
    )

    assert decision.status == SignalStatus.BLOCKED
    assert decision.regime == "UNAVAILABLE"
    assert decision.target_weights == {}
    assert decision.weight_deltas == {}
