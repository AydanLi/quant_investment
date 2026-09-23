from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from config.settings import Config
from data.calendar import NyseCalendar
from data.models import DATA_QUALITY_MODEL_VERSION, DataQualityReport, DataQualityStatus
from services.models import SignalStatus
from services.signal_service import SignalService
from research.runtime import build_runtime_manifest


def _frozen_config():
    return Config(universe=["SPY", "BIL"], benchmark="SPY",
                  strategy_version="SV-FROZEN-001", universe_version="UV-001")


@pytest.fixture(autouse=True)
def governed_runtime(monkeypatch):
    """These are signal-window unit tests; storage binding has integration tests."""
    class Governance:
        def __init__(self, **kwargs):
            pass

        def load_frozen_runtime(self, version):
            assert version == "SV-FROZEN-001"
            return build_runtime_manifest(_frozen_config(), code_identity={"fixture": True},
                                          research_cutoff="2023-12-31", dataset_snapshot_id=17)

        def is_universe_approved(self, version):
            return version == "UV-001"

        def is_strategy_frozen(self, version):
            return version == "SV-FROZEN-001"

    monkeypatch.setattr("services.signal_service.GovernanceRepository", Governance)


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
            quality_model_version=DATA_QUALITY_MODEL_VERSION,
        )
        self.dataset_snapshot_id = snapshot_id
        self.repository = SimpleNamespace(engine=object())

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
    config = _frozen_config()
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


def test_before_cutoff_is_diagnostic_and_frozen_frequency_change_is_blocked():
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
    assert exploratory.status == SignalStatus.BLOCKED
    assert any("rebalance_frequency" in reason for reason in exploratory.block_reasons)


def test_month_end_signal_can_be_caught_up_only_before_t1_0925_et():
    config, data = _signal_fixture()
    service = SignalService(config, loader=_TrustedLoader(data))

    caught_up = service.generate_decision(
        as_of=pd.Timestamp("2025-01-02 09:24", tz="America/New_York")
    )
    too_late = service.generate_decision(
        as_of=pd.Timestamp("2025-01-02 09:25", tz="America/New_York")
    )

    assert caught_up.status == SignalStatus.ACTIONABLE
    assert caught_up.signal_session == "2024-12-31"
    assert caught_up.next_rebalance_session == "2025-01-02"
    assert too_late.status == SignalStatus.DIAGNOSTIC


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


def test_malformed_blocked_data_returns_structured_blocked_decision():
    config, data = _signal_fixture()
    malformed = {
        ticker: frame.drop(columns=[column for column in frame if "Close" in column])
        for ticker, frame in data.items()
    }

    decision = SignalService(
        config,
        loader=_TrustedLoader(malformed, status=DataQualityStatus.BLOCKED),
    ).generate_decision(
        as_of=pd.Timestamp("2024-12-31 20:31", tz="America/New_York")
    )

    assert decision.status == SignalStatus.BLOCKED
    assert decision.regime == "UNAVAILABLE"
    assert decision.target_weights == {}


def test_signal_service_uses_diagnostic_loader_path_for_blocked_data():
    config, data = _signal_fixture()

    class _FailClosedLoader(_TrustedLoader):
        def load(self, *, require_actionable=True):
            assert require_actionable is False
            return self._data

    decision = SignalService(
        config,
        loader=_FailClosedLoader(data, status=DataQualityStatus.BLOCKED),
    ).generate_decision(
        as_of=pd.Timestamp("2024-12-31 20:31", tz="America/New_York")
    )

    assert decision.status == SignalStatus.BLOCKED
    assert decision.target_weights == {}
    assert decision.actionable is False
