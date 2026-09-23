"""Synthetic stress traverses actual features, strategy, orders, accounting and risk."""
import numpy as np
import pandas as pd
import pytest

from backtest.engine import Backtester
from config.settings import Config
from data.adjustments import locally_adjust_ohlcv
from data.calendar import NyseCalendar
from data.features import FeatureEngineer
from risk.engine import RiskEngine
from strategy.momentum_rotation import MomentumRotationStrategy
from strategy.regime import RegimeDetector


def stress_inputs(*, freeze_liquidity=False):
    sessions = NyseCalendar().sessions("2019-01-02", "2024-03-08")
    timeline = np.arange(len(sessions))
    spy = 100 * np.exp(.0003 * timeline + .0004 * np.sin(timeline))
    bil = 90 * np.exp(.00003 * timeline)
    shock = pd.Timestamp("2024-02-20")
    spy[sessions >= shock] *= .30
    data = {}
    for ticker, values in {"SPY": spy, "BIL": bil, "^VIX": np.full(len(sessions), 15.)}.items():
        close = pd.Series(values, index=sessions)
        frame = pd.DataFrame({"Open": close, "High": close * 1.01,
                              "Low": close * .99, "Close": close, "Volume": 10_000_000.})
        data[ticker] = locally_adjust_ohlcv(frame, ())
    config = Config(universe=["SPY", "BIL"], top_n=1)
    engineer = FeatureEngineer(data, config)
    prices = engineer.make_price_frame()
    returns = engineer.make_returns_frame(prices)
    adv = engineer.make_median_dollar_volume_frame()
    if freeze_liquidity:
        # The low capacity is known at the signal, before any basket executes.
        adv.loc["2024-01-31", "SPY"] = 1.0
    return config, engineer, prices, returns, adv, shock


def run_stress(*, freeze_liquidity=False):
    config, engineer, prices, returns, adv, shock = stress_inputs(freeze_liquidity=freeze_liquidity)
    result = Backtester(config, prices, returns, engineer.compute_features(prices, returns),
        RegimeDetector(config), MomentumRotationStrategy(config), RiskEngine(config),
        execution_prices=engineer.make_open_frame(), raw_close_prices=engineer.make_raw_close_frame(),
        corporate_actions=engineer.corporate_actions(), median_dollar_volume=adv,
        trade_start="2024-01-02", drawdown_reentry_mode="permanent_cash_stress").run()
    return result, shock


def test_gap_crash_generates_risk_liquidation_after_actual_strategy_fills():
    result, shock = run_stress()
    portfolio, orders = result["portfolio"], result["orders"]
    assert not result["signals"].empty
    assert ((orders.ticker == "SPY") & (orders.side == "BUY") & (orders.date < shock)).any()
    assert portfolio.loc[shock, "daily_return"] < -.15
    assert bool(portfolio.loc[shock, "stop_triggered"])
    next_session = NyseCalendar().next_session(shock)
    liquidations = orders[(orders.date == next_session) & (orders.side == "SELL")]
    assert "SPY" in set(liquidations.ticker)
    assert liquidations.est_cost.sum() > 0
    assert result["final_state"].ledger.quantities == {}
    assert (portfolio.cash >= 0).all()
    assert np.isfinite(portfolio.equity).all()


def test_known_liquidity_freeze_blocks_real_strategy_basket():
    with pytest.raises(ValueError, match="ADV"):
        run_stress(freeze_liquidity=True)


def test_file_database_valuation_and_close_race_has_one_atomic_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime
    import sqlite3
    from threading import Barrier, local
    from sqlalchemy import event, select
    from sqlalchemy.exc import OperationalError
    from storage.db import create_db_engine
    from storage.repositories.execution import ExecutionRepository
    from storage.schema import paper_account_closes
    from tests.test_remediation_execution import _risk_repo
    from tests.test_paper_cycle import ET, _execution_snapshot

    source, _ = _risk_repo(diversified=True)
    path = tmp_path / "competing-writers.db"
    raw = source.raw_connection()
    try:
        with sqlite3.connect(path) as destination:
            raw.driver_connection.backup(destination)
    finally:
        raw.close()
        source.dispose()
    engine = create_db_engine(f"sqlite:///{path.as_posix()}")
    first = ExecutionRepository(engine=engine)
    second = ExecutionRepository(engine=engine)
    before = first.get_account("audit")
    barrier, seen = Barrier(2), local()

    def align_initial_reads(conn, cursor, statement, parameters, context, many):
        if (statement.lstrip().upper().startswith("SELECT") and "FROM paper_accounts" in statement
                and not getattr(seen, "initial_read", False)):
            seen.initial_read = True
            barrier.wait(timeout=10)

    event.listen(engine, "after_cursor_execute", align_initial_reads)
    at = datetime(2026, 8, 3, 21, tzinfo=ET)
    prices = {"SPY": 99., "BIL": 100.}
    close_source = _execution_snapshot(engine, "2026-08-03", prices)
    def update(kind):
        try:
            if kind == "mark":
                first.mark_account("audit", prices=prices, valuation_session="2026-08-03",
                    at=at, drawdown_limit=.15, daily_loss_limit=.05)
            else:
                second.record_session_close("audit", session="2026-08-03", prices=prices,
                    recorded_at=at, source_snapshot_id=close_source)
            return kind, "committed"
        except (RuntimeError, OperationalError):
            return kind, "conflict"
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = dict(pool.map(update, ("mark", "close")))
    finally:
        event.remove(engine, "after_cursor_execute", align_initial_reads)
    assert sorted(outcomes.values()) == ["committed", "conflict"]
    after = first.get_account("audit")
    assert after.version == before.version + 1
    with engine.connect() as connection:
        closes = connection.execute(select(paper_account_closes).where(
            paper_account_closes.c.session == "2026-08-03")).mappings().all()
    assert len(closes) == int(outcomes["close"] == "committed")
    if closes:
        assert closes[0]["account_version"] == after.version
    engine.dispose()


def test_memory_and_persisted_accounts_match_identical_fills_actions_and_settlements():
    from datetime import datetime
    from backtest.ledger import PortfolioLedger
    from data.models import CorporateAction
    from execution.models import BrokerEnvironment, ExecutionFill, OrderIntent, OrderState, Quote, Side
    from storage.repositories.execution import ExecutionRepository
    from tests.test_paper_cycle import ET, _engine

    config = Config(strategy_version="SV-001")
    engine = _engine()
    repo = ExecutionRepository(engine=engine)
    repo.initialize_account(account_ref="parity", strategy_version="SV-001", initial_cash=10000,
                            at=datetime(2026, 7, 31, 15, tzinfo=ET))
    ledger = PortfolioLedger.initialize(config, session=pd.Timestamp("2026-07-31"), prices=pd.Series(dtype=float))
    actions = [
        CorporateAction("SPY", pd.Timestamp("2026-08-04"), "dividend", cash_amount=1.,
                        payment_date=pd.Timestamp("2026-08-06"), payment_source="issuer"),
        CorporateAction("BIL", pd.Timestamp("2026-08-04"), "split", split_factor=2.),
    ]
    for session in ("2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06"):
        date = pd.Timestamp(session)
        now = datetime.fromisoformat(session).replace(hour=16, tzinfo=ET)
        marks = pd.Series({"SPY": 100. if session == "2026-08-03" else 99.,
                           "BIL": 100. if session == "2026-08-03" else 50.})
        ledger.settle(date)
        repo.settle_due("parity", session=session, settled_at=now)
        ledger.apply_corporate_actions(actions, date)
        repo.process_corporate_actions("parity", actions=actions, session=session, at=now)
        if session in {"2026-08-03", "2026-08-05"}:
            before = len(ledger.order_log)
            target = {"SPY": .35, "BIL": .65} if session == "2026-08-03" else {"CASH_USD": 1.}
            ledger.rebalance(signal_date=NyseCalendar().previous_session(date), execution_date=date,
                             target_weights=target, prices=marks, median_dollar_volume=pd.Series({"SPY": 1e8, "BIL": 1e8}))
            for number, order in enumerate(ledger.order_log[before:]):
                identifier = f"{session}-{number}"
                intent = OrderIntent(identifier, BrokerEnvironment.PAPER, "SV-001", session,
                    order["ticker"], Side(order["side"]), order["quantity"], order["price"],
                    Quote(order["ticker"], order["reference_price"], order["reference_price"], now),
                    order["adv_fraction"], 0., state=OrderState.SUBMITTED, created_at=now)
                intent_id = repo.save_intent(intent)
                fill = ExecutionFill(identifier, identifier, now, order["quantity"], order["price"],
                    order["trading_cost_dollars"], 0., str(NyseCalendar().next_session(date).date()))
                repo.apply_paper_fill(account_ref="parity", order_intent_id=intent_id, fill=fill)
        # Reopening the repository must recover the same financial facts.
        repo = ExecutionRepository(engine=engine)
        account, *_ = repo.mark_account("parity", prices=marks.to_dict(), valuation_session=session,
            at=now, drawdown_limit=.15, daily_loss_limit=.05)
        assert {key: item.quantity for key, item in account.positions.items()} == pytest.approx(ledger.quantities)
        assert account.settled_cash == pytest.approx(ledger.settled_cash_balance)
        assert account.unsettled_cash == pytest.approx(ledger.unsettled_cash)
        assert account.available_cash == pytest.approx(ledger.cash)
        assert account.dividend_receivable == pytest.approx(ledger.dividend_receivable)
        assert account.nav == pytest.approx(ledger.mark(marks))
        assert account.total_commission == pytest.approx(sum(item["trading_cost_dollars"] for item in ledger.order_log))
    assert ledger.dividend_receivable == 0.
    assert ledger.quantities == {}
