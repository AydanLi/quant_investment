from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from math import sqrt
from typing import Mapping

import pandas as pd

from config.settings import Config
from execution.adapters import BrokerAdapter
from execution.models import (
    AccountSnapshot,
    BrokerEnvironment,
    OrderIntent,
    OrderState,
    Quote,
    ReconciliationResult,
    Side,
)
from services.models import SignalDecision, SignalStatus
from execution.pretrade import PreTradeVerification


class OrderManagementSystem:
    def __init__(self, config: Config, broker: BrokerAdapter) -> None:
        self.config = config
        self.broker = broker
        self._intents: dict[str, OrderIntent] = {}
        self.last_draft_warnings: tuple[str, ...] = ()

    @staticmethod
    def _idempotency_key(
        decision: SignalDecision,
        environment: BrokerEnvironment,
        ticker: str,
        side: Side,
        quantity: float,
    ) -> str:
        material = (
            f"{environment.value}|{decision.strategy_version}|"
            f"{decision.dataset_snapshot_id}|"
            f"{decision.signal_session}|{ticker}|{side.value}|{quantity:.8f}"
        )
        return sha256(material.encode("utf-8")).hexdigest()

    def create_drafts(
        self,
        decision: SignalDecision,
        *,
        quotes: Mapping[str, Quote],
        median_daily_dollar_volume: Mapping[str, float],
        account: AccountSnapshot,
        verification: PreTradeVerification,
    ) -> tuple[OrderIntent, ...]:
        if not verification.passed:
            raise ValueError(
                f"T+1 pre-open verification failed: {', '.join(verification.reasons)}"
            )
        if decision.status != SignalStatus.ACTIONABLE:
            raise ValueError("Only ACTIONABLE signal decisions may create order drafts.")
        verified_at = pd.Timestamp(verification.verified_at)
        if verified_at.tzinfo is None:
            verified_at = verified_at.tz_localize("UTC")
        local_verified_at = verified_at.tz_convert("America/New_York")
        hour, minute = (int(piece) for piece in self.config.execution_time_et.split(":"))
        if (local_verified_at.hour, local_verified_at.minute) < (hour, minute):
            raise ValueError(
                f"Initial limit orders cannot be drafted before {self.config.execution_time_et} ET."
            )
        if account.settled_cash < -1e-9 or account.buying_power > account.nav + 1e-6:
            raise ValueError("Cash-account invariant failed: negative cash or leverage detected.")

        for ticker, weight in decision.target_weights.items():
            if ticker in {self.config.cash_asset, self.config.synthetic_cash_asset}:
                continue
            if weight > self.config.max_asset_weight + 1e-9:
                raise ValueError(f"{ticker} target exceeds the risky-asset maximum.")
            if 1e-9 < weight < self.config.min_asset_weight - 1e-9:
                raise ValueError(f"{ticker} target is below the risky-asset minimum.")

        drafts: list[OrderIntent] = []
        warnings: list[str] = []
        drift_review = decision.risk_state.upper() == "DRIFT_REVIEW"
        for ticker, dollar_delta in decision.dollar_deltas.items():
            if ticker == self.config.synthetic_cash_asset or abs(dollar_delta) < 1.0:
                continue
            if drift_review and dollar_delta > 0.0:
                warnings.append(f"BUY_BLOCKED_DRIFT_REVIEW:{ticker}")
                continue
            if ticker not in quotes:
                raise ValueError(f"Missing arrival quote for {ticker}.")
            quote = quotes[ticker]
            if quote.bid <= 0.0 or quote.ask < quote.bid or quote.spread_bps > self.config.spread_block_bps:
                raise ValueError(f"{ticker} quote failed the 20 bp spread gate.")
            side = Side.BUY if dollar_delta > 0.0 else Side.SELL
            mid = quote.mid
            quantity = abs(float(dollar_delta)) / mid
            if not self.broker.supports_fractional(ticker, "LMT") and abs(quantity - round(quantity)) > 1e-8:
                raise ValueError(f"Fractional LMT order is not supported for {ticker}.")
            adv = float(median_daily_dollar_volume.get(ticker, 0.0))
            if adv <= 0.0:
                raise ValueError(f"Missing positive ADV for {ticker}.")
            adv_fraction = abs(float(dollar_delta)) / adv
            if adv_fraction > self.config.maximum_order_adv:
                raise ValueError(f"{ticker} order exceeds 1% ADV and is blocked.")
            impact_bps = 0.0
            if adv_fraction >= self.config.impact_model_adv_threshold:
                impact_bps = self.config.impact_coefficient_bps * sqrt(
                    adv_fraction / self.config.impact_model_adv_threshold
                )
            limit_offset = 20.0 / 10_000.0
            limit_price = mid * (1.0 + limit_offset if side == Side.BUY else 1.0 - limit_offset)
            client_id = self._idempotency_key(
                decision, self.broker.environment, ticker, side, quantity
            )
            existing = self._intents.get(client_id)
            if existing is not None:
                drafts.append(existing)
                continue
            intent = OrderIntent(
                client_order_id=client_id,
                environment=self.broker.environment,
                strategy_version=decision.strategy_version,
                signal_session=decision.signal_session,
                ticker=ticker,
                side=side,
                quantity=quantity,
                limit_price=limit_price,
                arrival_quote=quote,
                adv_fraction=adv_fraction,
                estimated_impact_bps=impact_bps,
                created_at=datetime.now(timezone.utc),
            )
            self._intents[client_id] = intent
            drafts.append(intent)

        self.last_draft_warnings = tuple(warnings)

        buys = sum(intent.notional for intent in drafts if intent.side == Side.BUY)
        sells = sum(intent.notional for intent in drafts if intent.side == Side.SELL)
        # Same-cycle sales and buys both settle T+1, but the broker-reported
        # available-cash check is repeated after sells before submissions.
        if buys > account.available_cash + sells + 1e-6:
            raise ValueError("Draft buys exceed cash available after planned sells.")
        return tuple(sorted(drafts, key=lambda item: item.side == Side.BUY))

    def approve(self, client_order_id: str, *, approved_by: str) -> OrderIntent:
        intent = self._intents[client_order_id]
        if intent.state != OrderState.DRAFT:
            raise ValueError("Only DRAFT orders can receive first approval.")
        if not approved_by.strip():
            raise ValueError("An identifiable human approver is required.")
        intent.state = OrderState.APPROVED
        intent.approved_at = datetime.now(timezone.utc)
        intent.approved_by = approved_by.strip()
        return intent

    def submit(self, client_order_id: str) -> OrderIntent:
        intent = self._intents[client_order_id]
        if intent.state != OrderState.APPROVED or not intent.approved_by:
            raise ValueError("Every order requires explicit human approval before submission.")
        if intent.environment == BrokerEnvironment.LIVE and self.config.strategy_version == "UNFROZEN":
            raise ValueError("An unfrozen strategy cannot submit a live order.")
        account = self.broker.account_snapshot()
        if intent.side == Side.BUY and intent.notional > account.available_cash + 1e-6:
            raise ValueError("Broker-reported available cash is insufficient for this buy.")
        preview = self.broker.preview(intent)
        if not bool(preview.get("accepted", False)):
            intent.state = OrderState.REJECTED
            raise ValueError("Broker preview rejected the order.")
        intent.broker_order_id = self.broker.submit(intent)
        intent.submitted_at = datetime.now(timezone.utc)
        intent.state = OrderState.SUBMITTED
        return intent

    def cancel_stale(self, *, now: datetime | None = None) -> tuple[str, ...]:
        current = now or datetime.now(timezone.utc)
        canceled: list[str] = []
        for intent in self._intents.values():
            if (
                intent.state in {OrderState.SUBMITTED, OrderState.PARTIAL}
                and intent.submitted_at is not None
                and current - intent.submitted_at >= timedelta(
                    minutes=10 if intent.state == OrderState.PARTIAL else 5
                )
                and intent.broker_order_id
            ):
                self.broker.cancel(intent.broker_order_id)
                intent.state = OrderState.CANCELED
                canceled.append(intent.client_order_id)
        return tuple(canceled)

    def update_status(self, client_order_id: str, broker_status: str) -> OrderIntent:
        intent = self._intents[client_order_id]
        normalized = broker_status.upper()
        mapping = {
            "PARTIAL": OrderState.PARTIAL,
            "PARTIALLY_FILLED": OrderState.PARTIAL,
            "FILLED": OrderState.FILLED,
            "CANCELED": OrderState.CANCELED,
            "CANCELLED": OrderState.CANCELED,
            "REJECTED": OrderState.REJECTED,
        }
        if normalized not in mapping:
            raise ValueError(f"Unsupported broker order status {broker_status}.")
        target = mapping[normalized]
        allowed = {
            OrderState.SUBMITTED: {
                OrderState.PARTIAL,
                OrderState.FILLED,
                OrderState.CANCELED,
                OrderState.REJECTED,
            },
            OrderState.PARTIAL: {
                OrderState.FILLED,
                OrderState.CANCELED,
                OrderState.REJECTED,
            },
        }
        if target == intent.state:
            return intent
        if target not in allowed.get(intent.state, set()):
            raise ValueError(
                f"Invalid OMS transition {intent.state.value} -> {target.value}."
            )
        intent.state = target
        return intent

    def reprice_with_second_approval(
        self,
        client_order_id: str,
        *,
        new_limit_price: float,
        approved_by: str,
    ) -> OrderIntent:
        intent = self._intents[client_order_id]
        if intent.state != OrderState.CANCELED:
            raise ValueError("Only a canceled initial order may be repriced.")
        if not approved_by.strip():
            raise ValueError("An identifiable human approver is required for repricing.")
        if new_limit_price <= 0.0:
            raise ValueError("Repriced limit must be positive.")
        mid = intent.arrival_quote.mid
        offset_bps = abs(new_limit_price / mid - 1.0) * 10_000.0
        if offset_bps > 40.0 + 1e-9:
            raise ValueError("Repriced limit cannot exceed 40 bp from arrival mid.")
        intent.limit_price = float(new_limit_price)
        intent.approved_by = approved_by.strip()
        intent.approved_at = datetime.now(timezone.utc)
        intent.state = OrderState.APPROVED
        intent.notes.append("SECOND_APPROVAL_REPRICE")
        return intent


def reconcile_account(
    *,
    account: AccountSnapshot,
    expected_values: Mapping[str, float],
    open_order_ids: tuple[str, ...] = (),
) -> ReconciliationResult:
    actual = {ticker: position.market_value for ticker, position in account.positions.items()}
    tickers = set(actual).union(expected_values)
    difference = sum(abs(actual.get(ticker, 0.0) - expected_values.get(ticker, 0.0)) for ticker in tickers)
    threshold = max(5.0, account.nav * 0.0005)
    short = tuple(sorted(ticker for ticker, item in account.positions.items() if item.quantity < -1e-9))
    unknown = tuple(sorted(set(actual) - set(expected_values)))
    negative_cash = account.settled_cash < -1e-9 or account.available_cash < -1e-9
    reasons: list[str] = []
    if difference > threshold:
        reasons.append("ACCOUNT_VALUE_MISMATCH")
    if negative_cash:
        reasons.append("NEGATIVE_CASH_OR_FINANCING")
    if short:
        reasons.append("SHORT_POSITION")
    if unknown:
        reasons.append("UNKNOWN_POSITION")
    if open_order_ids:
        reasons.append("OPEN_ORDERS_REMAIN")
    return ReconciliationResult(
        matched=not reasons,
        difference_value=float(difference),
        threshold=float(threshold),
        negative_cash=negative_cash,
        short_positions=short,
        unknown_positions=unknown,
        open_orders=tuple(open_order_ids),
        reasons=tuple(reasons),
    )
