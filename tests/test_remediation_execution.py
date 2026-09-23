from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from config.settings import Config
from execution.models import AccountSnapshot, BrokerPosition
from execution.oms import reconcile_account
from services.paper_cycle import PaperCycle
from storage.repositories.execution import ExecutionRepository
from storage.schema import paper_accounts
from tests.test_paper_cycle import ET, _decision, _engine, _draft_orders, _materialize_open, _prepare_cycle, _execution_snapshot


def test_explicit_empty_positions_reject_unknown_fractional_holding():
    expected = AccountSnapshot("audit", 10000, 10000, 10000, 10000, {}, datetime.now(timezone.utc))
    actual = replace(expected, nav=10001, positions={"SPY": BrokerPosition("SPY", .01, 1)})
    result = reconcile_account(account=actual, expected_account=expected)
    assert not result.matched
    assert result.unknown_positions == ("SPY",)
    assert result.quantity_differences == {"SPY": .01}


def test_full_investment_budget_completes_without_partial_cash_failure():
    engine = _engine()
    cycle = PaperCycle(Config(strategy_version="SV-001"), engine=engine, notifier=lambda *_: "test")
    cycle.initialize_account(strategy_version="SV-001", at=datetime(2026, 7, 31, 15, tzinfo=ET))
    cycle.execution.record_session_close("local-paper", session="2026-07-31", prices={},
        recorded_at=datetime(2026, 7, 31, 20, tzinfo=ET), source_snapshot_id=1)
    decision = replace(_decision(), target_weights={"SPY": .35, "BIL": .65})
    stored = cycle.persist_decision(decision, recorded_at=datetime(2026, 7, 31, 20, 32, tzinfo=ET))
    decision_id = stored.decision.decision_id
    _draft_orders(cycle,decision_id, reference_prices={"SPY": 100, "BIL": 100},
                       median_daily_dollar_volume={"SPY": 1e8, "BIL": 1e8},
                       verified_at=datetime(2026, 8, 3, 9, 20, tzinfo=ET))
    cycle.approve(decision_id, approved_by="operator", approved_at=datetime(2026, 8, 3, 9, 24, tzinfo=ET))
    result = _materialize_open(cycle,decision_id, open_prices={"SPY": 100, "BIL": 100},
                                    published_at=datetime(2026, 8, 3, 9, 31, tzinfo=ET))
    assert result.status.value == "COMPLETED"
    assert result.account.available_cash >= 50 - 1e-6


@pytest.mark.parametrize("bad_price", [float("nan"), float("inf"), -1, 0])
def test_invalid_mark_never_changes_financial_account(bad_price):
    engine = _engine()
    repo = ExecutionRepository(engine=engine)
    repo.initialize_account(account_ref="audit", strategy_version="SV-001", initial_cash=10000)
    with engine.begin() as conn:
        conn.execute(paper_accounts.update().values(settled_cash=0, available_cash=0,
            positions_json={"SPY": {"quantity": 100, "mark_price": 100}}))
    before = repo.get_account("audit")
    with pytest.raises(ValueError):
        repo.mark_account("audit", prices={"SPY": bad_price}, valuation_session="2026-08-03",
                          at=datetime(2026, 8, 3, 16, tzinfo=ET), drawdown_limit=.15, daily_loss_limit=.05)
    assert repo.get_account("audit") == before


def _risk_repo(*, diversified=False):
    engine = _engine()
    repo = ExecutionRepository(engine=engine)
    repo.initialize_account(account_ref="audit", strategy_version="SV-001", initial_cash=10000,
                            at=datetime(2026, 7, 31, 15, tzinfo=ET))
    positions = {"SPY": {"quantity": 35 if diversified else 100, "mark_price": 100}}
    if diversified:
        positions["BIL"] = {"quantity": 65, "mark_price": 100}
    with engine.begin() as conn:
        conn.execute(paper_accounts.update().values(settled_cash=0, available_cash=0,
            positions_json=positions, accounting_state_json={"average_costs": {k: 100 for k in positions},
                                                            "started_session": "2026-07-31"}))
    repo.record_session_close("audit", session="2026-07-31", prices={k: 100 for k in positions},
                              recorded_at=datetime(2026, 7, 31, 21, tzinfo=ET),
                              source_snapshot_id=_execution_snapshot(engine, "2026-07-31", {k: 100 for k in positions}))
    return engine, repo


def _mark(repo, day, hour, prices):
    return repo.mark_account("audit", prices=prices, valuation_session=day,
        at=datetime.fromisoformat(day).replace(hour=hour, tzinfo=ET), drawdown_limit=.15, daily_loss_limit=.05)[0]


def test_same_day_loss_and_later_drawdown_survive_restart_and_coexist():
    engine, repo = _risk_repo()
    _mark(repo, "2026-08-03", 9, {"SPY": 100})
    loss = _mark(ExecutionRepository(engine=engine), "2026-08-03", 16, {"SPY": 94})
    assert "DAILY_LOSS_HALTED" in loss.halt_reasons
    repo.record_session_close("audit", session="2026-08-03", prices={"SPY": 94},
                              recorded_at=datetime(2026, 8, 3, 21, tzinfo=ET),
                              source_snapshot_id=_execution_snapshot(engine, "2026-08-03", {"SPY": 94}))
    crashed = _mark(ExecutionRepository(engine=engine), "2026-08-04", 16, {"SPY": 80})
    assert set(crashed.halt_reasons) == {"DRAWDOWN_HALTED", "DAILY_LOSS_HALTED"}
    assert crashed.risk_state == "DRAWDOWN_HALTED"
    assert crashed.drift_state == "DRIFT_REVIEW"


def test_drift_and_missing_previous_close_are_independent_controls():
    engine, repo = _risk_repo(diversified=True)
    drift = _mark(repo, "2026-08-03", 9, {"SPY": 150, "BIL": 100})
    assert drift.drift_state == "DRIFT_REVIEW"
    assert drift.risk_state == "DRIFT_REVIEW"
    missing = _mark(repo, "2026-08-04", 9, {"SPY": 150, "BIL": 100})
    assert "VALUATION_HALTED" in missing.halt_reasons
    assert missing.drift_state == "DRIFT_REVIEW"


def test_dividend_is_receivable_before_payment_and_journal_is_idempotent():
    import pandas as pd
    from data.models import CorporateAction
    from storage.schema import paper_account_actions
    engine, repo = _risk_repo()
    action = CorporateAction("SPY", pd.Timestamp("2026-08-03"), "dividend", cash_amount=1,
                             payment_date=pd.Timestamp("2026-08-04"), payment_source="issuer")
    repo.process_corporate_actions("audit", actions=[action], session="2026-08-03", at=datetime(2026, 8, 3, 9, tzinfo=ET))
    ex_date = _mark(repo, "2026-08-03", 16, {"SPY": 99})
    assert ex_date.dividend_receivable == 100
    assert ex_date.nav == 10000
    assert ex_date.available_cash == 0
    restarted = ExecutionRepository(engine=engine)
    for _ in range(2):
        paid = restarted.process_corporate_actions("audit", actions=[action], session="2026-08-04", at=datetime(2026, 8, 4, 9, tzinfo=ET))
    assert paid.dividend_receivable == 0
    assert paid.available_cash == 100
    with engine.connect() as conn:
        phases = conn.execute(select(paper_account_actions.c.phase)).scalars().all()
    assert sorted(phases) == ["EX_DATE", "PAYMENT"]


def test_stale_account_draft_batch_cannot_overwrite_a_new_valuation():
    _, repo = _risk_repo()
    stale = repo.get_account("audit")
    updated = _mark(repo, "2026-08-03", 9, {"SPY": 100})
    with pytest.raises(RuntimeError, match="changed"):
        repo.save_draft_batch([], account=stale, paper_cycle_id=None)
    assert repo.get_account("audit").version == updated.version


def test_open_gap_budget_failure_does_not_apply_the_first_fill():
    from sqlalchemy import func
    from storage.schema import execution_fills
    engine = _engine()
    cycle, decision_id = _prepare_cycle(engine)
    with pytest.raises(ValueError, match="basket exceeds"):
        _materialize_open(cycle, decision_id, open_prices={"SPY": 400, "BIL": 400},
                          published_at=datetime(2026, 8, 3, 9, 31, tzinfo=ET))
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(execution_fills)).scalar_one() == 0
    assert cycle.execution.get_account("local-paper").available_cash == 10000


def test_restart_resumes_only_remaining_fills_and_freezes_open_inputs(monkeypatch):
    from storage.schema import execution_fills
    engine = _engine()
    cycle, decision_id = _prepare_cycle(engine)
    original = cycle.execution.apply_paper_fill
    calls = 0

    def interrupt_second(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected restart")
        return original(**kwargs)

    monkeypatch.setattr(cycle.execution, "apply_paper_fill", interrupt_second)
    with pytest.raises(RuntimeError, match="injected restart"):
        _materialize_open(cycle, decision_id, open_prices={"SPY": 100, "BIL": 100},
                          published_at=datetime(2026, 8, 3, 9, 31, tzinfo=ET))
    restarted = PaperCycle(Config(strategy_version="SV-001"), engine=engine, notifier=lambda *_: "test")
    interrupted_account = restarted.execution.get_account("local-paper")
    with pytest.raises(ValueError, match="immutable"):
        _materialize_open(restarted, decision_id, open_prices={"SPY": 100, "BIL": 101},
                          published_at=datetime(2026, 8, 3, 9, 32, tzinfo=ET))
    assert restarted.execution.get_account("local-paper") == interrupted_account
    result = _materialize_open(restarted, decision_id, open_prices={"SPY": 100, "BIL": 100},
                                published_at=datetime(2026, 8, 3, 9, 33, tzinfo=ET))
    assert result.status.value == "COMPLETED"
    with engine.connect() as conn:
        assert len(conn.execute(select(execution_fills)).all()) == 2


def test_late_corporate_action_blocks_approved_quantities_before_fill():
    from sqlalchemy import func
    from storage.schema import execution_fills, dataset_snapshot_actions
    engine = _engine()
    cycle, decision_id = _prepare_cycle(engine)
    snapshot_id = _execution_snapshot(engine, "2026-08-03", {"SPY": 100, "BIL": 100})
    with engine.begin() as conn:
        conn.execute(dataset_snapshot_actions.insert().values(snapshot_id=snapshot_id, ticker="SPY",
            ex_date="2026-08-03", action_type="split", role="primary", cash_amount=0, split_factor=2,
            status="active", source="tiingo"))
    with pytest.raises(ValueError, match="actions changed"):
        cycle.materialize_open(decision_id, open_prices={"SPY": 100, "BIL": 100},
            published_at=datetime(2026, 8, 3, 9, 31, tzinfo=ET), source_snapshot_id=snapshot_id)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(execution_fills)).scalar_one() == 0


def _submitted_fill(repo):
    from execution.models import BrokerEnvironment, ExecutionFill, OrderIntent, OrderState, Quote, Side
    at = datetime(2026, 8, 3, 9, 31, tzinfo=ET)
    intent = OrderIntent("cas-fill", BrokerEnvironment.PAPER, "SV-001", "2026-07-31",
        "SPY", Side.SELL, 10, 100, Quote("SPY", 100, 100, at), .001, 0,
        state=OrderState.SUBMITTED, created_at=at)
    order_id = repo.save_intent(intent)
    fill = ExecutionFill("cas-fill", "cas-execution", at, 10, 100, 1, 0, "2026-08-04")
    return order_id, fill


@pytest.mark.parametrize("operation", ["fill", "settlement", "actions"])
def test_account_cas_conflict_rolls_back_all_financial_side_effects(monkeypatch, operation):
    import pandas as pd
    from data.models import CorporateAction
    from storage.schema import execution_fills, paper_cash_movements, paper_account_actions, order_intents

    engine, repo = _risk_repo()
    order_id, fill = _submitted_fill(repo)
    if operation == "settlement":
        repo.apply_paper_fill(account_ref="audit", order_intent_id=order_id, fill=fill)
    tables = (paper_accounts, order_intents, execution_fills, paper_cash_movements, paper_account_actions)

    def financial_rows():
        with engine.connect() as conn:
            return {table.name: [dict(row) for row in conn.execute(select(table)).mappings()] for table in tables}

    before = financial_rows()
    original = repo._update_account

    def force_stale_version(conn, account, **values):
        # Force the account predicate to miss after the ledger writes, proving
        # that a conflict rolls the complete transaction back.
        conn.execute(paper_accounts.update().where(paper_accounts.c.id == account["id"])
                     .values(version=int(account["version"]) + 1))
        original(conn, account, **values)

    monkeypatch.setattr(repo, "_update_account", force_stale_version)
    with pytest.raises(RuntimeError, match="Concurrent"):
        if operation == "fill":
            repo.apply_paper_fill(account_ref="audit", order_intent_id=order_id, fill=fill)
        elif operation == "settlement":
            repo.settle_due("audit", session="2026-08-04", settled_at=datetime(2026, 8, 4, 9, tzinfo=ET))
        else:
            action = CorporateAction("SPY", pd.Timestamp("2026-08-03"), "dividend", cash_amount=1)
            repo.process_corporate_actions("audit", actions=[action], session="2026-08-03",
                at=datetime(2026, 8, 3, 9, tzinfo=ET))
    assert financial_rows() == before


def test_formal_close_requires_observed_raw_close_and_is_immutable():
    engine, repo = _risk_repo()
    snapshot_id = _execution_snapshot(engine, "2026-08-03", {"SPY": 94})
    before = repo.get_account("audit")
    with pytest.raises(ValueError, match="explicit execution snapshot"):
        repo.record_session_close("audit", session="2026-08-03", prices={"SPY": 94},
            recorded_at=datetime(2026, 8, 3, 21, tzinfo=ET))
    with pytest.raises(ValueError, match="known at recording"):
        repo.record_session_close("audit", session="2026-08-03", prices={"SPY": 94},
            recorded_at=datetime(2026, 8, 3, 20, tzinfo=ET), source_snapshot_id=snapshot_id)
    with pytest.raises(ValueError, match="differs"):
        repo.record_session_close("audit", session="2026-08-03", prices={"SPY": 95},
            recorded_at=datetime(2026, 8, 3, 21, tzinfo=ET), source_snapshot_id=snapshot_id)
    assert repo.get_account("audit") == before
    args = dict(session="2026-08-03", prices={"SPY": 94},
        recorded_at=datetime(2026, 8, 3, 21, tzinfo=ET), source_snapshot_id=snapshot_id)
    first = repo.record_session_close("audit", **args)
    assert repo.record_session_close("audit", **args) == first
    assert repo.get_account("audit").version == before.version + 1


def test_no_order_baseline_does_not_reapply_prior_company_actions():
    import pandas as pd
    from data.models import CorporateAction

    engine, repo = _risk_repo(diversified=True)
    cycle = PaperCycle(Config(strategy_version="SV-001"), engine=engine, account_ref="audit",
                       notifier=lambda *_: "test")
    stored = cycle.persist_decision(_decision(), recorded_at=datetime(2026, 7, 31, 20, 32, tzinfo=ET))
    actions = [
        CorporateAction("BIL", pd.Timestamp("2026-08-03"), "split", split_factor=2),
        CorporateAction("SPY", pd.Timestamp("2026-08-03"), "dividend", cash_amount=1,
                        payment_date=pd.Timestamp("2026-08-04"), payment_source="issuer"),
    ]
    repo.process_corporate_actions("audit", actions=actions, session="2026-08-03",
        at=datetime(2026, 8, 3, 9, tzinfo=ET))
    marks = {"SPY": 99, "BIL": 50}
    account = _mark(repo, "2026-08-03", 9, marks)
    repo.save_draft_batch([], account=account, paper_cycle_id=stored.paper_cycle_id)
    repo.process_corporate_actions("audit", actions=actions, session="2026-08-04",
        at=datetime(2026, 8, 4, 9, tzinfo=ET))
    actual = _mark(repo, "2026-08-04", 9, marks)
    expected = repo.expected_account_from_cycle(account_ref="audit", paper_cycle_id=stored.paper_cycle_id,
        mark_prices=marks, at=datetime(2026, 8, 4, 10, tzinfo=ET))
    assert expected.positions == actual.positions
    assert expected.settled_cash == actual.settled_cash == 35
    assert expected.dividend_receivable == actual.dividend_receivable == 0
    assert expected.nav == actual.nav == 10000


@pytest.mark.parametrize("invalid_policy", ["approver_missing", "approval_time_missing", "runtime_changed"])
def test_restored_approved_order_rechecks_strategy_policy_before_submission(invalid_policy):
    from execution.adapters import InMemoryPaperBroker
    from execution.models import OrderState, Quote
    from execution.oms import OrderManagementSystem
    from storage.schema import strategy_versions

    engine = _engine()
    cycle, _ = _prepare_cycle(engine)
    repo = ExecutionRepository(engine=engine)
    intent = repo.list_intents()[0]
    assert intent.state == OrderState.APPROVED
    config = Config(strategy_version="SV-001")
    if invalid_policy == "runtime_changed":
        config = replace(config, trading_cost_bps=config.trading_cost_bps + 1)
        expected_error = "configuration"
    else:
        values = {"approved_by": None} if invalid_policy == "approver_missing" else {"approved_at": None}
        with engine.begin() as conn:
            conn.execute(strategy_versions.update().values(**values))
        expected_error = "approval"
    account = repo.get_account("local-paper")
    broker = InMemoryPaperBroker(account, {intent.ticker: Quote(intent.ticker, 100, 100,
        datetime(2026, 8, 3, 9, 31, tzinfo=ET))})
    restored = OrderManagementSystem(config, broker, repository=repo)
    with pytest.raises(ValueError, match=expected_error):
        restored.submit(intent.client_order_id)
    assert repo.get_intent(intent.client_order_id).state == OrderState.APPROVED
    assert repo.get_account("local-paper") == account
