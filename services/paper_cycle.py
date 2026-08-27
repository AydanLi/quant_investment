from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from hashlib import sha256
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import Engine

from config.settings import Config
from data.calendar import NyseCalendar
from execution.adapters import InMemoryPaperBroker
from execution.models import (
    AccountSnapshot,
    BrokerEnvironment,
    ExecutionFill,
    OrderIntent,
    OrderState,
    Quote,
    Side,
)
from execution.oms import OrderManagementSystem, reconcile_account
from execution.pretrade import PreTradeVerification
from services.models import PaperCycleStatus, SignalDecision, StoredSignalDecision
from services.pushover import send_pushover
from storage.repositories.execution import ExecutionRepository
from storage.repositories.signals import SignalRepository


NEW_YORK = ZoneInfo("America/New_York")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=NEW_YORK)
    return value


def _account_payload(account: AccountSnapshot) -> dict[str, object]:
    return {
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


@dataclass(frozen=True)
class PaperCycleResult:
    decision_id: int
    paper_cycle_id: int
    status: PaperCycleStatus
    account: AccountSnapshot
    order_states: Mapping[str, str]
    reconciliation_id: int | None = None


class PaperCycle:
    """One-user, restart-safe REPLAY_OPEN workflow backed only by SQLite."""

    def __init__(
        self,
        config: Config,
        *,
        engine: Engine,
        account_ref: str = "local-paper",
        notifier: Callable[[str, str], str] = send_pushover,
        calendar: NyseCalendar | None = None,
    ) -> None:
        self.config = config
        self.account_ref = account_ref
        self.signals = SignalRepository(engine=engine)
        self.execution = ExecutionRepository(engine=engine, environment="PAPER")
        self.notifier = notifier
        self.calendar = calendar or NyseCalendar()

    def initialize_account(
        self,
        *,
        strategy_version: str,
        initial_cash: float | None = None,
        at: datetime | None = None,
    ) -> AccountSnapshot:
        self.signals.assert_local_sim_ready(strategy_version)
        return self.execution.initialize_account(
            account_ref=self.account_ref,
            strategy_version=strategy_version,
            initial_cash=float(
                self.config.initial_capital if initial_cash is None else initial_cash
            ),
            at=at,
        )

    def persist_decision(
        self,
        decision: SignalDecision,
        *,
        recorded_at: datetime | None = None,
    ) -> StoredSignalDecision:
        self.signals.assert_local_sim_ready(
            decision.strategy_version,
            universe_version=decision.universe_version,
            dataset_snapshot_id=decision.dataset_snapshot_id,
            decision=decision,
        )
        persisted = self.signals.save_decision(decision, environment="PAPER")
        cycle_id = self.signals.ensure_paper_cycle(
            int(persisted.decision_id), recorded_at=recorded_at
        )
        stored = self.signals.get_stored_decision(int(persisted.decision_id))
        if stored.cycle_status == PaperCycleStatus.MISSED:
            self._record_incident(
                stored,
                code="PAPER_DECISION_MISSED",
                severity="HIGH",
                details={"reason": stored.missed_reason or "DECISION_AFTER_0925_ET"},
            )
            self.flush_notifications()
        if cycle_id != stored.paper_cycle_id:  # pragma: no cover - schema invariant
            raise RuntimeError("Paper-cycle identity changed during persistence.")
        return stored

    def _record_incident(
        self,
        stored: StoredSignalDecision,
        *,
        code: str,
        severity: str,
        details: Mapping[str, object],
        trigger_value: float | None = None,
        reconciliation_id: int | None = None,
        order_intent_id: int | None = None,
    ) -> int:
        return self.execution.record_incident(
            strategy_version=stored.decision.strategy_version,
            code=code,
            severity=severity,
            trigger_value=trigger_value,
            details=details,
            account_ref=self.account_ref,
            paper_cycle_id=stored.paper_cycle_id,
            reconciliation_id=reconciliation_id,
            order_intent_id=order_intent_id,
        )

    def flush_notifications(self) -> Mapping[int, str]:
        outcomes: dict[int, str] = {}
        for incident in self.execution.pending_notifications():
            title = f"Quant Research [{incident.severity}]"
            message = f"{incident.code} | account={incident.account_ref or '-'}"
            try:
                request_id = self.notifier(message, title)
            except Exception as exc:  # network/credentials must not erase the incident
                self.execution.mark_notification_failed(incident.incident_id, str(exc))
                outcomes[incident.incident_id] = "FAILED"
            else:
                self.execution.mark_notification_sent(incident.incident_id, request_id)
                outcomes[incident.incident_id] = "SENT"
        return outcomes

    def _miss(self, stored: StoredSignalDecision, *, reason: str, at: datetime) -> None:
        self.signals.transition_cycle(
            stored.paper_cycle_id,
            PaperCycleStatus.MISSED,
            expected=(PaperCycleStatus.PENDING, PaperCycleStatus.DRAFTED),
            at=at,
            missed_reason=reason,
        )
        self.execution.mark_cycle_orders_missed(stored.paper_cycle_id)
        self._record_incident(
            stored,
            code="PAPER_APPROVAL_MISSED",
            severity="HIGH",
            details={"reason": reason, "deadline": stored.approval_deadline.isoformat()},
        )
        self.flush_notifications()

    def _create_liquidation_drafts(
        self,
        stored: StoredSignalDecision,
        *,
        account: AccountSnapshot,
        prices: Mapping[str, float],
        at: datetime,
        replace_terminal: bool = False,
    ) -> tuple[str, ...]:
        existing = tuple(
            intent
            for intent in self.execution.list_intents(
                paper_cycle_id=stored.paper_cycle_id
            )
            if intent.order_type == "REPLAY_OPEN_LIQUIDATION"
        )
        active = tuple(
            intent
            for intent in existing
            if intent.state
            in {
                OrderState.DRAFT,
                OrderState.APPROVED,
                OrderState.SUBMITTED,
                OrderState.PARTIAL,
            }
        )
        if active:
            return tuple(intent.client_order_id for intent in active)
        if existing and not replace_terminal:
            return tuple(intent.client_order_id for intent in existing)
        next_session = str(
            self.calendar.next_session(pd.Timestamp(at.astimezone(NEW_YORK).date())).date()
        )
        created: list[str] = []
        account_before = _account_payload(account)
        for ticker, position in sorted(account.positions.items()):
            if position.quantity <= 1e-8:
                continue
            if ticker not in prices or float(prices[ticker]) <= 0.0:
                raise ValueError(f"Missing liquidation reference price for {ticker}.")
            client_id = sha256(
                f"LIQUIDATE|{stored.paper_cycle_id}|{next_session}|{ticker}".encode("utf-8")
            ).hexdigest()
            quote = Quote(ticker, float(prices[ticker]), float(prices[ticker]), at)
            intent = OrderIntent(
                client_order_id=client_id,
                environment=BrokerEnvironment.PAPER,
                strategy_version=stored.decision.strategy_version,
                signal_session=stored.decision.signal_session,
                ticker=ticker,
                side=Side.SELL,
                quantity=position.quantity,
                limit_price=float(prices[ticker]),
                arrival_quote=quote,
                adv_fraction=0.0,
                estimated_impact_bps=0.0,
                signal_decision_id=stored.decision.decision_id,
                paper_cycle_id=stored.paper_cycle_id,
                order_type="REPLAY_OPEN_LIQUIDATION",
                execution_session=next_session,
                account_before=account_before,
                created_at=at.astimezone(timezone.utc),
                notes=["RISK_LIQUIDATION_DRAFT", "REQUIRES_NEW_HUMAN_APPROVAL"],
            )
            self.execution.save_intent(intent)
            created.append(client_id)
        return tuple(created)

    def redraft_liquidation(
        self,
        decision_id: int,
        *,
        reference_prices: Mapping[str, float],
        requested_by: str,
        reason: str,
        requested_at: datetime,
    ) -> tuple[str, ...]:
        """Explicitly replace terminal missed/canceled liquidation drafts."""
        stored = self.signals.get_stored_decision(decision_id)
        if stored.cycle_status != PaperCycleStatus.HALTED:
            raise ValueError("Risk liquidation redraft requires a HALTED paper cycle.")
        operator = requested_by.strip()
        explanation = reason.strip()
        if not operator or not explanation:
            raise ValueError("Liquidation redraft requires an operator and explanation.")
        account = self.execution.get_account(self.account_ref)
        if account.risk_state != "DRAWDOWN_HALTED":
            raise ValueError("Only DRAWDOWN_HALTED may redraft liquidation orders.")
        existing = tuple(
            intent
            for intent in self.execution.list_intents(
                paper_cycle_id=stored.paper_cycle_id
            )
            if intent.order_type == "REPLAY_OPEN_LIQUIDATION"
        )
        active = tuple(
            intent
            for intent in existing
            if intent.state
            in {
                OrderState.DRAFT,
                OrderState.APPROVED,
                OrderState.SUBMITTED,
                OrderState.PARTIAL,
            }
        )
        if active:
            return tuple(intent.client_order_id for intent in active)
        if not existing or any(
            intent.state == OrderState.FILLED for intent in existing
        ):
            raise ValueError("No terminal missed/canceled liquidation is eligible for redraft.")
        now = _aware(requested_at)
        self._record_incident(
            stored,
            code="PAPER_LIQUIDATION_REDRAFTED",
            severity="CRITICAL",
            details={
                "requested_by": operator,
                "reason": explanation,
                "replaced_order_ids": [
                    intent.client_order_id for intent in existing
                ],
            },
        )
        created = self._create_liquidation_drafts(
            stored,
            account=account,
            prices=reference_prices,
            at=now,
            replace_terminal=True,
        )
        self.flush_notifications()
        return created

    def _halt_cycle(
        self,
        stored: StoredSignalDecision,
        *,
        account: AccountSnapshot,
        prices: Mapping[str, float],
        at: datetime,
    ) -> PaperCycleResult:
        self.execution.cancel_cycle_orders(stored.paper_cycle_id)
        self.signals.transition_cycle(
            stored.paper_cycle_id,
            PaperCycleStatus.HALTED,
            expected=(
                PaperCycleStatus.PENDING,
                PaperCycleStatus.DRAFTED,
                PaperCycleStatus.APPROVED,
                PaperCycleStatus.FILLED,
                PaperCycleStatus.RECONCILED,
                PaperCycleStatus.COMPLETED,
                PaperCycleStatus.HALTED,
            ),
            at=at,
        )
        if account.risk_state == "DRAWDOWN_HALTED":
            self._create_liquidation_drafts(
                stored, account=account, prices=prices, at=at
            )
        self.flush_notifications()
        intents = self.execution.list_intents(paper_cycle_id=stored.paper_cycle_id)
        return PaperCycleResult(
            decision_id=int(stored.decision.decision_id),
            paper_cycle_id=stored.paper_cycle_id,
            status=PaperCycleStatus.HALTED,
            account=account,
            order_states={item.client_order_id: item.state.value for item in intents},
        )

    def _reconcile_cycle(
        self,
        stored: StoredSignalDecision,
        *,
        mark_prices: Mapping[str, float],
        at: datetime,
        order_type: str | None = None,
    ) -> tuple[AccountSnapshot, int, bool]:
        expected = self.execution.expected_account_from_cycle(
            account_ref=self.account_ref,
            paper_cycle_id=stored.paper_cycle_id,
            mark_prices=mark_prices,
            at=at,
            order_type=order_type,
        )
        actual = self.execution.get_account(self.account_ref)
        result = reconcile_account(
            account=actual,
            expected_account=expected,
            expected_total_commission=expected.total_commission,
            open_order_ids=self.execution.open_order_ids(
                paper_cycle_id=stored.paper_cycle_id
            ),
            reconciled_at=at,
        )
        reconciliation_id = self.execution.save_reconciliation(
            account_ref=self.account_ref,
            nav=actual.nav,
            result=result,
            paper_cycle_id=stored.paper_cycle_id,
        )
        if not result.matched:
            self._record_incident(
                stored,
                code="PAPER_RECONCILIATION_LOCK",
                severity="CRITICAL",
                reconciliation_id=reconciliation_id,
                details={"reasons": list(result.reasons)},
            )
        return actual, reconciliation_id, result.matched

    def draft_orders(
        self,
        decision_id: int,
        *,
        reference_prices: Mapping[str, float],
        median_daily_dollar_volume: Mapping[str, float],
        verified_at: datetime,
    ) -> tuple[str, ...]:
        stored = self.signals.get_stored_decision(decision_id)
        now = _aware(verified_at)
        if stored.cycle_status == PaperCycleStatus.MISSED:
            raise ValueError("Paper cycle is MISSED and cannot create orders.")
        if now.astimezone(timezone.utc) >= stored.approval_deadline.astimezone(timezone.utc):
            self._miss(stored, reason="DRAFT_AFTER_0925_ET", at=now)
            raise ValueError("REPLAY_OPEN draft deadline is 09:25 ET.")
        if now.astimezone(NEW_YORK).date().isoformat() != stored.decision.next_rebalance_session:
            raise ValueError("Draft must be created on the T+1 execution session.")
        account = self.execution.settle_due(
            self.account_ref,
            session=stored.decision.next_rebalance_session,
            settled_at=now,
        )
        try:
            account, _trigger, _trigger_value, _incident_id = self.execution.mark_account(
                self.account_ref,
                prices=reference_prices,
                valuation_session=stored.decision.signal_session,
                at=now,
                drawdown_limit=self.config.portfolio_drawdown_stop,
                daily_loss_limit=self.config.daily_loss_halt,
                paper_cycle_id=stored.paper_cycle_id,
            )
        except Exception as exc:
            self._record_incident(
                stored,
                code="PAPER_REFERENCE_MARK_FAILED",
                severity="HIGH",
                details={"error": str(exc)},
            )
            self.flush_notifications()
            raise
        if account.risk_state not in {"NORMAL", "WARNING", "DRIFT_REVIEW"}:
            self._halt_cycle(stored, account=account, prices=reference_prices, at=now)
            raise ValueError("Paper account is risk halted.")
        self.execution.save_cycle_baseline(
            account=account, paper_cycle_id=stored.paper_cycle_id
        )
        quotes = {
            ticker: Quote(ticker, float(price), float(price), now)
            for ticker, price in reference_prices.items()
            if float(price) > 0.0
        }
        verification = PreTradeVerification(now, True, ())
        broker = InMemoryPaperBroker(account, quotes)
        oms = OrderManagementSystem(
            self.config, broker, repository=self.execution
        )
        drafts = oms.create_drafts(
            stored.decision,
            quotes=quotes,
            median_daily_dollar_volume=median_daily_dollar_volume,
            account=account,
            verification=verification,
            paper_cycle_id=stored.paper_cycle_id,
            execution_model="REPLAY_OPEN",
        )
        self.signals.transition_cycle(
            stored.paper_cycle_id,
            PaperCycleStatus.DRAFTED,
            expected=(PaperCycleStatus.PENDING, PaperCycleStatus.DRAFTED),
            at=now,
        )
        return tuple(intent.client_order_id for intent in drafts)

    def approve(
        self,
        decision_id: int,
        *,
        approved_by: str,
        approved_at: datetime,
    ) -> tuple[str, ...]:
        stored = self.signals.get_stored_decision(decision_id)
        now = _aware(approved_at)
        if stored.cycle_status == PaperCycleStatus.MISSED:
            raise ValueError("Paper cycle is MISSED and cannot be approved.")
        if stored.cycle_status not in {
            PaperCycleStatus.DRAFTED,
            PaperCycleStatus.APPROVED,
        }:
            raise ValueError("Only a drafted paper cycle may be approved.")
        intents = self.execution.list_intents(paper_cycle_id=stored.paper_cycle_id)
        prior_approval = next((item for item in intents if item.approved_at), None)
        effective_at = prior_approval.approved_at if prior_approval else now
        effective_by = prior_approval.approved_by if prior_approval else approved_by.strip()
        if not effective_by:
            raise ValueError("An identifiable human approver is required.")
        if effective_at.astimezone(timezone.utc) >= stored.approval_deadline.astimezone(timezone.utc):
            self._miss(stored, reason="APPROVAL_AFTER_0925_ET", at=now)
            raise ValueError("REPLAY_OPEN approval deadline is 09:25 ET.")
        account = self.execution.get_account(self.account_ref)
        quotes = {item.ticker: item.arrival_quote for item in intents}
        oms = OrderManagementSystem(
            self.config,
            InMemoryPaperBroker(account, quotes),
            repository=self.execution,
        )
        approved: list[str] = []
        for intent in intents:
            if intent.state == OrderState.DRAFT:
                intent = oms.approve(
                    intent.client_order_id,
                    approved_by=effective_by,
                    approved_at=effective_at,
                )
            if intent.state == OrderState.APPROVED:
                approved.append(intent.client_order_id)
        self.signals.transition_cycle(
            stored.paper_cycle_id,
            PaperCycleStatus.APPROVED,
            expected=(PaperCycleStatus.DRAFTED, PaperCycleStatus.APPROVED),
            at=effective_at,
        )
        return tuple(approved)

    def approve_liquidation(
        self,
        decision_id: int,
        *,
        approved_by: str,
        approved_at: datetime,
    ) -> tuple[str, ...]:
        """Approve only the persisted drawdown liquidation; the cycle stays halted."""
        stored = self.signals.get_stored_decision(decision_id)
        if stored.cycle_status != PaperCycleStatus.HALTED:
            raise ValueError("Risk liquidation requires a HALTED paper cycle.")
        account = self.execution.get_account(self.account_ref)
        if account.risk_state != "DRAWDOWN_HALTED":
            raise ValueError("Only DRAWDOWN_HALTED may approve liquidation orders.")
        operator = approved_by.strip()
        if not operator:
            raise ValueError("An identifiable human approver is required.")
        now = _aware(approved_at)
        intents = tuple(
            intent
            for intent in self.execution.list_intents(
                paper_cycle_id=stored.paper_cycle_id
            )
            if intent.order_type == "REPLAY_OPEN_LIQUIDATION"
        )
        if not intents:
            raise ValueError("No drawdown liquidation draft exists.")
        if any(intent.side != Side.SELL for intent in intents):
            raise ValueError("Risk liquidation may contain SELL orders only.")
        remaining_by_ticker: dict[str, float] = {}
        for intent in intents:
            if intent.state != OrderState.FILLED:
                remaining_by_ticker[intent.ticker] = (
                    remaining_by_ticker.get(intent.ticker, 0.0)
                    + intent.remaining_quantity
                )
        positions = {
            ticker: position.quantity
            for ticker, position in account.positions.items()
            if position.quantity > 1e-8
        }
        if remaining_by_ticker and (
            set(remaining_by_ticker) != set(positions)
            or any(
                abs(remaining_by_ticker[ticker] - positions[ticker]) > 1e-8
                for ticker in remaining_by_ticker
            )
        ):
            raise ValueError("Liquidation drafts no longer match current holdings.")

        quotes = {intent.ticker: intent.arrival_quote for intent in intents}
        oms = OrderManagementSystem(
            self.config,
            InMemoryPaperBroker(account, quotes),
            repository=self.execution,
        )
        approved: list[str] = []
        for intent in intents:
            if intent.execution_session is None:
                raise ValueError("Liquidation draft has no execution session.")
            deadline = datetime.combine(
                datetime.fromisoformat(intent.execution_session).date(),
                time(9, 25),
                tzinfo=NEW_YORK,
            )
            effective_at = intent.approved_at or now
            if intent.state == OrderState.DRAFT and effective_at >= deadline:
                self.execution.transition_intent(
                    intent.client_order_id,
                    expected=(OrderState.DRAFT,),
                    target=OrderState.MISSED,
                )
                self._record_incident(
                    stored,
                    code="PAPER_LIQUIDATION_APPROVAL_MISSED",
                    severity="CRITICAL",
                    details={
                        "execution_session": intent.execution_session,
                        "deadline": deadline.isoformat(),
                    },
                )
                self.flush_notifications()
                raise ValueError("Liquidation approval deadline is 09:25 ET.")
            if intent.state == OrderState.DRAFT:
                intent = oms.approve(
                    intent.client_order_id,
                    approved_by=operator,
                    approved_at=effective_at,
                )
            if intent.state in {
                OrderState.APPROVED,
                OrderState.SUBMITTED,
                OrderState.PARTIAL,
                OrderState.FILLED,
            }:
                approved.append(intent.client_order_id)
            else:
                raise ValueError(
                    f"Liquidation order is not executable: {intent.state.value}."
                )
        return tuple(approved)

    def materialize_liquidation_open(
        self,
        decision_id: int,
        *,
        open_prices: Mapping[str, float],
        published_at: datetime,
    ) -> PaperCycleResult:
        """Replay an approved drawdown liquidation at its T+1 raw open."""
        stored = self.signals.get_stored_decision(decision_id)
        if stored.cycle_status != PaperCycleStatus.HALTED:
            raise ValueError("Risk liquidation requires a HALTED paper cycle.")
        account = self.execution.get_account(self.account_ref)
        if account.risk_state != "DRAWDOWN_HALTED":
            raise ValueError("Only DRAWDOWN_HALTED may materialize liquidation.")
        intents = tuple(
            intent
            for intent in self.execution.list_intents(
                paper_cycle_id=stored.paper_cycle_id
            )
            if intent.order_type == "REPLAY_OPEN_LIQUIDATION"
        )
        if not intents or any(intent.side != Side.SELL for intent in intents):
            raise ValueError("A SELL-only drawdown liquidation draft is required.")
        sessions = {intent.execution_session for intent in intents}
        if None in sessions or len(sessions) != 1:
            raise ValueError("Liquidation orders disagree on their execution session.")
        execution_session = str(next(iter(sessions)))
        now = _aware(published_at)
        local = now.astimezone(NEW_YORK)
        if local.date().isoformat() != execution_session:
            raise ValueError("Liquidation opens belong to the wrong execution session.")
        if local.time().replace(tzinfo=None) < time(9, 30):
            raise ValueError("Liquidation opens cannot be materialized before 09:30 ET.")
        blocked = [
            intent.client_order_id
            for intent in intents
            if intent.state
            not in {
                OrderState.APPROVED,
                OrderState.SUBMITTED,
                OrderState.PARTIAL,
                OrderState.FILLED,
            }
        ]
        if blocked:
            raise ValueError("Every liquidation order requires timely human approval.")
        required = {
            intent.ticker
            for intent in intents
            if intent.state != OrderState.FILLED
        }
        missing = sorted(required - set(open_prices))
        invalid = sorted(
            ticker
            for ticker in required
            if ticker in open_prices and float(open_prices[ticker]) <= 0.0
        )
        if missing or invalid:
            raise ValueError(
                "Missing or invalid liquidation opens: "
                + ", ".join(sorted(set(missing + invalid)))
            )
        account = self.execution.settle_due(
            self.account_ref, session=execution_session, settled_at=now
        )
        account, _trigger, _trigger_value, _incident_id = self.execution.mark_account(
            self.account_ref,
            prices=open_prices,
            valuation_session=execution_session,
            at=now,
            drawdown_limit=self.config.portfolio_drawdown_stop,
            daily_loss_limit=self.config.daily_loss_halt,
            paper_cycle_id=stored.paper_cycle_id,
        )
        quotes = {
            ticker: Quote(ticker, float(price), float(price), now)
            for ticker, price in open_prices.items()
            if float(price) > 0.0
        }
        oms = OrderManagementSystem(
            self.config,
            InMemoryPaperBroker(account, quotes),
            repository=self.execution,
        )
        settlement_date = str(
            self.calendar.next_session(pd.Timestamp(execution_session)).date()
        )
        for original in intents:
            intent = self.execution.get_intent(original.client_order_id)
            if intent.state == OrderState.FILLED:
                continue
            if intent.state == OrderState.APPROVED:
                intent = oms.submit(intent.client_order_id)
            if intent.state not in {OrderState.SUBMITTED, OrderState.PARTIAL}:
                raise ValueError("Liquidation order is not in an executable state.")
            quantity = intent.remaining_quantity
            raw_open = float(open_prices[intent.ticker])
            directional_bps = self.config.slippage_bps + intent.estimated_impact_bps
            price = raw_open * (1.0 - directional_bps / 10_000.0)
            commission = quantity * price * self.config.trading_cost_bps / 10_000.0
            execution_key = sha256(
                f"{intent.client_order_id}|{quantity:.12f}|{price:.12f}".encode(
                    "utf-8"
                )
            ).hexdigest()
            oms.record_fill(
                intent.client_order_id,
                ExecutionFill(
                    client_order_id=intent.client_order_id,
                    broker_execution_id=f"liquidation-{execution_key}",
                    filled_at=now.astimezone(timezone.utc),
                    quantity=quantity,
                    price=price,
                    commission=commission,
                    implementation_shortfall_bps=directional_bps,
                    settlement_date=settlement_date,
                ),
                account_ref=self.account_ref,
            )
        account, reconciliation_id, matched = self._reconcile_cycle(
            stored,
            mark_prices=open_prices,
            at=now,
            order_type="REPLAY_OPEN_LIQUIDATION",
        )
        self.flush_notifications()
        final_intents = self.execution.list_intents(
            paper_cycle_id=stored.paper_cycle_id
        )
        return PaperCycleResult(
            decision_id=decision_id,
            paper_cycle_id=stored.paper_cycle_id,
            status=PaperCycleStatus.HALTED,
            account=account,
            order_states={
                intent.client_order_id: intent.state.value for intent in final_intents
            },
            reconciliation_id=reconciliation_id,
        )

    def materialize_open(
        self,
        decision_id: int,
        *,
        open_prices: Mapping[str, float],
        published_at: datetime,
    ) -> PaperCycleResult:
        stored = self.signals.get_stored_decision(decision_id)
        now = _aware(published_at)
        local = now.astimezone(NEW_YORK)
        if local.date().isoformat() != stored.decision.next_rebalance_session:
            raise ValueError("Open prices belong to the wrong execution session.")
        if local.time().replace(tzinfo=None) < time(9, 30):
            raise ValueError("Open prices cannot be materialized before 09:30 ET.")
        if stored.cycle_status == PaperCycleStatus.HALTED:
            account = self.execution.get_account(self.account_ref)
            return self._halt_cycle(stored, account=account, prices=open_prices, at=now)
        if stored.cycle_status == PaperCycleStatus.COMPLETED:
            account, reconciliation_id, matched = self._reconcile_cycle(
                stored, mark_prices=open_prices, at=now
            )
            if not matched:
                account = self.execution.set_account_risk_state(
                    self.account_ref, "RECONCILIATION_HALTED", at=now
                )
                halted = self._halt_cycle(stored, account=account, prices=open_prices, at=now)
                return PaperCycleResult(
                    decision_id=halted.decision_id,
                    paper_cycle_id=halted.paper_cycle_id,
                    status=halted.status,
                    account=halted.account,
                    order_states=halted.order_states,
                    reconciliation_id=reconciliation_id,
                )
            intents = self.execution.list_intents(paper_cycle_id=stored.paper_cycle_id)
            return PaperCycleResult(
                decision_id=decision_id,
                paper_cycle_id=stored.paper_cycle_id,
                status=PaperCycleStatus.COMPLETED,
                account=account,
                order_states={item.client_order_id: item.state.value for item in intents},
                reconciliation_id=reconciliation_id,
            )
        if stored.cycle_status not in {
            PaperCycleStatus.APPROVED,
            PaperCycleStatus.FILLED,
            PaperCycleStatus.RECONCILED,
        }:
            raise ValueError("Paper cycle must be approved before open replay.")
        invalid = [ticker for ticker, value in open_prices.items() if float(value) <= 0.0]
        if invalid:
            raise ValueError(f"Invalid open prices: {', '.join(sorted(invalid))}.")
        account = self.execution.settle_due(
            self.account_ref,
            session=stored.decision.next_rebalance_session,
            settled_at=now,
        )
        try:
            account, _trigger, _trigger_value, _incident_id = self.execution.mark_account(
                self.account_ref,
                prices=open_prices,
                valuation_session=stored.decision.next_rebalance_session,
                at=now,
                drawdown_limit=self.config.portfolio_drawdown_stop,
                daily_loss_limit=self.config.daily_loss_halt,
                paper_cycle_id=stored.paper_cycle_id,
            )
        except Exception as exc:
            self._record_incident(
                stored,
                code="PAPER_MARK_FAILED",
                severity="HIGH",
                details={"error": str(exc)},
            )
            self.flush_notifications()
            raise
        if account.risk_state not in {"NORMAL", "WARNING", "DRIFT_REVIEW"}:
            return self._halt_cycle(stored, account=account, prices=open_prices, at=now)
        quotes = {
            ticker: Quote(ticker, float(price), float(price), now)
            for ticker, price in open_prices.items()
        }
        oms = OrderManagementSystem(
            self.config,
            InMemoryPaperBroker(account, quotes),
            repository=self.execution,
        )
        intents = self.execution.list_intents(paper_cycle_id=stored.paper_cycle_id)
        intents = tuple(sorted(intents, key=lambda item: item.side == Side.BUY))
        settlement_date = str(
            self.calendar.next_session(pd.Timestamp(stored.decision.next_rebalance_session)).date()
        )
        for intent in intents:
            if intent.state == OrderState.FILLED:
                continue
            try:
                if intent.state == OrderState.APPROVED:
                    intent = oms.submit(intent.client_order_id)
                if intent.state not in {OrderState.SUBMITTED, OrderState.PARTIAL}:
                    continue
                if intent.ticker not in open_prices:
                    raise ValueError(f"Missing open for order {intent.ticker}.")
                quantity = intent.remaining_quantity
                raw_open = float(open_prices[intent.ticker])
                directional_bps = self.config.slippage_bps + intent.estimated_impact_bps
                direction = 1.0 if intent.side == Side.BUY else -1.0
                price = raw_open * (1.0 + direction * directional_bps / 10_000.0)
                commission = quantity * price * self.config.trading_cost_bps / 10_000.0
                execution_key = sha256(
                    f"{intent.client_order_id}|{quantity:.12f}|{price:.12f}".encode("utf-8")
                ).hexdigest()
                fill = ExecutionFill(
                    client_order_id=intent.client_order_id,
                    broker_execution_id=f"replay-{execution_key}",
                    filled_at=now.astimezone(timezone.utc),
                    quantity=quantity,
                    price=price,
                    commission=commission,
                    implementation_shortfall_bps=directional_bps,
                    settlement_date=settlement_date,
                )
                oms.record_fill(
                    intent.client_order_id,
                    fill,
                    account_ref=self.account_ref,
                )
            except Exception as exc:
                self._record_incident(
                    stored,
                    code="PAPER_FILL_FAILED",
                    severity="HIGH",
                    order_intent_id=self.execution.get_intent_id(intent.client_order_id),
                    details={"ticker": intent.ticker, "error": str(exc)},
                )
                self.flush_notifications()
                raise
        self.signals.transition_cycle(
            stored.paper_cycle_id,
            PaperCycleStatus.FILLED,
            expected=(PaperCycleStatus.APPROVED, PaperCycleStatus.FILLED),
            at=now,
        )
        account, _trigger, _trigger_value, _incident_id = self.execution.mark_account(
            self.account_ref,
            prices=open_prices,
            valuation_session=stored.decision.next_rebalance_session,
            at=now,
            drawdown_limit=self.config.portfolio_drawdown_stop,
            daily_loss_limit=self.config.daily_loss_halt,
            paper_cycle_id=stored.paper_cycle_id,
        )
        account, reconciliation_id, reconciliation_matched = self._reconcile_cycle(
            stored, mark_prices=open_prices, at=now
        )
        if account.risk_state not in {"NORMAL", "WARNING", "DRIFT_REVIEW"}:
            halted = self._halt_cycle(stored, account=account, prices=open_prices, at=now)
            return PaperCycleResult(
                decision_id=halted.decision_id,
                paper_cycle_id=halted.paper_cycle_id,
                status=halted.status,
                account=halted.account,
                order_states=halted.order_states,
                reconciliation_id=reconciliation_id,
            )
        if not reconciliation_matched:
            account = self.execution.set_account_risk_state(
                self.account_ref, "RECONCILIATION_HALTED", at=now
            )
            halted = self._halt_cycle(stored, account=account, prices=open_prices, at=now)
            return PaperCycleResult(
                decision_id=halted.decision_id,
                paper_cycle_id=halted.paper_cycle_id,
                status=halted.status,
                account=halted.account,
                order_states=halted.order_states,
                reconciliation_id=reconciliation_id,
            )
        else:
            self.signals.transition_cycle(
                stored.paper_cycle_id,
                PaperCycleStatus.RECONCILED,
                expected=(PaperCycleStatus.FILLED, PaperCycleStatus.RECONCILED),
                at=now,
            )
            self.signals.transition_cycle(
                stored.paper_cycle_id,
                PaperCycleStatus.COMPLETED,
                expected=(PaperCycleStatus.RECONCILED, PaperCycleStatus.COMPLETED),
                at=now,
            )
            self.signals.mark_executed(decision_id, at=now)
            status = PaperCycleStatus.COMPLETED
        self.flush_notifications()
        final_intents = self.execution.list_intents(paper_cycle_id=stored.paper_cycle_id)
        return PaperCycleResult(
            decision_id=decision_id,
            paper_cycle_id=stored.paper_cycle_id,
            status=status,
            account=account,
            order_states={item.client_order_id: item.state.value for item in final_intents},
            reconciliation_id=reconciliation_id,
        )

    def settle(self, *, session: str, at: datetime | None = None) -> AccountSnapshot:
        return self.execution.settle_due(
            self.account_ref, session=session, settled_at=at
        )

    def reconcile_halted_account(
        self,
        decision_id: int,
        *,
        prices: Mapping[str, float],
        at: datetime,
    ) -> PaperCycleResult:
        stored = self.signals.get_stored_decision(decision_id)
        if stored.cycle_status != PaperCycleStatus.HALTED:
            raise ValueError("Halt reconciliation requires a HALTED paper cycle.")
        account = self.execution.get_account(self.account_ref)
        if account.risk_state not in {"DRAWDOWN_HALTED", "DAILY_LOSS_HALTED"}:
            raise ValueError("Account has no recoverable risk halt.")
        now = _aware(at)
        account, _trigger, _trigger_value, _incident_id = self.execution.mark_account(
            self.account_ref,
            prices=prices,
            valuation_session=now.astimezone(NEW_YORK).date().isoformat(),
            at=now,
            drawdown_limit=self.config.portfolio_drawdown_stop,
            daily_loss_limit=self.config.daily_loss_halt,
            paper_cycle_id=stored.paper_cycle_id,
        )
        order_type = (
            "REPLAY_OPEN_LIQUIDATION"
            if account.risk_state == "DRAWDOWN_HALTED"
            else None
        )
        account, reconciliation_id, _matched = self._reconcile_cycle(
            stored,
            mark_prices=prices,
            at=now,
            order_type=order_type,
        )
        self.flush_notifications()
        intents = self.execution.list_intents(paper_cycle_id=stored.paper_cycle_id)
        return PaperCycleResult(
            decision_id=decision_id,
            paper_cycle_id=stored.paper_cycle_id,
            status=PaperCycleStatus.HALTED,
            account=account,
            order_states={
                intent.client_order_id: intent.state.value for intent in intents
            },
            reconciliation_id=reconciliation_id,
        )

    def authorize_risk_recovery(
        self,
        *,
        reconciliation_id: int,
        authorized_by: str,
        note: str,
        at: datetime,
    ) -> AccountSnapshot:
        return self.execution.authorize_risk_recovery(
            account_ref=self.account_ref,
            reconciliation_id=reconciliation_id,
            authorized_by=authorized_by,
            note=note,
            at=at,
        )

    def value_account(
        self,
        *,
        prices: Mapping[str, float],
        valuation_session: str,
        at: datetime,
        decision_id: int | None = None,
    ) -> AccountSnapshot:
        stored = (
            self.signals.get_stored_decision(decision_id)
            if decision_id is not None
            else None
        )
        account, _trigger, _trigger_value, incident_id = self.execution.mark_account(
            self.account_ref,
            prices=prices,
            valuation_session=valuation_session,
            at=at,
            drawdown_limit=self.config.portfolio_drawdown_stop,
            daily_loss_limit=self.config.daily_loss_halt,
            paper_cycle_id=(stored.paper_cycle_id if stored is not None else None),
        )
        if incident_id is not None:
            self.flush_notifications()
        if stored is not None and account.risk_state not in {
            "NORMAL",
            "WARNING",
            "DRIFT_REVIEW",
        }:
            self._halt_cycle(stored, account=account, prices=prices, at=at)
        return account
