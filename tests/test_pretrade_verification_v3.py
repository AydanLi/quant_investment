from datetime import datetime, timezone

from data.models import DataQualityReport, DataQualityStatus
from execution import (
    AccountSnapshot,
    PreTradeVerification,
    Quote,
    ReconciliationResult,
    verify_pre_open,
)
from services.models import SignalDecision, SignalStatus


def _decision():
    return SignalDecision(
        strategy_version="SV-001",
        universe_version="UV-001",
        dataset_snapshot_id=1,
        signal_session="2026-07-16",
        data_as_of="2026-07-16",
        generated_at="2026-07-16T20:31:00-04:00",
        next_rebalance_session="2026-07-17",
        status=SignalStatus.ACTIONABLE,
        regime="neutral",
        target_weights={"BIL": 1.0},
        current_weights={"BIL": 1.0},
        weight_deltas={"BIL": 0.0},
        dollar_deltas={"BIL": 0.0},
        estimated_cost_dollars=0.0,
    )


def _quality():
    return DataQualityReport(
        status=DataQualityStatus.TRUSTED,
        primary_source="tiingo+cboe",
        secondary_source="yahoo",
        expected_session="2026-07-16",
        latest_session="2026-07-16",
        stale_sessions=0,
    )


def test_preopen_rechecks_session_data_account_quotes_and_risk():
    now = datetime(2026, 7, 17, 13, 35, tzinfo=timezone.utc)
    account = AccountSnapshot(
        account_ref="paper",
        nav=10_000.0,
        settled_cash=100.0,
        available_cash=100.0,
        buying_power=100.0,
        positions={},
        captured_at=now,
    )
    reconciliation = ReconciliationResult(
        matched=True,
        difference_value=0.0,
        threshold=5.0,
        negative_cash=False,
        short_positions=(),
        unknown_positions=(),
        open_orders=(),
        reasons=(),
    )
    quote = Quote("BIL", 91.0, 91.01, now)

    result = verify_pre_open(
        decision=_decision(),
        verified_at=now,
        account=account,
        quality=_quality(),
        reconciliation=reconciliation,
        quotes={"BIL": quote},
        risk_state="NORMAL",
    )

    assert result.passed is True
    halted = verify_pre_open(
        decision=_decision(),
        verified_at=now,
        account=account,
        quality=_quality(),
        reconciliation=reconciliation,
        quotes={"BIL": quote},
        risk_state="DRAWDOWN_HALTED",
    )
    assert halted.passed is False
    assert "RISK_HALTED" in halted.reasons
