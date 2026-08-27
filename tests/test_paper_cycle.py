from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from config.settings import Config
from execution.adapters import InMemoryPaperBroker
from execution.models import ExecutionFill, OrderState, Quote
from execution.oms import OrderManagementSystem
from services.models import PaperCycleStatus, SignalDecision, SignalStatus
from services.paper_cycle import PaperCycle
from storage.db import create_all, create_db_engine
from storage.repositories.signals import SignalRepository
from storage.schema import (
    admission_runs,
    dataset_snapshots,
    execution_fills,
    order_intents,
    parameter_trials,
    paper_accounts,
    paper_cash_movements,
    paper_cycles,
    reconciliations,
    risk_incidents,
    signal_decisions,
    strategy_versions,
    universe_versions,
)


ET = ZoneInfo("America/New_York")


def _quality_payload(
    session: str,
    *,
    status: str = "TRUSTED",
    content_hash: str = "c" * 64,
    raw_data_hash: str = "d" * 64,
    stale_sessions: int = 0,
    decision_set_hash: str | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "primary_source": "tiingo",
        "secondary_source": "yahoo",
        "expected_session": session,
        "latest_session": session,
        "stale_sessions": stale_sessions,
        "issues": [],
        "content_hash": content_hash,
        "raw_data_hash": raw_data_hash,
        "source_snapshot_id": None,
        "decision_set_hash": decision_set_hash,
        "adjudicated_issue_fingerprints": [],
    }


def _insert_snapshot(
    conn,
    *,
    snapshot_id: int,
    session: str = "2026-07-31",
    status: str = "TRUSTED",
    hash_character: str = "c",
    stale_sessions: int = 0,
) -> None:
    content_hash = hash_character * 64
    conn.execute(
        dataset_snapshots.insert().values(
            id=snapshot_id,
            as_of=f"{session}T20:30:00-04:00",
            start_date="2006-01-01",
            end_date=session,
            primary_source="tiingo",
            secondary_source="yahoo",
            content_hash=content_hash,
            status=status,
            quality_json=_quality_payload(
                session,
                status=status,
                content_hash=content_hash,
                raw_data_hash=(hash_character.upper() * 64),
                stale_sessions=stale_sessions,
            ),
        )
    )


def _engine():
    engine = create_db_engine("sqlite:///:memory:")
    create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            dataset_snapshots.insert().values(
                id=1,
                as_of="2026-07-31T20:30:00-04:00",
                start_date="2006-01-01",
                end_date="2026-07-31",
                primary_source="tiingo",
                secondary_source="yahoo",
                content_hash="a" * 64,
                status="TRUSTED",
                quality_json={
                    "status": "TRUSTED",
                    "primary_source": "tiingo",
                    "secondary_source": "yahoo",
                    "expected_session": "2026-07-31",
                    "latest_session": "2026-07-31",
                    "stale_sessions": 0,
                    "issues": [],
                    "content_hash": "a" * 64,
                    "raw_data_hash": "b" * 64,
                    "source_snapshot_id": None,
                    "decision_set_hash": None,
                    "adjudicated_issue_fingerprints": [],
                },
            )
        )
        conn.execute(
            universe_versions.insert().values(
                version="UV-001",
                effective_date="2026-07-01",
                status="approved",
                seed_tickers_json=["SPY", "BIL"],
                rules_json={"point_in_time": True},
                approved_at=datetime(2026, 7, 29, 12, 0, tzinfo=ET),
                approved_by="operator",
                historical_universe_integrity=0,
            )
        )
        conn.execute(
            strategy_versions.insert().values(
                version="SV-001",
                status="frozen",
                frozen_at=datetime(2026, 7, 29, 13, 0, tzinfo=ET),
                universe_version="UV-001",
                dataset_snapshot_id=1,
                protocol_json={"name": "paper-test", "candidate_count": 1},
                local_sim_start=datetime(2026, 7, 29, 14, 0, tzinfo=ET),
            )
        )
        admission_id = int(
            conn.execute(
                admission_runs.insert().values(
                    strategy_version="SV-001",
                    methodology="nested_expanding_v3",
                    status="admitted",
                    selection_uses_future_holdout=0,
                    results_json={
                        "admitted": True,
                        "gates": {"historical": True},
                    },
                    completed_at=datetime(2026, 7, 29, 12, 30, tzinfo=ET),
                )
            ).inserted_primary_key[0]
        )
        conn.execute(
            parameter_trials.insert().values(
                admission_run_id=admission_id,
                stage="final_selection_summary",
                fold_key="aggregate",
                label="candidate-000",
                parameters_json={},
                folds_json=[],
                status="evaluated",
            )
        )
    return engine


def _decision() -> SignalDecision:
    return SignalDecision(
        strategy_version="SV-001",
        universe_version="UV-001",
        dataset_snapshot_id=1,
        signal_session="2026-07-31",
        data_as_of="2026-07-31",
        generated_at="2026-07-31T20:31:00-04:00",
        next_rebalance_session="2026-08-03",
        status=SignalStatus.ACTIONABLE,
        regime="neutral",
        target_weights={"SPY": 0.35, "BIL": 0.60, "CASH_USD": 0.05},
        current_weights={"CASH_USD": 1.0},
        weight_deltas={"SPY": 0.35, "BIL": 0.60, "CASH_USD": -0.95},
        dollar_deltas={"SPY": 3500.0, "BIL": 6000.0, "CASH_USD": -9500.0},
        estimated_cost_dollars=6.65,
    )


def _prepare_cycle(engine, *, notifier=lambda _message, _title: "request-id"):
    cycle = PaperCycle(
        Config(strategy_version="SV-001"),
        engine=engine,
        notifier=notifier,
    )
    cycle.initialize_account(strategy_version="SV-001")
    stored = cycle.persist_decision(
        _decision(), recorded_at=datetime(2026, 7, 31, 20, 32, tzinfo=ET)
    )
    cycle.draft_orders(
        int(stored.decision.decision_id),
        reference_prices={"SPY": 100.0, "BIL": 100.0},
        median_daily_dollar_volume={"SPY": 100_000_000.0, "BIL": 100_000_000.0},
        verified_at=datetime(2026, 8, 3, 9, 20, tzinfo=ET),
    )
    cycle.approve(
        int(stored.decision.decision_id),
        approved_by="operator",
        approved_at=datetime(2026, 8, 3, 9, 24, tzinfo=ET),
    )
    return cycle, int(stored.decision.decision_id)


def _trigger_drawdown(engine):
    cycle = PaperCycle(
        Config(strategy_version="SV-001"),
        engine=engine,
        notifier=lambda _message, _title: "id",
    )
    cycle.initialize_account(strategy_version="SV-001")
    with engine.begin() as conn:
        conn.execute(
            paper_accounts.update().values(
                nav=10_000.0,
                settled_cash=0.0,
                available_cash=0.0,
                positions_json={
                    "SPY": {"quantity": 100.0, "mark_price": 100.0},
                    "_meta": {"total_commission": 0.0},
                },
                high_water=10_000.0,
            )
        )
    stored = cycle.persist_decision(
        _decision(), recorded_at=datetime(2026, 7, 31, 20, 32, tzinfo=ET)
    )
    decision_id = int(stored.decision.decision_id)
    cycle.draft_orders(
        decision_id,
        reference_prices={"SPY": 100.0, "BIL": 100.0},
        median_daily_dollar_volume={
            "SPY": 100_000_000.0,
            "BIL": 100_000_000.0,
        },
        verified_at=datetime(2026, 8, 3, 9, 20, tzinfo=ET),
    )
    cycle.approve(
        decision_id,
        approved_by="operator",
        approved_at=datetime(2026, 8, 3, 9, 24, tzinfo=ET),
    )
    halted = cycle.materialize_open(
        decision_id,
        open_prices={"SPY": 80.0, "BIL": 100.0},
        published_at=datetime(2026, 8, 3, 9, 31, tzinfo=ET),
    )
    assert halted.status == PaperCycleStatus.HALTED
    assert halted.account.risk_state == "DRAWDOWN_HALTED"
    return cycle, decision_id


def test_replay_open_is_restart_idempotent_and_settles_t_plus_one():
    engine = _engine()
    cycle, decision_id = _prepare_cycle(engine)

    first = cycle.materialize_open(
        decision_id,
        open_prices={"SPY": 100.0, "BIL": 100.0},
        published_at=datetime(2026, 8, 3, 9, 31, tzinfo=ET),
    )
    assert first.status == PaperCycleStatus.COMPLETED
    assert set(first.order_states.values()) == {"FILLED"}
    assert first.account.unsettled_cash < 0.0
    assert len(first.account.pending_settlements) == 2

    restarted = PaperCycle(
        Config(strategy_version="SV-001"), engine=engine, notifier=lambda _m, _t: "id"
    )
    second = restarted.materialize_open(
        decision_id,
        open_prices={"SPY": 100.0, "BIL": 100.0},
        published_at=datetime(2026, 8, 3, 9, 31, tzinfo=ET),
    )
    assert second.status == PaperCycleStatus.COMPLETED
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(order_intents)).scalar_one() == 2
        assert conn.execute(select(func.count()).select_from(execution_fills)).scalar_one() == 2
        assert conn.execute(select(func.count()).select_from(paper_cash_movements)).scalar_one() == 2

    settled = restarted.settle(
        session="2026-08-04", at=datetime(2026, 8, 4, 9, 0, tzinfo=ET)
    )
    assert settled.unsettled_cash == pytest.approx(0.0)
    assert settled.pending_settlements == ()
    assert settled.settled_cash == pytest.approx(settled.available_cash)


def test_event_replay_reconciliation_detects_persisted_account_drift_after_restart():
    engine = _engine()
    cycle, decision_id = _prepare_cycle(engine)
    cycle.materialize_open(
        decision_id,
        open_prices={"SPY": 100.0, "BIL": 100.0},
        published_at=datetime(2026, 8, 3, 9, 31, tzinfo=ET),
    )
    with engine.begin() as conn:
        row = conn.execute(select(paper_accounts)).mappings().one()
        payload = dict(row["positions_json"])
        payload["_meta"] = {
            **dict(payload.get("_meta") or {}),
            "total_commission": float(dict(payload.get("_meta") or {}).get("total_commission") or 0.0)
            + 1.0,
        }
        conn.execute(
            paper_accounts.update()
            .where(paper_accounts.c.id == row["id"])
            .values(positions_json=payload)
        )

    restarted = PaperCycle(
        Config(strategy_version="SV-001"), engine=engine, notifier=lambda _m, _t: "id"
    )
    result = restarted.materialize_open(
        decision_id,
        open_prices={"SPY": 100.0, "BIL": 100.0},
        published_at=datetime(2026, 8, 3, 9, 32, tzinfo=ET),
    )
    assert result.status == PaperCycleStatus.HALTED
    assert result.account.risk_state == "RECONCILIATION_HALTED"
    with engine.connect() as conn:
        incident = conn.execute(
            select(risk_incidents).where(
                risk_incidents.c.code == "PAPER_RECONCILIATION_LOCK"
            )
        ).mappings().one()
        assert incident["notification_status"] == "SENT"


def test_late_approval_is_missed_and_notification_retries_without_duplication():
    engine = _engine()
    sender_calls: list[int] = []

    def failing_sender(_message, _title):
        with engine.connect() as conn:
            sender_calls.append(
                conn.execute(select(func.count()).select_from(risk_incidents)).scalar_one()
            )
        raise RuntimeError("offline")

    cycle = PaperCycle(
        Config(strategy_version="SV-001"), engine=engine, notifier=failing_sender
    )
    cycle.initialize_account(strategy_version="SV-001")
    stored = cycle.persist_decision(
        _decision(), recorded_at=datetime(2026, 7, 31, 20, 32, tzinfo=ET)
    )
    decision_id = int(stored.decision.decision_id)
    cycle.draft_orders(
        decision_id,
        reference_prices={"SPY": 100.0, "BIL": 100.0},
        median_daily_dollar_volume={"SPY": 100_000_000.0, "BIL": 100_000_000.0},
        verified_at=datetime(2026, 8, 3, 9, 20, tzinfo=ET),
    )
    with pytest.raises(ValueError, match="09:25"):
        cycle.approve(
            decision_id,
            approved_by="operator",
            approved_at=datetime(2026, 8, 3, 9, 25, tzinfo=ET),
        )
    assert sender_calls == [1]

    restarted = PaperCycle(
        Config(strategy_version="SV-001"),
        engine=engine,
        notifier=lambda _m, _t: "retry-id",
    )
    assert list(restarted.flush_notifications().values()) == ["SENT"]
    assert restarted.flush_notifications() == {}
    with engine.connect() as conn:
        incident = conn.execute(select(risk_incidents)).mappings().one()
        assert incident["notification_attempts"] == 2
        assert incident["notification_status"] == "SENT"
        assert conn.execute(select(func.count()).select_from(risk_incidents)).scalar_one() == 1
        states = set(conn.execute(select(order_intents.c.status)).scalars())
        assert states == {"MISSED"}


def test_drawdown_halt_persists_and_creates_unapproved_next_session_liquidation():
    engine = _engine()
    cycle, decision_id = _trigger_drawdown(engine)

    restarted = PaperCycle(
        Config(strategy_version="SV-001"), engine=engine, notifier=lambda _m, _t: "id"
    )
    again = restarted.materialize_open(
        decision_id,
        open_prices={"SPY": 80.0, "BIL": 100.0},
        published_at=datetime(2026, 8, 3, 9, 32, tzinfo=ET),
    )
    assert again.status == PaperCycleStatus.HALTED
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(execution_fills)).scalar_one() == 0
        liquidations = tuple(
            conn.execute(
                select(order_intents).where(
                    order_intents.c.order_type == "REPLAY_OPEN_LIQUIDATION"
                )
            ).mappings()
        )
        assert len(liquidations) == 1
        assert liquidations[0]["status"] == "DRAFT"
        assert liquidations[0]["execution_session"] == "2026-08-04"
        assert conn.execute(select(paper_cycles.c.status)).scalar_one() == "HALTED"
        assert conn.execute(
            select(func.count()).select_from(risk_incidents).where(
                risk_incidents.c.code == "DRAWDOWN_HALTED"
            )
        ).scalar_one() == 1


def test_drawdown_liquidation_requires_approval_fills_at_next_raw_open_and_is_restart_idempotent():
    engine = _engine()
    _cycle, decision_id = _trigger_drawdown(engine)
    restarted = PaperCycle(
        Config(strategy_version="SV-001"), engine=engine, notifier=lambda _m, _t: "id"
    )
    approved = restarted.approve_liquidation(
        decision_id,
        approved_by="risk-operator",
        approved_at=datetime(2026, 8, 4, 9, 24, tzinfo=ET),
    )
    assert len(approved) == 1
    first = restarted.materialize_liquidation_open(
        decision_id,
        open_prices={"SPY": 79.0},
        published_at=datetime(2026, 8, 4, 9, 31, tzinfo=ET),
    )
    assert first.status == PaperCycleStatus.HALTED
    assert first.account.positions == {}
    assert first.account.unsettled_cash > 0.0
    assert first.account.risk_state == "DRAWDOWN_HALTED"

    second_restart = PaperCycle(
        Config(strategy_version="SV-001"), engine=engine, notifier=lambda _m, _t: "id"
    )
    second = second_restart.materialize_liquidation_open(
        decision_id,
        open_prices={"SPY": 79.0},
        published_at=datetime(2026, 8, 4, 9, 32, tzinfo=ET),
    )
    assert second.account.positions == {}
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(execution_fills)).scalar_one() == 1
        assert conn.execute(select(func.count()).select_from(paper_cash_movements)).scalar_one() == 1
        liquidation = conn.execute(
            select(order_intents).where(
                order_intents.c.order_type == "REPLAY_OPEN_LIQUIDATION"
            )
        ).mappings().one()
        assert liquidation["status"] == "FILLED"
        assert liquidation["approved_by"] == "risk-operator"


def test_drawdown_recovery_waits_until_next_month_end_and_resets_high_water():
    engine = _engine()
    _cycle, decision_id = _trigger_drawdown(engine)
    cycle = PaperCycle(
        Config(strategy_version="SV-001"), engine=engine, notifier=lambda _m, _t: "id"
    )
    cycle.approve_liquidation(
        decision_id,
        approved_by="risk-operator",
        approved_at=datetime(2026, 8, 4, 9, 24, tzinfo=ET),
    )
    cycle.materialize_liquidation_open(
        decision_id,
        open_prices={"SPY": 79.0},
        published_at=datetime(2026, 8, 4, 9, 31, tzinfo=ET),
    )
    cycle.settle(session="2026-08-05", at=datetime(2026, 8, 5, 8, 0, tzinfo=ET))
    reconciliation = cycle.reconcile_halted_account(
        decision_id,
        prices={},
        at=datetime(2026, 8, 5, 9, 0, tzinfo=ET),
    )
    assert reconciliation.account.positions == {}
    with engine.connect() as conn:
        assert conn.execute(
            select(reconciliations.c.status).where(
                reconciliations.c.id == reconciliation.reconciliation_id
            )
        ).scalar_one() == "matched"

    with pytest.raises(ValueError, match="not eligible"):
        cycle.authorize_risk_recovery(
            reconciliation_id=int(reconciliation.reconciliation_id),
            authorized_by="risk-operator",
            note="Liquidation and account reconciliation reviewed.",
            at=datetime(2026, 8, 28, 16, 0, tzinfo=ET),
        )
    recovered = cycle.authorize_risk_recovery(
        reconciliation_id=int(reconciliation.reconciliation_id),
        authorized_by="risk-operator",
        note="Liquidation and account reconciliation reviewed.",
        at=datetime(2026, 8, 31, 20, 31, tzinfo=ET),
    )
    assert recovered.risk_state == "NORMAL"
    assert recovered.high_water == pytest.approx(recovered.nav)

    valued = cycle.value_account(
        prices={},
        valuation_session="2026-08-31",
        at=datetime(2026, 8, 31, 20, 32, tzinfo=ET),
        decision_id=decision_id,
    )
    assert valued.risk_state == "NORMAL"
    assert valued.high_water == pytest.approx(valued.nav)
    with engine.connect() as conn:
        incident = conn.execute(
            select(risk_incidents).where(
                risk_incidents.c.code == "DRAWDOWN_HALTED"
            )
        ).mappings().one()
        assert incident["status"] == "resolved"
        assert incident["recovery_authorized_by"] == "risk-operator"


def test_halted_cross_day_retry_reuses_liquidation_and_missed_requires_explicit_redraft():
    engine = _engine()
    _cycle, decision_id = _trigger_drawdown(engine)
    restarted = PaperCycle(
        Config(strategy_version="SV-001"), engine=engine, notifier=lambda _m, _t: "id"
    )
    with pytest.raises(ValueError, match="09:25"):
        restarted.approve_liquidation(
            decision_id,
            approved_by="risk-operator",
            approved_at=datetime(2026, 8, 4, 9, 25, tzinfo=ET),
        )
    with pytest.raises(ValueError, match="wrong execution session"):
        restarted.materialize_open(
            decision_id,
            open_prices={"SPY": 80.0},
            published_at=datetime(2026, 8, 4, 9, 31, tzinfo=ET),
        )
    with engine.connect() as conn:
        first = tuple(
            conn.execute(
                select(order_intents).where(
                    order_intents.c.order_type == "REPLAY_OPEN_LIQUIDATION"
                )
            ).mappings()
        )
        assert len(first) == 1
        assert first[0]["status"] == "MISSED"

    redrafted = restarted.redraft_liquidation(
        decision_id,
        reference_prices={"SPY": 80.0},
        requested_by="risk-operator",
        reason="Missed prior approval window after workstation outage.",
        requested_at=datetime(2026, 8, 4, 10, 0, tzinfo=ET),
    )
    assert len(redrafted) == 1
    with engine.connect() as conn:
        rows = tuple(
            conn.execute(
                select(order_intents).where(
                    order_intents.c.order_type == "REPLAY_OPEN_LIQUIDATION"
                )
            ).mappings()
        )
        assert len(rows) == 2
        assert {row["execution_session"] for row in rows} == {
            "2026-08-04",
            "2026-08-05",
        }
        assert conn.execute(
            select(func.count()).select_from(risk_incidents).where(
                risk_incidents.c.code == "PAPER_LIQUIDATION_REDRAFTED"
            )
        ).scalar_one() == 1


def test_partial_and_filled_states_are_derived_from_cumulative_persisted_fills():
    engine = _engine()
    cycle, _decision_id = _prepare_cycle(engine)
    intent = cycle.execution.list_intents()[0]
    account = cycle.execution.get_account("local-paper")
    quote = Quote(intent.ticker, 100.0, 100.0, datetime(2026, 8, 3, 9, 31, tzinfo=ET))
    oms = OrderManagementSystem(
        Config(strategy_version="SV-001"),
        InMemoryPaperBroker(account, {intent.ticker: quote}),
        repository=cycle.execution,
    )
    submitted = oms.submit(intent.client_order_id)
    half = submitted.quantity / 2.0
    first = ExecutionFill(
        client_order_id=submitted.client_order_id,
        broker_execution_id="partial-1",
        filled_at=datetime(2026, 8, 3, 9, 31, tzinfo=ET),
        quantity=half,
        price=100.0,
        commission=0.25,
        implementation_shortfall_bps=0.0,
        settlement_date="2026-08-04",
    )
    assert oms.record_fill(
        submitted.client_order_id, first, account_ref="local-paper"
    ).state == OrderState.PARTIAL
    with pytest.raises(ValueError, match="persisted fills"):
        oms.update_status(submitted.client_order_id, "FILLED")

    restarted_cycle = PaperCycle(
        Config(strategy_version="SV-001"), engine=engine, notifier=lambda _m, _t: "id"
    )
    partial = restarted_cycle.execution.get_intent(submitted.client_order_id)
    restarted_oms = OrderManagementSystem(
        Config(strategy_version="SV-001"),
        InMemoryPaperBroker(
            restarted_cycle.execution.get_account("local-paper"),
            {intent.ticker: quote},
        ),
        repository=restarted_cycle.execution,
    )
    second = ExecutionFill(
        client_order_id=partial.client_order_id,
        broker_execution_id="partial-2",
        filled_at=datetime(2026, 8, 3, 9, 32, tzinfo=ET),
        quantity=partial.remaining_quantity,
        price=100.0,
        commission=0.25,
        implementation_shortfall_bps=0.0,
        settlement_date="2026-08-04",
    )
    assert restarted_oms.record_fill(
        partial.client_order_id, second, account_ref="local-paper"
    ).state == OrderState.FILLED
    restarted_oms.record_fill(
        partial.client_order_id, second, account_ref="local-paper"
    )
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(execution_fills)).scalar_one() == 2


def test_daily_loss_halt_persists_without_automatic_liquidation_or_completion():
    engine = _engine()
    cycle = PaperCycle(
        Config(strategy_version="SV-001"), engine=engine, notifier=lambda _m, _t: "id"
    )
    cycle.initialize_account(strategy_version="SV-001")
    with engine.begin() as conn:
        conn.execute(
            paper_accounts.update().values(
                nav=10_000.0,
                settled_cash=0.0,
                available_cash=0.0,
                positions_json={
                    "SPY": {"quantity": 100.0, "mark_price": 100.0},
                    "_meta": {"total_commission": 0.0},
                },
                high_water=10_000.0,
            )
        )
    stored = cycle.persist_decision(
        _decision(), recorded_at=datetime(2026, 7, 31, 20, 32, tzinfo=ET)
    )
    decision_id = int(stored.decision.decision_id)
    cycle.draft_orders(
        decision_id,
        reference_prices={"SPY": 100.0, "BIL": 100.0},
        median_daily_dollar_volume={"SPY": 100_000_000.0, "BIL": 100_000_000.0},
        verified_at=datetime(2026, 8, 3, 9, 20, tzinfo=ET),
    )
    cycle.approve(
        decision_id,
        approved_by="operator",
        approved_at=datetime(2026, 8, 3, 9, 24, tzinfo=ET),
    )
    result = cycle.materialize_open(
        decision_id,
        open_prices={"SPY": 94.0, "BIL": 100.0},
        published_at=datetime(2026, 8, 3, 9, 31, tzinfo=ET),
    )
    assert result.status == PaperCycleStatus.HALTED
    assert result.account.risk_state == "DAILY_LOSS_HALTED"
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(execution_fills)).scalar_one() == 0
        assert conn.execute(
            select(func.count()).select_from(order_intents).where(
                order_intents.c.order_type == "REPLAY_OPEN_LIQUIDATION"
            )
        ).scalar_one() == 0
        assert conn.execute(
            select(func.count()).select_from(risk_incidents).where(
                risk_incidents.c.code == "DAILY_LOSS_HALTED"
            )
        ).scalar_one() == 1

    reconciliation = cycle.reconcile_halted_account(
        decision_id,
        prices={"SPY": 94.0},
        at=datetime(2026, 8, 3, 9, 32, tzinfo=ET),
    )
    with pytest.raises(ValueError, match="not eligible"):
        cycle.authorize_risk_recovery(
            reconciliation_id=int(reconciliation.reconciliation_id),
            authorized_by="risk-operator",
            note="Daily loss and account reconciliation reviewed.",
            at=datetime(2026, 8, 3, 16, 0, tzinfo=ET),
        )
    recovered = cycle.authorize_risk_recovery(
        reconciliation_id=int(reconciliation.reconciliation_id),
        authorized_by="risk-operator",
        note="Daily loss and account reconciliation reviewed.",
        at=datetime(2026, 8, 4, 9, 0, tzinfo=ET),
    )
    assert recovered.risk_state == "NORMAL"
    assert recovered.high_water == pytest.approx(10_000.0)


def test_no_trade_cycle_still_has_independent_baseline_and_completes():
    engine = _engine()
    cycle = PaperCycle(
        Config(strategy_version="SV-001"), engine=engine, notifier=lambda _m, _t: "id"
    )
    cycle.initialize_account(strategy_version="SV-001")
    no_trade = replace(
        _decision(),
        target_weights={"CASH_USD": 1.0},
        current_weights={"CASH_USD": 1.0},
        weight_deltas={"CASH_USD": 0.0},
        dollar_deltas={"CASH_USD": 0.0},
        estimated_cost_dollars=0.0,
    )
    stored = cycle.persist_decision(
        no_trade, recorded_at=datetime(2026, 7, 31, 20, 32, tzinfo=ET)
    )
    decision_id = int(stored.decision.decision_id)
    assert cycle.draft_orders(
        decision_id,
        reference_prices={},
        median_daily_dollar_volume={},
        verified_at=datetime(2026, 8, 3, 9, 20, tzinfo=ET),
    ) == ()
    assert cycle.approve(
        decision_id,
        approved_by="operator",
        approved_at=datetime(2026, 8, 3, 9, 24, tzinfo=ET),
    ) == ()
    result = cycle.materialize_open(
        decision_id,
        open_prices={},
        published_at=datetime(2026, 8, 3, 9, 31, tzinfo=ET),
    )
    assert result.status == PaperCycleStatus.COMPLETED
    assert result.account.nav == pytest.approx(10_000.0)


@pytest.mark.parametrize(
    "mutation,error",
    [
        ({"strategy_status": "draft"}, "frozen strategy"),
        ({"admission_status": "rejected"}, "ADMITTED run"),
        ({"local_sim_start": None}, "simulation clock"),
        ({"snapshot_status": "BLOCKED"}, "admission snapshot"),
    ],
)
def test_invalid_governance_cannot_create_paper_account_or_cycle(mutation, error):
    engine = _engine()
    with engine.begin() as conn:
        if "strategy_status" in mutation:
            conn.execute(
                strategy_versions.update().values(status=mutation["strategy_status"])
            )
        if "admission_status" in mutation:
            conn.execute(
                admission_runs.update().values(status=mutation["admission_status"])
            )
        if "local_sim_start" in mutation:
            conn.execute(strategy_versions.update().values(local_sim_start=None))
        if "snapshot_status" in mutation:
            row = conn.execute(select(dataset_snapshots)).mappings().one()
            quality = {**dict(row["quality_json"]), "status": "BLOCKED"}
            conn.execute(
                dataset_snapshots.update().values(
                    status="BLOCKED", quality_json=quality
                )
            )
    cycle = PaperCycle(
        Config(strategy_version="SV-001"), engine=engine, notifier=lambda _m, _t: "id"
    )
    with pytest.raises(ValueError, match=error):
        cycle.initialize_account(strategy_version="SV-001")
    with pytest.raises(ValueError, match=error):
        cycle.persist_decision(
            _decision(), recorded_at=datetime(2026, 7, 31, 20, 32, tzinfo=ET)
        )
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(paper_accounts)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(paper_cycles)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(signal_decisions)).scalar_one() == 0


def test_direct_ensure_cycle_rechecks_governance_after_decision_persistence():
    engine = _engine()
    repository = SignalRepository(engine=engine)
    decision = repository.save_decision(_decision(), environment="PAPER")
    with engine.begin() as conn:
        row = conn.execute(select(dataset_snapshots)).mappings().one()
        conn.execute(
            dataset_snapshots.update().values(
                status="BLOCKED",
                quality_json={**dict(row["quality_json"]), "status": "BLOCKED"},
            )
        )
    with pytest.raises(ValueError, match="admission snapshot"):
        repository.ensure_paper_cycle(int(decision.decision_id))
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(paper_cycles)).scalar_one() == 0


def test_zero_or_negative_initial_cash_is_rejected_without_creating_account():
    for value in (0.0, -1.0):
        engine = _engine()
        cycle = PaperCycle(
            Config(strategy_version="SV-001"),
            engine=engine,
            notifier=lambda _m, _t: "id",
        )
        with pytest.raises(ValueError, match="positive"):
            cycle.initialize_account(
                strategy_version="SV-001", initial_cash=value
            )
        with engine.connect() as conn:
            assert conn.execute(
                select(func.count()).select_from(paper_accounts)
            ).scalar_one() == 0


def test_actionable_timing_and_snapshot_session_are_enforced_at_cycle_boundary():
    invalid = (
        (replace(_decision(), signal_session="2026-07-30"), "month-end"),
        (
            replace(_decision(), next_rebalance_session="2026-08-04"),
            "next NYSE session",
        ),
        (replace(_decision(), data_as_of="2026-07-30"), "data_as_of"),
        (
            replace(_decision(), generated_at="2026-07-31T20:29:59-04:00"),
            "T 20:30",
        ),
        (
            replace(_decision(), generated_at="2026-08-03T09:25:00-04:00"),
            "T 20:30",
        ),
    )
    for decision, message in invalid:
        engine = _engine()
        cycle = PaperCycle(
            Config(strategy_version="SV-001"),
            engine=engine,
            notifier=lambda _m, _t: "id",
        )
        with pytest.raises(ValueError, match=message):
            cycle.persist_decision(
                decision, recorded_at=datetime(2026, 7, 31, 20, 32, tzinfo=ET)
            )

    engine = _engine()
    with engine.begin() as conn:
        row = conn.execute(select(dataset_snapshots)).mappings().one()
        conn.execute(
            dataset_snapshots.update().values(
                quality_json={
                    **dict(row["quality_json"]),
                    "latest_session": "2026-07-30",
                    "expected_session": "2026-07-30",
                }
            )
        )
    with pytest.raises(ValueError, match="sessions"):
        PaperCycle(
            Config(strategy_version="SV-001"),
            engine=engine,
            notifier=lambda _m, _t: "id",
        ).persist_decision(
            _decision(), recorded_at=datetime(2026, 7, 31, 20, 32, tzinfo=ET)
        )


def test_later_actionable_snapshot_is_allowed_but_blocked_snapshot_is_rejected():
    engine = _engine()
    with engine.begin() as conn:
        _insert_snapshot(conn, snapshot_id=2, hash_character="e")
    current = replace(_decision(), dataset_snapshot_id=2)
    cycle = PaperCycle(
        Config(strategy_version="SV-001"), engine=engine, notifier=lambda _m, _t: "id"
    )
    stored = cycle.persist_decision(
        current, recorded_at=datetime(2026, 7, 31, 20, 32, tzinfo=ET)
    )
    assert stored.decision.dataset_snapshot_id == 2

    blocked_engine = _engine()
    with blocked_engine.begin() as conn:
        _insert_snapshot(
            conn, snapshot_id=2, status="BLOCKED", hash_character="f"
        )
    with pytest.raises(ValueError, match="current dataset snapshot"):
        PaperCycle(
            Config(strategy_version="SV-001"),
            engine=blocked_engine,
            notifier=lambda _m, _t: "id",
        ).persist_decision(
            replace(_decision(), dataset_snapshot_id=2),
            recorded_at=datetime(2026, 7, 31, 20, 32, tzinfo=ET),
        )


def test_compatible_later_universe_is_allowed_and_policy_change_is_rejected():
    engine = _engine()
    with engine.begin() as conn:
        conn.execute(
            universe_versions.insert().values(
                version="UV-002",
                effective_date="2026-07-31",
                status="approved",
                seed_tickers_json=["SPY", "BIL"],
                rules_json={"point_in_time": True},
                eligibility_json=[{"ticker": "SPY", "eligible": True}],
                approved_at=datetime(2026, 7, 31, 12, 0, tzinfo=ET),
                approved_by="operator-2",
                historical_universe_integrity=0,
            )
        )
    cycle = PaperCycle(
        Config(strategy_version="SV-001"), engine=engine, notifier=lambda _m, _t: "id"
    )
    stored = cycle.persist_decision(
        replace(_decision(), universe_version="UV-002"),
        recorded_at=datetime(2026, 7, 31, 20, 32, tzinfo=ET),
    )
    assert stored.decision.universe_version == "UV-002"

    for changes in (
        {"seed_tickers_json": ["QQQ", "BIL"]},
        {"rules_json": {"point_in_time": False}},
        {"historical_universe_integrity": 1},
        {"effective_date": "2026-08-01"},
    ):
        incompatible = _engine()
        with incompatible.begin() as conn:
            values = {
                "version": "UV-002",
                "effective_date": "2026-07-31",
                "status": "approved",
                "seed_tickers_json": ["SPY", "BIL"],
                "rules_json": {"point_in_time": True},
                "approved_at": datetime(2026, 7, 31, 12, 0, tzinfo=ET),
                "approved_by": "operator-2",
                "historical_universe_integrity": 0,
                **changes,
            }
            conn.execute(universe_versions.insert().values(**values))
        with pytest.raises(ValueError, match="policy|effective"):
            PaperCycle(
                Config(strategy_version="SV-001"),
                engine=incompatible,
                notifier=lambda _m, _t: "id",
            ).persist_decision(
                replace(_decision(), universe_version="UV-002"),
                recorded_at=datetime(2026, 7, 31, 20, 32, tzinfo=ET),
            )
