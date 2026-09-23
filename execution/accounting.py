"""Economic accounting shared by historical simulation and local paper accounts.

Prices are always raw and quantities are actual shares. Distribution receivables
belong in NAV, but only confirmed payments become spendable cash.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Iterable, Mapping

import pandas as pd

from data.models import CorporateAction
from execution.validation import finite_number

ACCOUNTING_MODEL_VERSION = "raw_shares_receivables_v2"


@dataclass(frozen=True)
class DividendReceivable:
    action_key: str
    ticker: str
    ex_date: str
    amount: float
    payment_date: str | None
    revision_hash: str
    payment_source: str | None = None


@dataclass(frozen=True)
class CorporateActionState:
    quantities: Mapping[str, float] = field(default_factory=dict)
    average_costs: Mapping[str, float] = field(default_factory=dict)
    settled_cash: float = 0.
    receivables: tuple[DividendReceivable, ...] = ()
    applied_actions: Mapping[str, str] = field(default_factory=dict)
    started_session: str | None = None
    last_session: str | None = None

    @property
    def dividend_receivable(self) -> float:
        return sum(item.amount for item in self.receivables)

    @property
    def unconfirmed_payments(self) -> bool:
        return any(item.payment_date is None or not item.payment_source for item in self.receivables)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "CorporateActionState":
        values = dict(payload)
        values["receivables"] = tuple(DividendReceivable(**dict(item)) for item in values.get("receivables", ()))
        state = cls(**values)
        _validate_state(state)
        return state


def _validate_state(state: CorporateActionState) -> None:
    finite_number(state.settled_cash, "settled cash", nonnegative=True)
    for ticker, quantity in state.quantities.items():
        finite_number(quantity, f"{ticker} quantity", nonnegative=True)
    for ticker, basis in state.average_costs.items():
        finite_number(basis, f"{ticker} average cost", nonnegative=True)
    for item in state.receivables:
        finite_number(item.amount, "distribution receivable", nonnegative=True)


def apply_corporate_actions(
    state: CorporateActionState,
    actions: Iterable[CorporateAction],
    session: object,
) -> CorporateActionState:
    """Apply pre-open entitlements once; reject retrospective event revisions.

    The first call establishes the account's event-history boundary. Events before
    that boundary are not entitlements of this newly initialized account. Subsequent
    calls may skip sessions without actions, but cannot infer past share ownership
    for a newly discovered action. Such late actions require an explicit replay.
    """
    _validate_state(state)
    date = pd.Timestamp(session).normalize()
    date_text = str(date.date())
    if state.last_session is not None and date_text < state.last_session:
        raise ValueError("Corporate action sessions must be processed chronologically.")
    started = state.started_session or date_text
    if date_text < started:
        raise ValueError("Corporate action session predates the account inception boundary.")
    quantities = dict(state.quantities)
    costs = dict(state.average_costs)
    applied = dict(state.applied_actions)
    receivables = list(state.receivables)
    settled_cash = state.settled_cash
    canonical: dict[str, CorporateAction] = {}
    for raw in actions:
        action = raw.normalized()
        if pd.isna(action.ex_date) or (action.payment_date is not None and pd.isna(action.payment_date)):
            raise ValueError("Corporate action dates must be valid.")
        if action.action_type not in {"split", "dividend"}:
            raise ValueError(f"Unsupported corporate action: {action.action_type}.")
        finite_number(action.cash_amount, "distribution amount", nonnegative=True)
        finite_number(action.split_factor, "split factor", positive=True)
        if action.payment_date is not None and action.payment_date < action.ex_date:
            raise ValueError("Dividend payment date cannot precede its ex-date.")
        key = action.action_key
        previous = canonical.get(key)
        if previous is not None and previous.revision_hash != action.revision_hash:
            raise ValueError(f"Conflicting corporate action revision: {key}.")
        canonical[key] = action
    todays_types: dict[str, set[str]] = {}
    for action in canonical.values():
        if action.ex_date == date and action.status == "active":
            todays_types.setdefault(action.ticker, set()).add(action.action_type)
    if any(len(kinds) > 1 for kinds in todays_types.values()):
        raise ValueError("Same-session split and dividend require an explicit per-share basis review.")
    for key, action in sorted(canonical.items()):
        if key in applied:
            if applied[key] != action.revision_hash:
                raise ValueError(f"Applied corporate action revision changed: {key}; replay is required.")
            continue
        if action.ex_date > date or str(action.ex_date.date()) < started:
            continue
        if action.ex_date < date:
            raise ValueError(f"Unprocessed historical corporate action: {key}; replay is required.")
        applied[key] = action.revision_hash
        if action.status != "active":
            continue
        quantity = quantities.get(action.ticker, 0.)
        if action.action_type == "split":
            quantities[action.ticker] = quantity * action.split_factor
            if action.ticker in costs:
                costs[action.ticker] /= action.split_factor
        elif quantity > 0. and action.cash_amount > 0.:
            receivables.append(DividendReceivable(
                action_key=key, ticker=action.ticker, ex_date=date_text,
                amount=quantity * action.cash_amount,
                payment_date=None if action.payment_date is None else str(action.payment_date.date()),
                revision_hash=action.revision_hash, payment_source=action.payment_source,
            ))
    unpaid: list[DividendReceivable] = []
    for item in receivables:
        if item.payment_date is not None and item.payment_source and item.payment_date <= date_text:
            settled_cash += item.amount
        else:
            unpaid.append(item)
    result = CorporateActionState(
        quantities={ticker: quantity for ticker, quantity in quantities.items() if quantity > 1e-12},
        average_costs=costs, settled_cash=settled_cash, receivables=tuple(unpaid),
        applied_actions=applied, started_session=started, last_session=date_text,
    )
    _validate_state(result)
    return result


def portfolio_nav(*, quantities: Mapping[str, float], prices: Mapping[str, float], settled_cash: float,
                  unsettled_cash: float = 0., dividend_receivable: float = 0.) -> float:
    value = finite_number(settled_cash, "settled cash", nonnegative=True)
    value += finite_number(unsettled_cash, "unsettled cash")
    value += finite_number(dividend_receivable, "distribution receivable", nonnegative=True)
    for ticker, quantity in quantities.items():
        quantity = finite_number(quantity, f"{ticker} quantity", nonnegative=True)
        if quantity <= 1e-12:
            continue
        if ticker not in prices:
            raise ValueError(f"Missing valid mark price for active holding {ticker}.")
        price = finite_number(prices[ticker], f"mark price for active holding {ticker}", positive=True)
        value += quantity * price
    return finite_number(value, "Portfolio NAV", positive=True)


@dataclass(frozen=True)
class FillAccounting:
    quantities: dict[str, float]
    average_costs: dict[str, float]
    cash_change: float
    gross_realized_pnl: float | None
    realized_pnl: float | None


def apply_fill(*, quantities: Mapping[str, float], average_costs: Mapping[str, float],
               ticker: str, quantity_change: float, price: float, commission: float) -> FillAccounting:
    change = finite_number(quantity_change, "quantity change")
    if change == 0.:
        raise ValueError("A fill must change the position quantity.")
    price = finite_number(price, "fill price", positive=True)
    commission = finite_number(commission, "commission", nonnegative=True)
    current = finite_number(quantities.get(ticker, 0.), "current quantity", nonnegative=True)
    target = current + change
    if target < -1e-8:
        raise ValueError("Fill would create a short position.")
    result_quantities, result_costs = dict(quantities), dict(average_costs)
    gross = realized = None
    if change > 0.:
        if current > 1e-12 and ticker not in average_costs:
            raise ValueError(f"Missing cost basis for existing holding {ticker}.")
        old_cost = finite_number(average_costs.get(ticker, 0.), "average cost", nonnegative=True)
        result_costs[ticker] = finite_number((current * old_cost + change * price + commission) / target,
                                      "average cost after fill", nonnegative=True)
    else:
        # Unknown historical basis prevents trustworthy P/L, but must not
        # prevent a sale of a reconciled holding during risk reduction.
        if ticker in average_costs:
            old_cost = finite_number(average_costs[ticker], "average cost", nonnegative=True)
            gross = finite_number((-change) * (price - old_cost), "gross realized P/L")
            realized = finite_number(gross - commission, "realized P/L")
    if target <= 1e-12:
        result_quantities.pop(ticker, None)
        result_costs.pop(ticker, None)
    else:
        result_quantities[ticker] = target
    cash_change = finite_number(-change * price - commission, "fill cash change")
    return FillAccounting(result_quantities, result_costs, cash_change, gross, realized)
