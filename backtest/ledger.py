from __future__ import annotations

from dataclasses import dataclass, field, replace
from copy import deepcopy
from typing import Iterable, Mapping

import numpy as np
import pandas as pd

from config.settings import Config
from data.calendar import NyseCalendar
from data.models import CorporateAction
from execution.accounting import CorporateActionState, apply_corporate_actions, apply_fill, portfolio_nav
from execution.budget import plan_rebalance


@dataclass
class CashSettlement:
    settlement_date: pd.Timestamp
    amount: float


@dataclass
class PortfolioLedger:
    config: Config
    quantities: dict[str, float] = field(default_factory=dict)
    average_costs: dict[str, float] = field(default_factory=dict)
    settled_cash_balance: float = 0.
    settlement_queue: list[CashSettlement] = field(default_factory=list)
    order_log: list[dict[str, object]] = field(default_factory=list)
    corporate_action_state: CorporateActionState = field(default_factory=CorporateActionState)
    calendar: NyseCalendar = field(default_factory=NyseCalendar, repr=False)

    def __deepcopy__(self, memo: dict) -> "PortfolioLedger":
        # The exchange schedule contains pandas-market-calendars ProtectedDict
        # objects. It is stateless here and may safely be shared across windows.
        copied = type(self)(
            config=deepcopy(self.config, memo), quantities=deepcopy(self.quantities, memo),
            average_costs=deepcopy(self.average_costs, memo), settled_cash_balance=self.settled_cash_balance,
            settlement_queue=deepcopy(self.settlement_queue, memo), order_log=deepcopy(self.order_log, memo),
            corporate_action_state=deepcopy(self.corporate_action_state, memo), calendar=self.calendar,
        )
        memo[id(self)] = copied
        return copied

    @classmethod
    def initialize(cls, config: Config, *, session: pd.Timestamp, prices: pd.Series) -> "PortfolioLedger":
        ledger = cls(config=config, settled_cash_balance=float(config.initial_capital))
        # Initial capital is cash. Existing positions require an explicit
        # carried account state; creating BIL here would skip its trade costs.
        ledger.corporate_action_state = CorporateActionState(
            started_session=str(ledger.calendar.next_session(session).date()),
            last_session=str(pd.Timestamp(session).date()),
        )
        return ledger

    def settle(self, session: pd.Timestamp) -> None:
        pending = []
        for item in self.settlement_queue:
            if item.settlement_date <= pd.Timestamp(session).normalize():
                self.settled_cash_balance += item.amount
            else:
                pending.append(item)
        self.settlement_queue = pending
        if not np.isfinite(self.settled_cash_balance) or self.settled_cash_balance < -1e-6:
            raise ValueError("Settled cash became invalid; cash-account invariant failed.")
        if self.settled_cash_balance < 0.:
            self.settled_cash_balance = 0.  # Floating-point dust, never financing.

    def apply_corporate_actions(self, actions: Iterable[CorporateAction], session: object) -> None:
        current = replace(self.corporate_action_state, quantities=dict(self.quantities),
                          average_costs=dict(self.average_costs), settled_cash=self.settled_cash_balance)
        result = apply_corporate_actions(current, actions, session)
        self.quantities = dict(result.quantities)
        self.average_costs = dict(result.average_costs)
        self.settled_cash_balance = result.settled_cash
        self.corporate_action_state = result

    @property
    def unsettled_cash(self) -> float:
        return float(sum(item.amount for item in self.settlement_queue))

    @property
    def cash(self) -> float:
        """Spendable claim under the declared cash-account settlement policy."""
        return float(self.settled_cash_balance + self.unsettled_cash)

    @property
    def dividend_receivable(self) -> float:
        return self.corporate_action_state.dividend_receivable

    def mark(self, prices: pd.Series) -> float:
        return portfolio_nav(quantities=self.quantities, prices=prices,
                             settled_cash=self.settled_cash_balance, unsettled_cash=self.unsettled_cash,
                             dividend_receivable=self.dividend_receivable)

    def weights(self, prices: pd.Series) -> dict[str, float]:
        nav = self.mark(prices)
        result = {ticker: quantity * float(prices[ticker]) / nav
                  for ticker, quantity in self.quantities.items() if quantity > 1e-12}
        # Both cash and receivables are non-market exposure. Sizing separately
        # uses cash only, so this reporting weight never creates buying power.
        cash_weight = (self.cash + self.dividend_receivable) / nav
        if cash_weight > 1e-12:
            result[self.config.synthetic_cash_asset] = cash_weight
        return result

    def rebalance(self, *, signal_date: pd.Timestamp, execution_date: pd.Timestamp,
                  target_weights: Mapping[str, float], prices: pd.Series,
                  median_dollar_volume: pd.Series | None = None, risk_off: bool = False) -> dict[str, float]:
        open_nav = self.mark(prices)
        plan = plan_rebalance(self.config, nav=open_nav, cash_available=self.cash,
                              quantities=self.quantities, target_weights=target_weights, prices=prices,
                              median_dollar_volume=median_dollar_volume, risk_off=risk_off)
        quantities, costs = dict(self.quantities), dict(self.average_costs)
        orders = []
        net_settlement = 0.
        for planned in plan.orders:
            ticker = planned.ticker
            signed_quantity = planned.quantity if planned.side == "BUY" else -planned.quantity
            current_quantity = quantities.get(ticker, 0.)
            entry_cost = costs.get(ticker)
            accounting = apply_fill(quantities=quantities, average_costs=costs, ticker=ticker,
                                    quantity_change=signed_quantity, price=planned.price, commission=planned.commission)
            quantities, costs = accounting.quantities, accounting.average_costs
            net_settlement += accounting.cash_change
            cost_dollars = planned.commission + planned.slippage + planned.impact
            orders.append({
                "signal_date": pd.Timestamp(signal_date), "date": pd.Timestamp(execution_date),
                "ticker": ticker, "side": planned.side, "quantity": planned.quantity,
                "quantity_change": signed_quantity, "current_quantity": current_quantity,
                "target_quantity": quantities.get(ticker, 0.), "notional": planned.notional,
                "price": planned.price, "reference_price": planned.reference_price,
                "average_entry_cost": costs.get(ticker) if planned.side == "BUY" else entry_cost,
                "weight_change": signed_quantity * planned.reference_price / open_nav,
                "trading_cost_dollars": planned.commission, "slippage_dollars": planned.slippage,
                "impact_cost_dollars": planned.impact, "adv_fraction": planned.adv_fraction,
                "gross_realized_pnl": accounting.gross_realized_pnl, "realized_pnl": accounting.realized_pnl,
                "est_trading_cost": planned.commission / open_nav,
                "est_slippage": planned.slippage / open_nav, "est_impact": planned.impact / open_nav,
                "est_cost": cost_dollars / open_nav,
            })
        if self.cash + net_settlement < -1e-6:
            raise ValueError("Cash-account execution would create a negative cash balance.")
        # All calculations and validations precede mutation, including cost
        # basis checks. A rejected basket cannot leave a partially changed ledger.
        self.quantities, self.average_costs = quantities, costs
        if abs(net_settlement) > 1e-12:
            self.settlement_queue.append(CashSettlement(self.calendar.next_session(execution_date), net_settlement))
        self.order_log.extend(orders)
        total_cost = plan.total_commission + plan.total_slippage + plan.total_impact
        return {
            "turnover": sum(order["notional"] for order in orders) / open_nav,
            "est_trading_cost": plan.total_commission / open_nav,
            "est_slippage": plan.total_slippage / open_nav, "est_impact": plan.total_impact / open_nav,
            "est_cost": total_cost / open_nav, "cost_dollars": total_cost,
            "net_cash_settlement": net_settlement,
            "maximum_adv_fraction": max((order["adv_fraction"] for order in orders), default=float("nan")),
        }
