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


@dataclass(frozen=True)
class AccountSnapshot:
    account_ref: str
    nav: float
    settled_cash: float
    available_cash: float
    buying_power: float
    positions: Mapping[str, BrokerPosition]
    captured_at: datetime


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


@dataclass(frozen=True)
class ExecutionFill:
    client_order_id: str
    broker_execution_id: str
    filled_at: datetime
    quantity: float
    price: float
    commission: float
    implementation_shortfall_bps: float


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
