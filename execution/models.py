from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping


class BrokerEnvironment(StrEnum):
    RESEARCH = "RESEARCH"
    PAPER = "PAPER"
    LIVE = "LIVE"


class OrderState(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    SUBMITTED = "SUBMITTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    MISSED = "MISSED"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Quote:
    ticker: str
    bid: float
    ask: float
    captured_at: datetime

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * 10_000.0


@dataclass(frozen=True)
class BrokerPosition:
    ticker: str
    quantity: float
    market_value: float

    @property
    def mark_price(self) -> float:
        if abs(self.quantity) < 1e-12:
            return 0.0
        return self.market_value / self.quantity


@dataclass(frozen=True)
class PendingSettlement:
    movement_key: str
    amount: float
    trade_date: str
    settlement_date: str
    status: str = "PENDING"


@dataclass(frozen=True)
class AccountSnapshot:
    account_ref: str
    nav: float
    settled_cash: float
    available_cash: float
    buying_power: float
    positions: Mapping[str, BrokerPosition]
    captured_at: datetime
    unsettled_cash: float = 0.0
    pending_settlements: tuple[PendingSettlement, ...] = ()
    high_water: float | None = None
    risk_state: str = "NORMAL"
    total_commission: float = 0.0
    last_valuation_session: str | None = None
    version: int = 1


@dataclass
class OrderIntent:
    client_order_id: str
    environment: BrokerEnvironment
    strategy_version: str
    signal_session: str
    ticker: str
    side: Side
    quantity: float
    limit_price: float
    arrival_quote: Quote
    adv_fraction: float
    estimated_impact_bps: float
    signal_decision_id: int | None = None
    paper_cycle_id: int | None = None
    order_type: str = "LMT"
    execution_session: str | None = None
    filled_quantity: float = 0.0
    account_before: Mapping[str, object] | None = None
    state: OrderState = OrderState.DRAFT
    created_at: datetime | None = None
    approved_at: datetime | None = None
    approved_by: str | None = None
    broker_order_id: str | None = None
    submitted_at: datetime | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def notional(self) -> float:
        return self.quantity * self.limit_price

    @property
    def remaining_quantity(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)


@dataclass(frozen=True)
class ExecutionFill:
    client_order_id: str
    broker_execution_id: str
    filled_at: datetime
    quantity: float
    price: float
    commission: float
    implementation_shortfall_bps: float
    settlement_date: str | None = None


@dataclass(frozen=True)
class ReconciliationResult:
    matched: bool
    difference_value: float
    threshold: float
    negative_cash: bool
    short_positions: tuple[str, ...]
    unknown_positions: tuple[str, ...]
    open_orders: tuple[str, ...]
    reasons: tuple[str, ...]
    reconciled_at: datetime | None = None
    quantity_differences: Mapping[str, float] = field(default_factory=dict)
    settled_cash_difference: float = 0.0
    unsettled_cash_difference: float = 0.0
    available_cash_difference: float = 0.0
    nav_difference: float = 0.0
    commission_difference: float = 0.0


@dataclass(frozen=True)
class IncidentNotification:
    incident_id: int
    code: str
    severity: str
    strategy_version: str
    account_ref: str | None
    details: Mapping[str, object]
    attempts: int
