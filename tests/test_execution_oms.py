from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from config.settings import Config
from execution import (
    AccountSnapshot,
    BrokerPosition,
    InMemoryPaperBroker,
    OrderManagementSystem,
    OrderState,
    PreTradeVerification,
    Quote,
    reconcile_account,
)
from services.models import SignalDecision, SignalStatus


NOW = datetime(2026, 7, 17, 13, 35, tzinfo=timezone.utc)
VERIFIED = PreTradeVerification(NOW, True, ())


def _decision(status=SignalStatus.ACTIONABLE):
    return SignalDecision(
        strategy_version="SV-001",
        universe_version="UV-001",
        dataset_snapshot_id=1,
        signal_session="2026-07-16",
        data_as_of="2026-07-16",
        generated_at=NOW.isoformat(),
        next_rebalance_session="2026-07-17",
        status=status,
        regime="neutral",
        target_weights={"SPY": 0.3, "BIL": 0.7},
        current_weights={"QQQ": 0.1, "BIL": 0.9},
        weight_deltas={"SPY": 0.1, "QQQ": -0.1},
        dollar_deltas={"SPY": 1000.0, "QQQ": -1000.0},
        estimated_cost_dollars=1.4,
    )


def _broker():
    account = AccountSnapshot(
        account_ref="paper",
        nav=10_000.0,
        settled_cash=5_000.0,
        available_cash=5_000.0,
        buying_power=10_000.0,
        positions={"QQQ": BrokerPosition("QQQ", 5.0, 1000.0)},
        captured_at=NOW,
    )
    quotes = {
        "SPY": Quote("SPY", 99.95, 100.05, NOW),
        "QQQ": Quote("QQQ", 199.90, 200.10, NOW),
    }
    return InMemoryPaperBroker(account, quotes), account, quotes


def test_order_drafts_are_idempotent_sell_first_and_require_human_approval():
    broker, account, quotes = _broker()
    oms = OrderManagementSystem(Config(strategy_version="SV-001"), broker)
    kwargs = {
        "quotes": quotes,
        "median_daily_dollar_volume": {"SPY": 10_000_000.0, "QQQ": 10_000_000.0},
        "account": account,
        "verification": VERIFIED,
    }

    drafts = oms.create_drafts(_decision(), **kwargs)
    duplicate = oms.create_drafts(_decision(), **kwargs)

    assert drafts[0].side.value == "SELL"
    assert [item.client_order_id for item in drafts] == [item.client_order_id for item in duplicate]
    with pytest.raises(ValueError, match="approval"):
        oms.submit(drafts[0].client_order_id)

    approved = oms.approve(drafts[0].client_order_id, approved_by="operator")
    submitted = oms.submit(approved.client_order_id)
    assert submitted.state == OrderState.SUBMITTED
    assert submitted.broker_order_id
    assert oms.update_status(submitted.client_order_id, "PARTIAL").state == OrderState.PARTIAL
    assert oms.update_status(submitted.client_order_id, "FILLED").state == OrderState.FILLED


def test_stale_limit_is_canceled_and_40bp_reprice_needs_second_approval():
    broker, account, quotes = _broker()
    oms = OrderManagementSystem(Config(strategy_version="SV-001"), broker)
    intent = oms.create_drafts(
        _decision(),
        quotes=quotes,
        median_daily_dollar_volume={"SPY": 10_000_000.0, "QQQ": 10_000_000.0},
        account=account,
        verification=VERIFIED,
    )[0]
    oms.approve(intent.client_order_id, approved_by="operator")
    oms.submit(intent.client_order_id)
    intent.submitted_at = NOW

    canceled = oms.cancel_stale(now=NOW + timedelta(minutes=5))
    assert intent.client_order_id in canceled
    assert intent.state == OrderState.CANCELED
    with pytest.raises(ValueError, match="40 bp"):
        oms.reprice_with_second_approval(
            intent.client_order_id,
            new_limit_price=intent.arrival_quote.mid * 1.005,
            approved_by="operator",
        )


def test_spread_adv_and_nonactionable_gates_block_orders():
    broker, account, quotes = _broker()
    oms = OrderManagementSystem(Config(strategy_version="SV-001"), broker)
    with pytest.raises(ValueError, match="ACTIONABLE"):
        oms.create_drafts(
            _decision(SignalStatus.DIAGNOSTIC),
            quotes=quotes,
            median_daily_dollar_volume={"SPY": 10_000_000.0, "QQQ": 10_000_000.0},
            account=account,
            verification=VERIFIED,
        )
    wide = dict(quotes)
    wide["SPY"] = Quote("SPY", 99.0, 101.0, NOW)
    with pytest.raises(ValueError, match="spread"):
        oms.create_drafts(
            _decision(),
            quotes=wide,
            median_daily_dollar_volume={"SPY": 10_000_000.0, "QQQ": 10_000_000.0},
            account=account,
            verification=VERIFIED,
        )
    with pytest.raises(ValueError, match="1% ADV"):
        oms.create_drafts(
            _decision(),
            quotes=quotes,
            median_daily_dollar_volume={"SPY": 50_000.0, "QQQ": 50_000.0},
            account=account,
            verification=VERIFIED,
        )


def test_reconciliation_locks_material_difference():
    _, account, _ = _broker()
    matched = reconcile_account(account=account, expected_values={"QQQ": 1000.0})
    mismatch = reconcile_account(account=account, expected_values={"QQQ": 900.0})

    assert matched.matched is True
    assert mismatch.matched is False
    assert "ACCOUNT_VALUE_MISMATCH" in mismatch.reasons


def test_drift_review_blocks_new_buys_but_keeps_risk_reducing_sells():
    broker, account, quotes = _broker()
    oms = OrderManagementSystem(Config(strategy_version="SV-001"), broker)
    decision = replace(_decision(), risk_state="DRIFT_REVIEW")

    drafts = oms.create_drafts(
        decision,
        quotes=quotes,
        median_daily_dollar_volume={"SPY": 10_000_000.0, "QQQ": 10_000_000.0},
        account=account,
        verification=VERIFIED,
    )

    assert [intent.ticker for intent in drafts] == ["QQQ"]
    assert drafts[0].side.value == "SELL"
    assert oms.last_draft_warnings == ("BUY_BLOCKED_DRIFT_REVIEW:SPY",)


def test_oms_uses_configured_impact_coefficient_and_0935_execution_gate():
    broker, account, quotes = _broker()
    oms = OrderManagementSystem(
        Config(strategy_version="SV-001", impact_coefficient_bps=25.0), broker
    )
    with pytest.raises(ValueError, match="09:35"):
        oms.create_drafts(
            _decision(),
            quotes=quotes,
            median_daily_dollar_volume={"SPY": 1_000_000.0, "QQQ": 1_000_000.0},
            account=account,
            verification=PreTradeVerification(
                datetime(2026, 7, 17, 13, 34, tzinfo=timezone.utc), True, ()
            ),
        )

    drafts = oms.create_drafts(
        _decision(),
        quotes=quotes,
        median_daily_dollar_volume={"SPY": 1_000_000.0, "QQQ": 1_000_000.0},
        account=account,
        verification=VERIFIED,
    )
    assert all(intent.estimated_impact_bps == pytest.approx(25.0) for intent in drafts)
