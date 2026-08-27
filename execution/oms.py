from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
from math import sqrt
from typing import Mapping, TYPE_CHECKING
from zoneinfo import ZoneInfo

from config.settings import Config
from execution.adapters import BrokerAdapter
from execution.models import (
    AccountSnapshot,
    BrokerEnvironment,
    ExecutionFill,
    OrderIntent,
    OrderState,
    Quote,
    ReconciliationResult,
    Side,
)
from execution.pretrade import PreTradeVerification
from services.models import SignalDecision, SignalStatus

if TYPE_CHECKING:
    from storage.repositories.execution import ExecutionRepository


class OrderManagementSystem:
    def __init__(
        self,
        config: Config,
        broker: BrokerAdapter,
        *,
        repository: ExecutionRepository | None = None,
    ) -> None:
        config.validate_execution_mode()
        if broker.external_connectivity and not config.broker_connectivity_enabled:
            raise ValueError(
                "External broker connectivity is disabled in the current operating mode."
            )
        if broker.environment == BrokerEnvironment.LIVE and not config.live_order_submission_enabled:
            raise ValueError("Live order submission is disabled by configuration.")
        if repository is not None and repository.environment != broker.environment.value:
            raise ValueError("OMS repository and broker environments differ.")
        self.config = config
        self.broker = broker
        self.repository = repository
        self._intents: dict[str, OrderIntent] = {}
        if repository is not None:
            self._intents = {
                item.client_order_id: item
                for item in repository.list_intents(nonterminal_only=True)
            }
        self.last_draft_warnings: tuple[str, ...] = ()

    @staticmethod
    def _idempotency_key(
        decision: SignalDecision,
        environment: BrokerEnvironment,
        ticker: str,
        side: Side,
    ) -> str:
        material = (
            f"{environment.value}|{decision.decision_id or decision.decision_key}|"
            f"{decision.strategy_version}|{decision.signal_session}|{ticker}|{side.value}"
        )
        return sha256(material.encode("utf-8")).hexdigest()

    def _get(self, client_order_id: str) -> OrderIntent:
        if self.repository is not None:
            intent = self.repository.get_intent(client_order_id)
            self._intents[client_order_id] = intent
            return intent
        return self._intents[client_order_id]

    def create_drafts(
        self,
        decision: SignalDecision,
        *,
        quotes: Mapping[str, Quote],
        median_daily_dollar_volume: Mapping[str, float],
        account: AccountSnapshot,
        verification: PreTradeVerification,
        paper_cycle_id: int | None = None,
        execution_model: str = "LMT",
    ) -> tuple[OrderIntent, ...]:
        if not verification.passed:
            raise ValueError(
                f"T+1 verification failed: {', '.join(verification.reasons)}"
            )
        if decision.status != SignalStatus.ACTIONABLE:
            raise ValueError("Only ACTIONABLE signal decisions may create order drafts.")
        if self.repository is not None and decision.decision_id is None:
            raise ValueError("Persistent orders require a persisted SignalDecision.")
        verified_at = verification.verified_at
        if verified_at.tzinfo is None:
            verified_at = verified_at.replace(tzinfo=timezone.utc)
        if execution_model == "REPLAY_OPEN":
            if verified_at.astimezone(decision.approval_deadline.tzinfo) >= decision.approval_deadline:
                raise ValueError("REPLAY_OPEN approval deadline is 09:25 ET.")
        else:
            local = verified_at.astimezone(decision.approval_deadline.tzinfo)
            hour, minute = (int(piece) for piece in self.config.execution_time_et.split(":"))
            if (local.hour, local.minute) < (hour, minute):
                raise ValueError(
                    f"Initial limit orders cannot be drafted before {self.config.execution_time_et} ET."
                )
        if account.settled_cash < -1e-9 or account.available_cash < -1e-9:
            raise ValueError("Cash-account invariant failed: negative cash detected.")
        if account.buying_power > account.nav + 1e-6:
            raise ValueError("Cash-account invariant failed: leverage detected.")
        if account.risk_state.upper() not in {"NORMAL", "WARNING", "DRIFT_REVIEW"}:
            raise ValueError("Halted paper accounts cannot create order drafts.")

        for ticker, weight in decision.target_weights.items():
            if ticker in {self.config.cash_asset, self.config.synthetic_cash_asset}:
                continue
            if weight > self.config.max_asset_weight + 1e-9:
                raise ValueError(f"{ticker} target exceeds the risky-asset maximum.")
            if 1e-9 < weight < self.config.min_asset_weight - 1e-9:
                raise ValueError(f"{ticker} target is below the risky-asset minimum.")

        current_values = {
            ticker: float(position.market_value)
            for ticker, position in account.positions.items()
        }
        tickers = set(decision.target_weights).union(current_values)
        dollar_deltas = {
            ticker: float(decision.target_weights.get(ticker, 0.0) * account.nav)
            - current_values.get(ticker, 0.0)
            for ticker in tickers
        }
        account_before = {
            "account_ref": account.account_ref,
            "nav": account.nav,
            "settled_cash": account.settled_cash,
            "unsettled_cash": account.unsettled_cash,
            "available_cash": account.available_cash,
            "positions": {
                ticker: {
                    "quantity": position.quantity,
                    "mark_price": position.mark_price,
                }
                for ticker, position in account.positions.items()
            },
            "pending_settlements": [
                {
                    "movement_key": item.movement_key,
                    "amount": item.amount,
                    "trade_date": item.trade_date,
                    "settlement_date": item.settlement_date,
                    "status": item.status,
                }
                for item in account.pending_settlements
            ],
            "high_water": account.high_water,
            "risk_state": account.risk_state,
            "total_commission": account.total_commission,
            "last_valuation_session": account.last_valuation_session,
            "captured_at": account.captured_at.isoformat(),
        }
        drafts: list[OrderIntent] = []
        warnings: list[str] = []
        drift_review = account.risk_state.upper() == "DRIFT_REVIEW" or decision.risk_state.upper() == "DRIFT_REVIEW"
        for ticker, dollar_delta in sorted(dollar_deltas.items()):
            if ticker == self.config.synthetic_cash_asset or abs(dollar_delta) < 1.0:
                continue
            if (
                drift_review
                and dollar_delta > 0.0
                and ticker != self.config.cash_asset
            ):
                warnings.append(f"BUY_BLOCKED_DRIFT_REVIEW:{ticker}")
                continue
            if ticker not in quotes:
                raise ValueError(f"Missing reference quote for {ticker}.")
            quote = quotes[ticker]
            if quote.bid <= 0.0 or quote.ask < quote.bid or quote.spread_bps > self.config.spread_block_bps:
                raise ValueError(f"{ticker} quote failed the 20 bp spread gate.")
            side = Side.BUY if dollar_delta > 0.0 else Side.SELL
            quantity = abs(dollar_delta) / quote.mid
            order_type = "REPLAY_OPEN" if execution_model == "REPLAY_OPEN" else "LMT"
            if not self.broker.supports_fractional(ticker, order_type) and abs(quantity - round(quantity)) > 1e-8:
                raise ValueError(f"Fractional {order_type} order is not supported for {ticker}.")
            adv = float(median_daily_dollar_volume.get(ticker, 0.0))
            if adv <= 0.0:
                raise ValueError(f"Missing positive ADV for {ticker}.")
            adv_fraction = abs(dollar_delta) / adv
            if adv_fraction > self.config.maximum_order_adv:
                raise ValueError(f"{ticker} order exceeds 1% ADV and is blocked.")
            impact_bps = 0.0
            if adv_fraction >= self.config.impact_model_adv_threshold:
                impact_bps = self.config.impact_coefficient_bps * sqrt(
                    adv_fraction / self.config.impact_model_adv_threshold
                )
            limit_price = quote.mid
            if order_type == "LMT":
                offset = 20.0 / 10_000.0
                limit_price *= 1.0 + offset if side == Side.BUY else 1.0 - offset
            client_id = self._idempotency_key(decision, self.broker.environment, ticker, side)
            existing = self._intents.get(client_id)
            if existing is None and self.repository is not None:
                try:
                    existing = self.repository.get_intent(client_id)
                except KeyError:
                    existing = None
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
                signal_decision_id=decision.decision_id,
                paper_cycle_id=paper_cycle_id,
                order_type=order_type,
                execution_session=decision.next_rebalance_session,
                account_before=account_before,
                created_at=verified_at.astimezone(timezone.utc),
            )
            if self.repository is not None:
                self.repository.save_intent(intent)
                intent = self.repository.get_intent(client_id)
            self._intents[client_id] = intent
            drafts.append(intent)

        self.last_draft_warnings = tuple(warnings)
        buys = sum(intent.notional for intent in drafts if intent.side == Side.BUY)
        sells = sum(intent.notional for intent in drafts if intent.side == Side.SELL)
        if buys > account.available_cash + sells + 1e-6:
            raise ValueError("Draft buys exceed cash available after planned sells.")
        return tuple(sorted(drafts, key=lambda item: item.side == Side.BUY))

    def approve(
        self,
        client_order_id: str,
        *,
        approved_by: str,
        approved_at: datetime | None = None,
    ) -> OrderIntent:
        intent = self._get(client_order_id)
        if intent.state == OrderState.APPROVED and intent.approved_by == approved_by.strip():
            return intent
        if intent.state != OrderState.DRAFT:
            raise ValueError("Only DRAFT orders can receive first approval.")
        if not approved_by.strip():
            raise ValueError("An identifiable human approver is required.")
        at = approved_at or datetime.now(timezone.utc)
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if intent.order_type in {"REPLAY_OPEN", "REPLAY_OPEN_LIQUIDATION"}:
            deadline = datetime.combine(
                datetime.fromisoformat(str(intent.execution_session)).date(),
                time(9, 25),
                tzinfo=ZoneInfo("America/New_York"),
            )
            if at.astimezone(ZoneInfo("America/New_York")) >= deadline:
                if self.repository is not None:
                    self.repository.transition_intent(
                        client_order_id,
                        expected=(OrderState.DRAFT,),
                        target=OrderState.MISSED,
                    )
                else:
                    intent.state = OrderState.MISSED
                raise ValueError("REPLAY_OPEN approval deadline is 09:25 ET.")
        if self.repository is not None:
            return self.repository.transition_intent(
                client_order_id,
                expected=(OrderState.DRAFT,),
                target=OrderState.APPROVED,
                values={"approved_at": at, "approved_by": approved_by.strip()},
            )
        intent.state = OrderState.APPROVED
        intent.approved_at = at.astimezone(timezone.utc)
        intent.approved_by = approved_by.strip()
        return intent

    def submit(self, client_order_id: str) -> OrderIntent:
        intent = self._get(client_order_id)
        if intent.state in {OrderState.SUBMITTED, OrderState.PARTIAL, OrderState.FILLED}:
            return intent
        if intent.state != OrderState.APPROVED or not intent.approved_by:
            raise ValueError("Every order requires explicit human approval before submission.")
        if intent.environment == BrokerEnvironment.LIVE and self.config.strategy_version == "UNFROZEN":
            raise ValueError("An unfrozen strategy cannot submit a live order.")
        if intent.order_type != "REPLAY_OPEN":
            account = self.broker.account_snapshot()
            if intent.side == Side.BUY and intent.notional > account.available_cash + 1e-6:
                raise ValueError("Broker-reported available cash is insufficient for this buy.")
        preview = self.broker.preview(intent)
        if not bool(preview.get("accepted", False)):
            if self.repository is not None:
                self.repository.transition_intent(
                    client_order_id,
                    expected=(OrderState.APPROVED,),
                    target=OrderState.REJECTED,
                )
            else:
                intent.state = OrderState.REJECTED
            raise ValueError("Broker preview rejected the order.")
        broker_order_id = self.broker.submit(intent)
        submitted_at = datetime.now(timezone.utc)
        if self.repository is not None:
            return self.repository.transition_intent(
                client_order_id,
                expected=(OrderState.APPROVED,),
                target=OrderState.SUBMITTED,
                values={
                    "broker_order_id": broker_order_id,
                    "submitted_at": submitted_at,
                },
            )
        intent.broker_order_id = broker_order_id
        intent.submitted_at = submitted_at
        intent.state = OrderState.SUBMITTED
        return intent

    def record_fill(
        self,
        client_order_id: str,
        fill: ExecutionFill,
        *,
        account_ref: str | None = None,
    ) -> OrderIntent:
        if self.repository is None:
            raise RuntimeError("Fill processing requires the persistent execution repository.")
        intent_id = self.repository.get_intent_id(client_order_id)
        if account_ref is None:
            self.repository.save_fill(intent_id, fill)
        else:
            self.repository.apply_paper_fill(
                account_ref=account_ref,
                order_intent_id=intent_id,
                fill=fill,
            )
        return self.repository.get_intent(client_order_id)

    def cancel_stale(self, *, now: datetime | None = None) -> tuple[str, ...]:
        current = now or datetime.now(timezone.utc)
        intents = (
            self.repository.list_intents(nonterminal_only=True)
            if self.repository is not None
            else tuple(self._intents.values())
        )
        canceled: list[str] = []
        for intent in intents:
            if (
                intent.state in {OrderState.SUBMITTED, OrderState.PARTIAL}
                and intent.submitted_at is not None
                and current - intent.submitted_at
                >= timedelta(minutes=10 if intent.state == OrderState.PARTIAL else 5)
                and intent.broker_order_id
            ):
                self.broker.cancel(intent.broker_order_id)
                if self.repository is not None:
                    self.repository.transition_intent(
                        intent.client_order_id,
                        expected=(OrderState.SUBMITTED, OrderState.PARTIAL),
                        target=OrderState.CANCELED,
                    )
                else:
                    intent.state = OrderState.CANCELED
                canceled.append(intent.client_order_id)
        return tuple(canceled)

    def update_status(self, client_order_id: str, broker_status: str) -> OrderIntent:
        normalized = broker_status.upper()
        if normalized in {"PARTIAL", "PARTIALLY_FILLED", "FILLED"}:
            raise ValueError("PARTIAL/FILLED states are derived only from persisted fills.")
        mapping = {
            "CANCELED": OrderState.CANCELED,
            "CANCELLED": OrderState.CANCELED,
            "REJECTED": OrderState.REJECTED,
        }
        if normalized not in mapping:
            raise ValueError(f"Unsupported broker order status {broker_status}.")
        intent = self._get(client_order_id)
        target = mapping[normalized]
        if intent.state == target:
            return intent
        if self.repository is not None:
            return self.repository.transition_intent(
                client_order_id,
                expected=(OrderState.SUBMITTED, OrderState.PARTIAL),
                target=target,
            )
        if intent.state not in {OrderState.SUBMITTED, OrderState.PARTIAL}:
            raise ValueError(f"Invalid OMS transition {intent.state.value} -> {target.value}.")
        intent.state = target
        return intent

    def reprice_with_second_approval(
        self,
        client_order_id: str,
        *,
        new_limit_price: float,
        approved_by: str,
    ) -> OrderIntent:
        intent = self._get(client_order_id)
        if intent.order_type == "REPLAY_OPEN":
            raise ValueError("REPLAY_OPEN orders cannot be repriced.")
        if intent.state != OrderState.CANCELED:
            raise ValueError("Only a canceled initial order may be repriced.")
        if not approved_by.strip() or new_limit_price <= 0.0:
            raise ValueError("Repricing requires a positive limit and identifiable approver.")
        offset_bps = abs(new_limit_price / intent.arrival_quote.mid - 1.0) * 10_000.0
        if offset_bps > 40.0 + 1e-9:
            raise ValueError("Repriced limit cannot exceed 40 bp from arrival mid.")
        if self.repository is not None:
            raise RuntimeError("Persistent repricing is outside the local REPLAY_OPEN workflow.")
        intent.limit_price = float(new_limit_price)
        intent.approved_by = approved_by.strip()
        intent.approved_at = datetime.now(timezone.utc)
        intent.state = OrderState.APPROVED
        intent.notes.append("SECOND_APPROVAL_REPRICE")
        return intent


def reconcile_account(
    *,
    account: AccountSnapshot,
    expected_values: Mapping[str, float] | None = None,
    expected_account: AccountSnapshot | None = None,
    expected_positions: Mapping[str, float] | None = None,
    expected_total_commission: float | None = None,
    open_order_ids: tuple[str, ...] = (),
    reconciled_at: datetime | None = None,
) -> ReconciliationResult:
    """Reconcile quantities, cash, NAV identity, fees, unknowns and open orders."""
    reasons: list[str] = []
    threshold = max(5.0, account.nav * 0.0005)
    actual_quantities = {
        ticker: float(position.quantity) for ticker, position in account.positions.items()
    }
    quantity_differences: dict[str, float] = {}
    settled_difference = 0.0
    unsettled_difference = 0.0
    available_difference = 0.0
    nav_difference = 0.0
    commission_difference = 0.0
    unknown: tuple[str, ...]
    value_difference = 0.0

    if expected_account is not None:
        expected_quantities = {
            ticker: float(position.quantity)
            for ticker, position in expected_account.positions.items()
        }
        expected_total_commission = expected_account.total_commission
        settled_difference = account.settled_cash - expected_account.settled_cash
        unsettled_difference = account.unsettled_cash - expected_account.unsettled_cash
        available_difference = account.available_cash - expected_account.available_cash
        nav_difference = account.nav - expected_account.nav
    else:
        expected_quantities = {
            str(ticker): float(quantity)
            for ticker, quantity in dict(expected_positions or {}).items()
        }
    if expected_quantities:
        for ticker in set(actual_quantities).union(expected_quantities):
            difference = actual_quantities.get(ticker, 0.0) - expected_quantities.get(ticker, 0.0)
            if abs(difference) > 1e-8:
                quantity_differences[ticker] = difference
        if quantity_differences:
            reasons.append("POSITION_QUANTITY_MISMATCH")
    unknown = tuple(sorted(set(actual_quantities) - set(expected_quantities))) if expected_quantities else ()

    if expected_values is not None:
        actual_values = {
            ticker: position.market_value for ticker, position in account.positions.items()
        }
        tickers = set(actual_values).union(expected_values)
        value_difference = sum(
            abs(actual_values.get(ticker, 0.0) - expected_values.get(ticker, 0.0))
            for ticker in tickers
        )
        if value_difference > threshold:
            reasons.append("ACCOUNT_VALUE_MISMATCH")

    identity_nav = account.settled_cash + account.unsettled_cash + sum(
        position.market_value for position in account.positions.values()
    )
    identity_difference = account.nav - identity_nav
    nav_difference = nav_difference or identity_difference
    dollar_differences = (
        abs(settled_difference)
        + abs(unsettled_difference)
        + abs(available_difference)
        + abs(nav_difference)
    )
    if dollar_differences > threshold:
        reasons.append("CASH_OR_NAV_MISMATCH")
    if abs(identity_difference) > threshold:
        reasons.append("NAV_IDENTITY_MISMATCH")
    if expected_total_commission is not None:
        commission_difference = account.total_commission - expected_total_commission
        if abs(commission_difference) > 0.01:
            reasons.append("COMMISSION_MISMATCH")
    short = tuple(
        sorted(ticker for ticker, quantity in actual_quantities.items() if quantity < -1e-9)
    )
    negative_cash = account.settled_cash < -1e-9 or account.available_cash < -1e-9
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
        difference_value=float(value_difference + dollar_differences + abs(commission_difference)),
        threshold=float(threshold),
        negative_cash=negative_cash,
        short_positions=short,
        unknown_positions=unknown,
        open_orders=tuple(open_order_ids),
        reasons=tuple(dict.fromkeys(reasons)),
        reconciled_at=reconciled_at or datetime.now(timezone.utc),
        quantity_differences=quantity_differences,
        settled_cash_difference=float(settled_difference),
        unsettled_cash_difference=float(unsettled_difference),
        available_cash_difference=float(available_difference),
        nav_difference=float(nav_difference),
        commission_difference=float(commission_difference),
    )
