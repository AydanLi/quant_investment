from dataclasses import replace

import pandas as pd
import pytest

from data.models import CorporateAction
from execution.accounting import CorporateActionState, apply_corporate_actions, apply_fill, portfolio_nav
from backtest.engine import Backtester, BacktestState
from backtest.ledger import PortfolioLedger
from config.settings import Config
from data.adjustments import locally_adjust_ohlcv
from data.calendar import NyseCalendar
from data.features import FeatureEngineer
from tests.test_remediation_data import HoldCash, Neutral, PassRisk


def test_dividend_receivable_survives_sale_and_payment_is_nav_neutral():
    action = CorporateAction("SPY", pd.Timestamp("2024-01-03"), "dividend", cash_amount=1., payment_date=pd.Timestamp("2024-01-05"), payment_source="issuer")
    state = CorporateActionState(quantities={"SPY": 100.}, average_costs={"SPY": 100.}, settled_cash=0.)
    accrued = apply_corporate_actions(state, [action], pd.Timestamp("2024-01-03"))
    assert accrued.dividend_receivable == 100.
    assert accrued.settled_cash == 0.
    assert portfolio_nav(quantities=accrued.quantities, prices={"SPY": 99.}, settled_cash=0., dividend_receivable=100.) == 10000.
    sold = replace(accrued, quantities={}, average_costs={}, settled_cash=9900.)
    paid = apply_corporate_actions(sold, [action], pd.Timestamp("2024-01-05"))
    assert paid.settled_cash == 10000.
    assert paid.dividend_receivable == 0.
    assert apply_corporate_actions(paid, [action], pd.Timestamp("2024-01-05")) == paid


def test_unknown_payment_stays_unavailable_and_revision_fails_closed():
    action = CorporateAction("SPY", pd.Timestamp("2024-01-03"), "dividend", cash_amount=1.)
    state = CorporateActionState(quantities={"SPY": 100.}, average_costs={"SPY": 100.}, settled_cash=0.)
    accrued = apply_corporate_actions(state, [action], pd.Timestamp("2024-01-03"))
    later = apply_corporate_actions(accrued, [action], pd.Timestamp("2024-02-01"))
    assert later.dividend_receivable == 100.
    assert later.settled_cash == 0.
    with pytest.raises(ValueError, match="revision"):
        apply_corporate_actions(later, [replace(action, cash_amount=2.)], pd.Timestamp("2024-02-01"))


def test_split_is_idempotent_and_preserves_total_cost():
    state = CorporateActionState(quantities={"SPY": 100.}, average_costs={"SPY": 100.}, settled_cash=0.)
    action = CorporateAction("SPY", pd.Timestamp("2024-01-03"), "split", split_factor=2.)
    result = apply_corporate_actions(state, [action], pd.Timestamp("2024-01-03"))
    assert result.quantities["SPY"] == 200.
    assert result.average_costs["SPY"] == 50.
    assert apply_corporate_actions(result, [action], pd.Timestamp("2024-01-03")) == result


def test_future_close_cannot_change_ex_dividend_open_liquidation():
    index = NyseCalendar().sessions("2023-01-01", "2024-03-01")[:254]
    outcomes = []
    for future_close in (90., 110.):
        frame = pd.DataFrame({"Open": 100., "High": 111., "Low": 89., "Close": 100., "Volume": 1e6}, index=index)
        frame.loc[index[-1], ["Open", "Close"]] = [99., future_close]
        action = CorporateAction("BIL", index[-1], "dividend", cash_amount=1.)
        config = Config(universe=["BIL"], rebalance_frequency="D", trading_cost_bps=0., slippage_bps=0.,
                        operational_cash_buffer_min=0., operational_cash_buffer_pct=0.)
        engineer = FeatureEngineer({"BIL": locally_adjust_ohlcv(frame, [action])}, config)
        signal = engineer.make_price_frame()
        ledger = PortfolioLedger.initialize(config, session=index[251], prices=pd.Series({"BIL": 100.}))
        ledger.quantities = {"BIL": 100.}
        ledger.average_costs = {"BIL": 100.}
        ledger.settled_cash_balance = 0.
        initial = BacktestState(ledger, 10000., index[251], {"high_water": 10000., "drawdown_halted": False})
        result = Backtester(config, signal, engineer.make_returns_frame(signal), {}, Neutral(), HoldCash(), PassRisk(),
                            execution_prices=engineer.make_open_frame(), raw_close_prices=engineer.make_raw_close_frame(),
                            corporate_actions=engineer.corporate_actions(), initial_state=initial).run()
        outcomes.append(result)
    for result in outcomes:
        last = result["portfolio"].iloc[-1]
        assert last.equity == pytest.approx(10000.)
        assert last.dividend_receivable == pytest.approx(100.)
        assert last.cash == pytest.approx(9900.)
        assert result["orders"].iloc[0]["quantity"] == pytest.approx(100.)
    pd.testing.assert_frame_equal(outcomes[0]["orders"], outcomes[1]["orders"])


def test_continuous_state_matches_uninterrupted_account_with_dividend_and_pending_order():
    index = NyseCalendar().sessions("2023-01-01", "2024-07-01")[:285]
    frame = pd.DataFrame({"SPY": 100., "BIL": 100.}, index=index)
    frame.loc[index[266]:, "SPY"] = 99.
    action = CorporateAction("SPY", index[266], "dividend", cash_amount=1.,
                             payment_date=index[271], payment_source="issuer")
    config = Config(universe=["SPY", "BIL"], rebalance_frequency="D", max_asset_weight=1., min_asset_weight=0.)
    class HoldSpy:
        def target_weights(self, *args):
            return {"SPY": 1.}
    def run(start, end, state=None):
        return Backtester(config, frame, frame.pct_change(fill_method=None), {}, Neutral(), HoldSpy(), PassRisk(),
                          execution_prices=frame, raw_close_prices=frame, corporate_actions=[action],
                          trade_start=start, trade_end=end, initial_state=state).run()
    full = run(index[252], index[-1])
    first = run(index[252], index[268])
    second = run(index[269], index[-1], first["final_state"])
    assert first["final_state"].ledger.dividend_receivable > 0.
    assert first["final_state"].pending_target is not None
    pd.testing.assert_frame_equal(full["portfolio"], pd.concat([first["portfolio"], second["portfolio"]]))
    pd.testing.assert_frame_equal(full["orders"].reset_index(drop=True), pd.concat([first["orders"], second["orders"]]).reset_index(drop=True))
    assert second["final_state"].ledger.dividend_receivable == 0.


def test_new_ex_date_buyer_does_not_get_existing_distribution():
    action = CorporateAction("SPY", pd.Timestamp("2024-01-03"), "dividend", cash_amount=1.)
    empty = apply_corporate_actions(CorporateActionState(settled_cash=10000.), [action], "2024-01-03")
    bought = replace(empty, quantities={"SPY": 100.}, average_costs={"SPY": 99.}, settled_cash=100.)
    assert apply_corporate_actions(bought, [action], "2024-01-03").dividend_receivable == 0.


def test_unknown_cost_does_not_prevent_risk_reducing_sale():
    result = apply_fill(quantities={"SPY": 10.}, average_costs={}, ticker="SPY",
                        quantity_change=-5., price=100., commission=1.)
    assert result.quantities == {"SPY": 5.}
    assert result.cash_change == 499.
    assert result.realized_pnl is None
    with pytest.raises(ValueError, match="Missing cost basis"):
        apply_fill(quantities={"SPY": 10.}, average_costs={}, ticker="SPY",
                   quantity_change=5., price=100., commission=1.)


def test_account_initialization_has_no_unrecorded_etf_purchase():
    ledger = PortfolioLedger.initialize(Config(), session=pd.Timestamp("2024-01-02"), prices=pd.Series({"BIL": 100.}))
    assert ledger.quantities == {}
    assert ledger.settled_cash_balance == 10000.
    assert ledger.order_log == []


def test_date_without_payment_evidence_does_not_create_spendable_cash():
    action = CorporateAction("SPY", pd.Timestamp("2024-01-03"), "dividend", cash_amount=1., payment_date=pd.Timestamp("2024-01-05"))
    initial = CorporateActionState(quantities={"SPY": 100.}, average_costs={"SPY": 100.})
    accrued = apply_corporate_actions(initial, [action], "2024-01-03")
    later = apply_corporate_actions(accrued, [action], "2024-01-08")
    assert later.unconfirmed_payments
    assert later.dividend_receivable == 100.
    assert later.settled_cash == 0.
    restored = CorporateActionState.from_dict(later.to_dict())
    assert restored == later
    with pytest.raises(ValueError, match="revision"):
        apply_corporate_actions(later, [replace(action, payment_source="issuer")], "2024-01-08")


def test_historical_events_cannot_be_replayed_into_a_later_account():
    state = CorporateActionState(settled_cash=10000., started_session="2024-02-01")
    with pytest.raises(ValueError, match="inception boundary"):
        apply_corporate_actions(state, [], "2024-01-03")


@pytest.mark.parametrize("target,increases_risk", [({"SPY": 1.}, True), ({"CASH_USD": 1.}, False)])
def test_pending_order_rechecks_open_gap_risk_before_trading(target, increases_risk):
    index = NyseCalendar().sessions("2023-01-01", "2024-07-01")[:253]
    prices = pd.DataFrame({"SPY": 100., "BIL": 100.}, index=index)
    opens = prices.copy()
    opens.loc[index[-1], "SPY"] = 40.
    config = Config(universe=["SPY", "BIL"], max_asset_weight=1., min_asset_weight=0.)
    ledger = PortfolioLedger.initialize(config, session=index[-2], prices=prices.iloc[-2])
    ledger.quantities = {"SPY": 50.}
    ledger.average_costs = {"SPY": 100.}
    ledger.settled_cash_balance = 5000.
    state = BacktestState(
        ledger=ledger, previous_nav=10000., last_session=index[-2],
        risk_monitor_state={"high_water": 10000., "drawdown_halted": False},
        pending_target=target, pending_signal_date=index[-2],
        pending_regime="neutral", pending_execution_config=config,
    )
    result = Backtester(
        config, prices, prices.pct_change(fill_method=None), {}, Neutral(), HoldCash(), PassRisk(),
        execution_prices=opens, raw_close_prices=prices, initial_state=state,
    ).run()
    if increases_risk:
        assert result["orders"].empty
        assert "DRAWDOWN_HALTED" in result["portfolio"].iloc[0]["execution_skip_reason"]
        assert result["final_state"].ledger.quantities == {"SPY": 50.}
    else:
        assert result["orders"]["side"].tolist() == ["SELL"]
        assert result["final_state"].ledger.quantities == {}
    assert result["portfolio"].attrs["execution_model"] == "BACKTEST_OPEN_REBALANCE"


def test_carried_account_rejects_newly_discovered_historical_action():
    index = NyseCalendar().sessions("2023-01-01", "2024-07-01")[:265]
    prices = pd.DataFrame({"SPY": 100., "BIL": 100.}, index=index)
    config = Config(universe=["SPY", "BIL"])
    first = Backtester(
        config, prices, prices.pct_change(fill_method=None), {}, Neutral(), HoldCash(), PassRisk(),
        execution_prices=prices, raw_close_prices=prices, trade_end=index[260],
    ).run()
    late_action = CorporateAction("SPY", index[256], "dividend", cash_amount=1.)
    with pytest.raises(ValueError, match="historical corporate action"):
        Backtester(
            config, prices, prices.pct_change(fill_method=None), {}, Neutral(), HoldCash(), PassRisk(),
            execution_prices=prices, raw_close_prices=prices,
            corporate_actions=[late_action], initial_state=first["final_state"],
        ).run()
