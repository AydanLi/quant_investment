from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Mapping, Sequence

from sqlalchemy import and_, func, select

from data.calendar import NEW_YORK, NyseCalendar
from execution.models import (
    AccountSnapshot,
    BrokerEnvironment,
    BrokerPosition,
    ExecutionFill,
    IncidentNotification,
    OrderIntent,
    OrderState,
    PendingSettlement,
    Quote,
    ReconciliationResult,
    Side,
)
from storage.repositories.base import BaseRepository
from storage.schema import (
    execution_fills,
    order_intents,
    paper_accounts,
    paper_cash_movements,
    reconciliations,
    risk_incidents,
)


TERMINAL_ORDER_STATES = {
    OrderState.FILLED.value,
    OrderState.CANCELED.value,
    OrderState.REJECTED.value,
    OrderState.MISSED.value,
}


def _utc_naive(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ExecutionRepository(BaseRepository):
    """SQLite-backed execution journal and local cash-account ledger."""

    def __init__(self, *args: object, environment: str = "PAPER", **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.environment = environment.upper()
        if self.environment not in {"RESEARCH", "PAPER", "LIVE"}:
            raise ValueError("Unknown execution environment.")

    @staticmethod
    def _intent_from_row(row: Mapping[str, object]) -> OrderIntent:
        metadata = dict(row.get("metadata_json") or {})
        captured = metadata.get("arrival_captured_at") or row.get("created_at")
        captured_at = _aware_utc(captured) if isinstance(captured, datetime) else datetime.now(timezone.utc)
        return OrderIntent(
            client_order_id=str(row["client_order_id"]),
            environment=BrokerEnvironment(str(row["environment"])),
            strategy_version=str(row["strategy_version"]),
            signal_session=str(metadata.get("signal_session") or ""),
            ticker=str(row["ticker"]),
            side=Side(str(row["side"])),
            quantity=float(row["quantity"]),
            limit_price=float(row["limit_price"]),
            arrival_quote=Quote(
                str(row["ticker"]),
                float(row.get("arrival_bid") or row["limit_price"]),
                float(row.get("arrival_ask") or row["limit_price"]),
                captured_at,
            ),
            adv_fraction=float(row.get("adv_fraction") or 0.0),
            estimated_impact_bps=float(metadata.get("estimated_impact_bps") or 0.0),
            signal_decision_id=(
                int(row["signal_decision_id"])
                if row.get("signal_decision_id") is not None
                else None
            ),
            paper_cycle_id=(
                int(row["paper_cycle_id"])
                if row.get("paper_cycle_id") is not None
                else None
            ),
            order_type=str(row.get("order_type") or "LMT"),
            execution_session=(
                str(row["execution_session"])
                if row.get("execution_session") is not None
                else None
            ),
            filled_quantity=float(row.get("filled_quantity") or 0.0),
            state=OrderState(str(row["status"])),
            created_at=(
                _aware_utc(row["created_at"])
                if isinstance(row.get("created_at"), datetime)
                else None
            ),
            approved_at=(
                _aware_utc(row["approved_at"])
                if isinstance(row.get("approved_at"), datetime)
                else None
            ),
            approved_by=(str(row["approved_by"]) if row.get("approved_by") else None),
            broker_order_id=(
                str(row["broker_order_id"]) if row.get("broker_order_id") else None
            ),
            submitted_at=(
                _aware_utc(row["submitted_at"])
                if isinstance(row.get("submitted_at"), datetime)
                else None
            ),
            notes=list(metadata.get("notes") or ()),
            account_before=(
                dict(metadata["account_before"])
                if isinstance(metadata.get("account_before"), Mapping)
                else None
            ),
        )

    def save_intent(self, intent: OrderIntent) -> int:
        if intent.environment.value != self.environment:
            raise ValueError("Execution repository environment mismatch.")
        row = {
            "client_order_id": intent.client_order_id,
            "environment": intent.environment.value,
            "strategy_version": intent.strategy_version,
            "signal_decision_id": intent.signal_decision_id,
            "paper_cycle_id": intent.paper_cycle_id,
            "created_at": _utc_naive(intent.created_at),
            "approved_at": _utc_naive(intent.approved_at) if intent.approved_at else None,
            "approved_by": intent.approved_by,
            "status": intent.state.value,
            "ticker": intent.ticker,
            "side": intent.side.value,
            "quantity": intent.quantity,
            "limit_price": intent.limit_price,
            "order_type": intent.order_type,
            "execution_session": intent.execution_session,
            "filled_quantity": intent.filled_quantity,
            "arrival_bid": intent.arrival_quote.bid,
            "arrival_ask": intent.arrival_quote.ask,
            "arrival_mid": intent.arrival_quote.mid,
            "adv_fraction": intent.adv_fraction,
            "broker_order_id": intent.broker_order_id,
            "submitted_at": _utc_naive(intent.submitted_at) if intent.submitted_at else None,
            "metadata_json": {
                "signal_session": intent.signal_session,
                "estimated_impact_bps": intent.estimated_impact_bps,
                "arrival_captured_at": intent.arrival_quote.captured_at.isoformat(),
                "notes": list(intent.notes),
                "account_before": (
                    dict(intent.account_before) if intent.account_before is not None else None
                ),
            },
        }
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(order_intents).where(
                    order_intents.c.environment == self.environment,
                    order_intents.c.client_order_id == intent.client_order_id,
                )
            ).mappings().one_or_none()
            if existing is not None:
                immutable = (
                    "strategy_version",
                    "signal_decision_id",
                    "paper_cycle_id",
                    "ticker",
                    "side",
                    "quantity",
                    "order_type",
                    "execution_session",
                )
                if any(existing[name] != row[name] for name in immutable):
                    raise ValueError("Existing OrderIntent has different immutable fields.")
                return int(existing["id"])
            inserted = conn.execute(order_intents.insert().values(**row))
            return int(inserted.inserted_primary_key[0])

    def get_intent(self, client_order_id: str) -> OrderIntent:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(order_intents).where(
                    order_intents.c.environment == self.environment,
                    order_intents.c.client_order_id == client_order_id,
                )
            ).mappings().one_or_none()
        if row is None:
            raise KeyError(client_order_id)
        return self._intent_from_row(row)

    def get_intent_id(self, client_order_id: str) -> int:
        with self.engine.connect() as conn:
            value = conn.execute(
                select(order_intents.c.id).where(
                    order_intents.c.environment == self.environment,
                    order_intents.c.client_order_id == client_order_id,
                )
            ).scalar_one_or_none()
        if value is None:
            raise KeyError(client_order_id)
        return int(value)

    def list_intents(
        self,
        *,
        paper_cycle_id: int | None = None,
        nonterminal_only: bool = False,
    ) -> tuple[OrderIntent, ...]:
        stmt = select(order_intents).where(order_intents.c.environment == self.environment)
        if paper_cycle_id is not None:
            stmt = stmt.where(order_intents.c.paper_cycle_id == paper_cycle_id)
        if nonterminal_only:
            stmt = stmt.where(order_intents.c.status.not_in(TERMINAL_ORDER_STATES))
        stmt = stmt.order_by(order_intents.c.id)
        with self.engine.connect() as conn:
            return tuple(self._intent_from_row(row) for row in conn.execute(stmt).mappings())

    def transition_intent(
        self,
        client_order_id: str,
        *,
        expected: Sequence[OrderState],
        target: OrderState,
        values: Mapping[str, object] | None = None,
    ) -> OrderIntent:
        updates = dict(values or {})
        updates["status"] = target.value
        for name in ("approved_at", "submitted_at"):
            if isinstance(updates.get(name), datetime):
                updates[name] = _utc_naive(updates[name])
        with self.engine.begin() as conn:
            current = conn.execute(
                select(order_intents.c.status).where(
                    order_intents.c.environment == self.environment,
                    order_intents.c.client_order_id == client_order_id,
                )
            ).scalar_one_or_none()
            if current is None:
                raise KeyError(client_order_id)
            if current == target.value:
                pass
            elif current not in {state.value for state in expected}:
                raise ValueError(f"Invalid OMS transition {current} -> {target.value}.")
            else:
                result = conn.execute(
                    order_intents.update()
                    .where(
                        order_intents.c.environment == self.environment,
                        order_intents.c.client_order_id == client_order_id,
                        order_intents.c.status == current,
                    )
                    .values(**updates)
                )
                if result.rowcount != 1:
                    raise RuntimeError("Concurrent OrderIntent state change detected.")
        return self.get_intent(client_order_id)

    def mark_cycle_orders_missed(self, paper_cycle_id: int) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                order_intents.update()
                .where(
                    order_intents.c.environment == self.environment,
                    order_intents.c.paper_cycle_id == paper_cycle_id,
                    order_intents.c.status.in_(
                        [OrderState.DRAFT.value, OrderState.APPROVED.value]
                    ),
                )
                .values(status=OrderState.MISSED.value)
            )

    def save_fill(self, order_intent_id: int, fill: ExecutionFill) -> int:
        """Journal a fill idempotently and derive PARTIAL/FILLED from quantity."""
        with self.engine.begin() as conn:
            order = conn.execute(
                select(order_intents).where(order_intents.c.id == order_intent_id)
            ).mappings().one_or_none()
            if order is None or order["environment"] != self.environment:
                raise ValueError("Fill order does not belong to this execution environment.")
            existing = conn.execute(
                select(execution_fills.c.id).where(
                    execution_fills.c.environment == self.environment,
                    execution_fills.c.broker_execution_id == fill.broker_execution_id,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return int(existing)
            if order["signal_decision_id"] is not None and order["status"] not in {
                OrderState.SUBMITTED.value,
                OrderState.PARTIAL.value,
            }:
                raise ValueError("A fill requires a SUBMITTED or PARTIAL order.")
            prior = float(order["filled_quantity"] or 0.0)
            cumulative = prior + float(fill.quantity)
            if fill.quantity <= 0.0 or cumulative > float(order["quantity"]) + 1e-8:
                raise ValueError("Fill quantity is non-positive or exceeds the order quantity.")
            inserted = conn.execute(
                execution_fills.insert().values(
                    order_intent_id=order_intent_id,
                    environment=self.environment,
                    filled_at=_utc_naive(fill.filled_at),
                    quantity=fill.quantity,
                    price=fill.price,
                    commission=fill.commission,
                    implementation_shortfall_bps=fill.implementation_shortfall_bps,
                    broker_execution_id=fill.broker_execution_id,
                    settlement_date=fill.settlement_date,
                )
            )
            target = (
                OrderState.FILLED
                if cumulative >= float(order["quantity"]) - 1e-8
                else OrderState.PARTIAL
            )
            conn.execute(
                order_intents.update()
                .where(order_intents.c.id == order_intent_id)
                .values(filled_quantity=cumulative, status=target.value)
            )
            return int(inserted.inserted_primary_key[0])

    def initialize_account(
        self,
        *,
        account_ref: str,
        strategy_version: str,
        initial_cash: float,
        at: datetime | None = None,
    ) -> AccountSnapshot:
        if initial_cash <= 0.0:
            raise ValueError("Initial paper cash must be positive.")
        now = _utc_naive(at)
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(paper_accounts).where(
                    paper_accounts.c.environment == self.environment,
                    paper_accounts.c.account_ref == account_ref,
                )
            ).mappings().one_or_none()
            if existing is None:
                conn.execute(
                    paper_accounts.insert().values(
                        environment=self.environment,
                        account_ref=account_ref,
                        strategy_version=strategy_version,
                        nav=initial_cash,
                        settled_cash=initial_cash,
                        available_cash=initial_cash,
                        positions_json={"_meta": {"total_commission": 0.0}},
                        high_water=initial_cash,
                        risk_state="NORMAL",
                        version=1,
                        updated_at=now,
                    )
                )
            elif existing["strategy_version"] != strategy_version:
                raise ValueError("Paper account strategy version is immutable.")
        return self.get_account(account_ref)

    @staticmethod
    def _decode_positions(payload: Mapping[str, object]) -> dict[str, dict[str, float]]:
        positions: dict[str, dict[str, float]] = {}
        for ticker, raw in dict(payload or {}).items():
            item = dict(raw) if isinstance(raw, Mapping) else {"quantity": raw}
            quantity = float(item.get("quantity") or 0.0)
            if abs(quantity) < 1e-12:
                continue
            positions[str(ticker)] = {
                "quantity": quantity,
                "mark_price": float(item.get("mark_price") or 0.0),
            }
        return positions

    @staticmethod
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
            "pending_settlements": [asdict(item) for item in account.pending_settlements],
            "high_water": account.high_water,
            "risk_state": account.risk_state,
            "total_commission": account.total_commission,
            "last_valuation_session": account.last_valuation_session,
        }

    def save_cycle_baseline(
        self,
        *,
        account: AccountSnapshot,
        paper_cycle_id: int,
    ) -> int:
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(reconciliations.c.id).where(
                    reconciliations.c.environment == self.environment,
                    reconciliations.c.account_ref == account.account_ref,
                    reconciliations.c.paper_cycle_id == paper_cycle_id,
                    reconciliations.c.status == "baseline",
                )
            ).scalar_one_or_none()
            if existing is not None:
                return int(existing)
            inserted = conn.execute(
                reconciliations.insert().values(
                    environment=self.environment,
                    account_ref=account.account_ref,
                    paper_cycle_id=paper_cycle_id,
                    status="baseline",
                    nav=account.nav,
                    difference_value=0.0,
                    details_json={"account_before": self._account_payload(account)},
                )
            )
            return int(inserted.inserted_primary_key[0])

    def _total_commission(self, conn, strategy_version: str) -> float:  # noqa: ANN001
        value = conn.execute(
            select(func.coalesce(func.sum(execution_fills.c.commission), 0.0))
            .select_from(
                execution_fills.join(
                    order_intents,
                    execution_fills.c.order_intent_id == order_intents.c.id,
                )
            )
            .where(
                execution_fills.c.environment == self.environment,
                order_intents.c.strategy_version == strategy_version,
            )
        ).scalar_one()
        return float(value or 0.0)

    def get_account(self, account_ref: str) -> AccountSnapshot:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(paper_accounts).where(
                    paper_accounts.c.environment == self.environment,
                    paper_accounts.c.account_ref == account_ref,
                )
            ).mappings().one_or_none()
            if row is None:
                raise KeyError(f"Unknown paper account {account_ref}.")
            movement_rows = tuple(
                conn.execute(
                    select(paper_cash_movements)
                    .where(
                        paper_cash_movements.c.account_id == row["id"],
                        paper_cash_movements.c.status == "PENDING",
                    )
                    .order_by(paper_cash_movements.c.id)
                ).mappings()
            )
            raw_positions = dict(row["positions_json"] or {})
            account_metadata = dict(raw_positions.get("_meta") or {})
            commission = float(
                account_metadata.get("total_commission")
                if account_metadata.get("total_commission") is not None
                else self._total_commission(conn, str(row["strategy_version"]))
            )
        decoded = self._decode_positions(row["positions_json"])
        positions = {
            ticker: BrokerPosition(
                ticker=ticker,
                quantity=item["quantity"],
                market_value=item["quantity"] * item["mark_price"],
            )
            for ticker, item in decoded.items()
        }
        pending = tuple(
            PendingSettlement(
                movement_key=str(item["movement_key"]),
                amount=float(item["amount"]),
                trade_date=str(item["trade_date"]),
                settlement_date=str(item["settlement_date"]),
                status=str(item["status"]),
            )
            for item in movement_rows
        )
        return AccountSnapshot(
            account_ref=account_ref,
            nav=float(row["nav"]),
            settled_cash=float(row["settled_cash"]),
            available_cash=float(row["available_cash"]),
            buying_power=float(row["available_cash"]),
            positions=positions,
            captured_at=_aware_utc(row["updated_at"]),
            unsettled_cash=sum(item.amount for item in pending),
            pending_settlements=pending,
            high_water=float(row["high_water"]),
            risk_state=str(row["risk_state"]),
            total_commission=commission,
            last_valuation_session=(
                str(row["last_valuation_session"])
                if row["last_valuation_session"] is not None
                else None
            ),
            version=int(row["version"]),
        )

    def get_account_strategy_version(self, account_ref: str) -> str:
        with self.engine.connect() as conn:
            value = conn.execute(
                select(paper_accounts.c.strategy_version).where(
                    paper_accounts.c.environment == self.environment,
                    paper_accounts.c.account_ref == account_ref,
                )
            ).scalar_one_or_none()
        if value is None:
            raise KeyError(account_ref)
        return str(value)

    def set_account_risk_state(
        self,
        account_ref: str,
        risk_state: str,
        *,
        at: datetime | None = None,
    ) -> AccountSnapshot:
        with self.engine.begin() as conn:
            result = conn.execute(
                paper_accounts.update()
                .where(
                    paper_accounts.c.environment == self.environment,
                    paper_accounts.c.account_ref == account_ref,
                )
                .values(risk_state=risk_state, updated_at=_utc_naive(at))
            )
            if result.rowcount != 1:
                raise KeyError(account_ref)
        return self.get_account(account_ref)

    def expected_account_from_cycle(
        self,
        *,
        account_ref: str,
        paper_cycle_id: int,
        mark_prices: Mapping[str, float],
        at: datetime,
        order_type: str | None = None,
    ) -> AccountSnapshot:
        """Rebuild expected state from the immutable pre-cycle snapshot and journal."""
        intent_type_filter = (
            order_intents.c.order_type == order_type
            if order_type is not None
            else order_intents.c.order_type != "REPLAY_OPEN_LIQUIDATION"
        )
        with self.engine.connect() as conn:
            account_row = conn.execute(
                select(paper_accounts).where(
                    paper_accounts.c.environment == self.environment,
                    paper_accounts.c.account_ref == account_ref,
                )
            ).mappings().one_or_none()
            if account_row is None:
                raise KeyError(account_ref)
            intent_rows = tuple(
                conn.execute(
                    select(order_intents)
                    .where(
                        order_intents.c.environment == self.environment,
                        order_intents.c.paper_cycle_id == paper_cycle_id,
                        intent_type_filter,
                    )
                    .order_by(order_intents.c.id)
                ).mappings()
            )
            snapshots = [
                dict(row["metadata_json"] or {}).get("account_before")
                for row in intent_rows
                if dict(row["metadata_json"] or {}).get("account_before") is not None
            ]
            if not snapshots:
                baseline = conn.execute(
                    select(reconciliations.c.details_json)
                    .where(
                        reconciliations.c.environment == self.environment,
                        reconciliations.c.account_ref == account_ref,
                        reconciliations.c.paper_cycle_id == paper_cycle_id,
                        reconciliations.c.status == "baseline",
                    )
                    .order_by(reconciliations.c.id)
                    .limit(1)
                ).scalar_one_or_none()
                if baseline is not None and baseline.get("account_before") is not None:
                    snapshots.append(baseline["account_before"])
            if not snapshots:
                raise ValueError("Paper cycle has no immutable pre-cycle account snapshot.")
            first = snapshots[0]
            if any(snapshot != first for snapshot in snapshots[1:]):
                raise ValueError("Paper-cycle intents disagree on the pre-cycle account snapshot.")
            pre = dict(first)
            positions = self._decode_positions(pre.get("positions") or {})
            fills = tuple(
                conn.execute(
                    select(
                        execution_fills.c.id,
                        execution_fills.c.quantity,
                        execution_fills.c.price,
                        execution_fills.c.commission,
                        order_intents.c.ticker,
                        order_intents.c.side,
                    )
                    .select_from(
                        execution_fills.join(
                            order_intents,
                            execution_fills.c.order_intent_id == order_intents.c.id,
                        )
                    )
                    .where(
                        execution_fills.c.environment == self.environment,
                        order_intents.c.paper_cycle_id == paper_cycle_id,
                        intent_type_filter,
                    )
                    .order_by(execution_fills.c.id)
                ).mappings()
            )
            for fill in fills:
                ticker = str(fill["ticker"])
                current = positions.get(ticker, {"quantity": 0.0, "mark_price": 0.0})
                signed = float(fill["quantity"]) if fill["side"] == Side.BUY.value else -float(fill["quantity"])
                quantity = float(current["quantity"]) + signed
                if quantity < -1e-8:
                    raise ValueError("Fill journal reconstructs a short position.")
                if abs(quantity) < 1e-8:
                    positions.pop(ticker, None)
                else:
                    positions[ticker] = {"quantity": quantity, "mark_price": float(fill["price"])}
            missing = sorted(set(positions) - set(mark_prices))
            if missing:
                raise ValueError(f"Missing marks for expected positions: {', '.join(missing)}.")
            for ticker, item in positions.items():
                item["mark_price"] = float(mark_prices[ticker])
            movement_rows = {
                str(row["movement_key"]): row
                for row in conn.execute(
                    select(paper_cash_movements).where(
                        paper_cash_movements.c.account_id == account_row["id"]
                    )
                ).mappings()
            }
            relevant_movements: dict[str, Mapping[str, object]] = {}
            for raw in pre.get("pending_settlements") or ():
                item = dict(raw)
                key = str(item["movement_key"])
                relevant_movements[key] = movement_rows.get(key, item)
            order_ids = {int(row["id"]) for row in intent_rows}
            for key, row in movement_rows.items():
                if row["order_intent_id"] is not None and int(row["order_intent_id"]) in order_ids:
                    relevant_movements[key] = row
            settled_cash = float(pre["settled_cash"])
            pending: list[PendingSettlement] = []
            for key, movement in relevant_movements.items():
                amount = float(movement["amount"])
                status = str(movement.get("status") or "PENDING")
                if status == "SETTLED":
                    settled_cash += amount
                else:
                    pending.append(
                        PendingSettlement(
                            movement_key=key,
                            amount=amount,
                            trade_date=str(movement["trade_date"]),
                            settlement_date=str(movement["settlement_date"]),
                            status=status,
                        )
                    )
            cycle_commission = sum(float(row["commission"] or 0.0) for row in fills)
        unsettled_cash = sum(item.amount for item in pending)
        broker_positions = {
            ticker: BrokerPosition(
                ticker=ticker,
                quantity=item["quantity"],
                market_value=item["quantity"] * item["mark_price"],
            )
            for ticker, item in positions.items()
        }
        nav = settled_cash + unsettled_cash + sum(
            position.market_value for position in broker_positions.values()
        )
        return AccountSnapshot(
            account_ref=account_ref,
            nav=nav,
            settled_cash=settled_cash,
            available_cash=settled_cash + unsettled_cash,
            buying_power=settled_cash + unsettled_cash,
            positions=broker_positions,
            captured_at=_aware_utc(at),
            unsettled_cash=unsettled_cash,
            pending_settlements=tuple(pending),
            high_water=max(float(pre.get("high_water") or nav), nav),
            risk_state=str(pre.get("risk_state") or "NORMAL"),
            total_commission=float(pre.get("total_commission") or 0.0) + cycle_commission,
            last_valuation_session=(
                str(pre["last_valuation_session"])
                if pre.get("last_valuation_session") is not None
                else None
            ),
            version=int(account_row["version"]),
        )

    def cancel_cycle_orders(self, paper_cycle_id: int) -> tuple[str, ...]:
        with self.engine.begin() as conn:
            rows = tuple(
                conn.execute(
                    select(order_intents.c.client_order_id).where(
                        order_intents.c.environment == self.environment,
                        order_intents.c.paper_cycle_id == paper_cycle_id,
                        order_intents.c.order_type != "REPLAY_OPEN_LIQUIDATION",
                        order_intents.c.status.in_(
                            [
                                OrderState.DRAFT.value,
                                OrderState.APPROVED.value,
                                OrderState.SUBMITTED.value,
                                OrderState.PARTIAL.value,
                            ]
                        ),
                    )
                ).scalars()
            )
            if rows:
                conn.execute(
                    order_intents.update()
                    .where(
                        order_intents.c.environment == self.environment,
                        order_intents.c.paper_cycle_id == paper_cycle_id,
                        order_intents.c.order_type != "REPLAY_OPEN_LIQUIDATION",
                        order_intents.c.status.in_(
                            [
                                OrderState.DRAFT.value,
                                OrderState.APPROVED.value,
                                OrderState.SUBMITTED.value,
                                OrderState.PARTIAL.value,
                            ]
                        ),
                    )
                    .values(status=OrderState.CANCELED.value)
                )
        return tuple(str(value) for value in rows)

    def apply_paper_fill(
        self,
        *,
        account_ref: str,
        order_intent_id: int,
        fill: ExecutionFill,
    ) -> AccountSnapshot:
        if not fill.settlement_date:
            raise ValueError("Paper fills require a T+1 settlement date.")
        with self.engine.begin() as conn:
            order = conn.execute(
                select(order_intents).where(order_intents.c.id == order_intent_id)
            ).mappings().one_or_none()
            if order is None or order["environment"] != self.environment:
                raise ValueError("Fill order does not belong to this execution environment.")
            account = conn.execute(
                select(paper_accounts).where(
                    paper_accounts.c.environment == self.environment,
                    paper_accounts.c.account_ref == account_ref,
                )
            ).mappings().one_or_none()
            if account is None:
                raise KeyError(account_ref)
            existing = conn.execute(
                select(execution_fills.c.id).where(
                    execution_fills.c.environment == self.environment,
                    execution_fills.c.broker_execution_id == fill.broker_execution_id,
                )
            ).scalar_one_or_none()
            duplicate = existing is not None
            if duplicate:
                pass
            elif order["status"] not in {
                OrderState.SUBMITTED.value,
                OrderState.PARTIAL.value,
            }:
                raise ValueError("A paper fill requires a SUBMITTED or PARTIAL order.")
            else:
                cumulative = float(order["filled_quantity"] or 0.0) + fill.quantity
                if fill.quantity <= 0.0 or cumulative > float(order["quantity"]) + 1e-8:
                    raise ValueError("Fill quantity is non-positive or exceeds the order quantity.")
                raw_positions = dict(account["positions_json"] or {})
                account_metadata = dict(raw_positions.get("_meta") or {})
                positions = self._decode_positions(raw_positions)
                current = positions.get(str(order["ticker"]), {"quantity": 0.0, "mark_price": fill.price})
                signed_quantity = fill.quantity if order["side"] == Side.BUY.value else -fill.quantity
                new_quantity = float(current["quantity"]) + signed_quantity
                if new_quantity < -1e-8:
                    raise ValueError("Paper fill would create a short position.")
                if abs(new_quantity) < 1e-8:
                    positions.pop(str(order["ticker"]), None)
                else:
                    positions[str(order["ticker"])] = {
                        "quantity": new_quantity,
                        "mark_price": fill.price,
                    }
                cash_amount = fill.quantity * fill.price
                cash_amount = (
                    -(cash_amount + fill.commission)
                    if order["side"] == Side.BUY.value
                    else cash_amount - fill.commission
                )
                pending_before = float(
                    conn.execute(
                        select(func.coalesce(func.sum(paper_cash_movements.c.amount), 0.0)).where(
                            paper_cash_movements.c.account_id == account["id"],
                            paper_cash_movements.c.status == "PENDING",
                        )
                    ).scalar_one()
                    or 0.0
                )
                if float(account["settled_cash"]) + pending_before + cash_amount < -1e-6:
                    raise ValueError("Paper buy exceeds available cash after same-cycle sales.")
                inserted = conn.execute(
                    execution_fills.insert().values(
                        order_intent_id=order_intent_id,
                        environment=self.environment,
                        filled_at=_utc_naive(fill.filled_at),
                        quantity=fill.quantity,
                        price=fill.price,
                        commission=fill.commission,
                        implementation_shortfall_bps=fill.implementation_shortfall_bps,
                        broker_execution_id=fill.broker_execution_id,
                        settlement_date=fill.settlement_date,
                    )
                )
                conn.execute(
                    paper_cash_movements.insert().values(
                        movement_key=f"FILL:{self.environment}:{fill.broker_execution_id}",
                        account_id=account["id"],
                        order_intent_id=order_intent_id,
                        amount=cash_amount,
                        trade_date=fill.filled_at.date().isoformat(),
                        settlement_date=fill.settlement_date,
                        status="PENDING",
                        created_at=_utc_naive(fill.filled_at),
                    )
                )
                pending_after = pending_before + cash_amount
                market_value = sum(
                    item["quantity"] * item["mark_price"] for item in positions.values()
                )
                nav = float(account["settled_cash"]) + pending_after + market_value
                result = conn.execute(
                    paper_accounts.update()
                    .where(
                        paper_accounts.c.id == account["id"],
                        paper_accounts.c.version == account["version"],
                    )
                    .values(
                        nav=nav,
                        available_cash=float(account["settled_cash"]) + pending_after,
                        positions_json={
                            **positions,
                            "_meta": {
                                **account_metadata,
                                "total_commission": float(
                                    account_metadata.get("total_commission") or 0.0
                                )
                                + fill.commission,
                            },
                        },
                        version=int(account["version"]) + 1,
                        updated_at=_utc_naive(fill.filled_at),
                    )
                )
                if result.rowcount != 1:
                    raise RuntimeError("Concurrent paper-account update detected.")
                target = (
                    OrderState.FILLED
                    if cumulative >= float(order["quantity"]) - 1e-8
                    else OrderState.PARTIAL
                )
                conn.execute(
                    order_intents.update()
                    .where(order_intents.c.id == order_intent_id)
                    .values(filled_quantity=cumulative, status=target.value)
                )
                if int(inserted.inserted_primary_key[0]) <= 0:  # pragma: no cover
                    raise RuntimeError("Fill insert did not return an identifier.")
        return self.get_account(account_ref)

    def settle_due(
        self,
        account_ref: str,
        *,
        session: str,
        settled_at: datetime | None = None,
    ) -> AccountSnapshot:
        now = _utc_naive(settled_at)
        with self.engine.begin() as conn:
            account = conn.execute(
                select(paper_accounts).where(
                    paper_accounts.c.environment == self.environment,
                    paper_accounts.c.account_ref == account_ref,
                )
            ).mappings().one_or_none()
            if account is None:
                raise KeyError(account_ref)
            due = float(
                conn.execute(
                    select(func.coalesce(func.sum(paper_cash_movements.c.amount), 0.0)).where(
                        paper_cash_movements.c.account_id == account["id"],
                        paper_cash_movements.c.status == "PENDING",
                        paper_cash_movements.c.settlement_date <= session,
                    )
                ).scalar_one()
                or 0.0
            )
            if abs(due) > 0.0:
                conn.execute(
                    paper_cash_movements.update()
                    .where(
                        paper_cash_movements.c.account_id == account["id"],
                        paper_cash_movements.c.status == "PENDING",
                        paper_cash_movements.c.settlement_date <= session,
                    )
                    .values(status="SETTLED", settled_at=now)
                )
                remaining = float(
                    conn.execute(
                        select(func.coalesce(func.sum(paper_cash_movements.c.amount), 0.0)).where(
                            paper_cash_movements.c.account_id == account["id"],
                            paper_cash_movements.c.status == "PENDING",
                        )
                    ).scalar_one()
                    or 0.0
                )
                settled_cash = float(account["settled_cash"]) + due
                conn.execute(
                    paper_accounts.update()
                    .where(paper_accounts.c.id == account["id"])
                    .values(
                        settled_cash=settled_cash,
                        available_cash=settled_cash + remaining,
                        version=int(account["version"]) + 1,
                        updated_at=now,
                    )
                )
        return self.get_account(account_ref)

    def mark_account(
        self,
        account_ref: str,
        *,
        prices: Mapping[str, float],
        valuation_session: str,
        at: datetime,
        drawdown_limit: float,
        daily_loss_limit: float,
        paper_cycle_id: int | None = None,
    ) -> tuple[AccountSnapshot, str | None, float | None, int | None]:
        with self.engine.begin() as conn:
            account = conn.execute(
                select(paper_accounts).where(
                    paper_accounts.c.environment == self.environment,
                    paper_accounts.c.account_ref == account_ref,
                )
            ).mappings().one_or_none()
            if account is None:
                raise KeyError(account_ref)
            raw_positions = dict(account["positions_json"] or {})
            account_metadata = dict(raw_positions.get("_meta") or {})
            positions = self._decode_positions(raw_positions)
            missing = sorted(set(positions) - set(prices))
            if missing:
                raise ValueError(f"Missing marks for active positions: {', '.join(missing)}.")
            for ticker, item in positions.items():
                price = float(prices[ticker])
                if price <= 0.0:
                    raise ValueError(f"Invalid mark for {ticker}.")
                item["mark_price"] = price
            pending = float(
                conn.execute(
                    select(func.coalesce(func.sum(paper_cash_movements.c.amount), 0.0)).where(
                        paper_cash_movements.c.account_id == account["id"],
                        paper_cash_movements.c.status == "PENDING",
                    )
                ).scalar_one()
                or 0.0
            )
            nav = float(account["settled_cash"]) + pending + sum(
                item["quantity"] * item["mark_price"] for item in positions.values()
            )
            high_water = max(float(account["high_water"]), nav)
            drawdown = nav / high_water - 1.0 if high_water > 0.0 else -1.0
            daily_return = None
            if account["last_valuation_session"] not in {None, valuation_session}:
                prior_nav = float(account["nav"])
                daily_return = nav / prior_nav - 1.0 if prior_nav > 0.0 else -1.0
            risk_state = str(account["risk_state"])
            trigger = None
            trigger_value = None
            incident_id = None
            if risk_state not in {"DRAWDOWN_HALTED", "DAILY_LOSS_HALTED"}:
                if drawdown <= -drawdown_limit:
                    risk_state = "DRAWDOWN_HALTED"
                    trigger = risk_state
                    trigger_value = drawdown
                elif daily_return is not None and daily_return <= -daily_loss_limit:
                    risk_state = "DAILY_LOSS_HALTED"
                    trigger = risk_state
                    trigger_value = daily_return
            if trigger is not None:
                existing_incident = conn.execute(
                    select(risk_incidents.c.id).where(
                        risk_incidents.c.strategy_version == account["strategy_version"],
                        risk_incidents.c.environment == self.environment,
                        risk_incidents.c.account_ref == account_ref,
                        risk_incidents.c.paper_cycle_id.is_(paper_cycle_id)
                        if paper_cycle_id is None
                        else risk_incidents.c.paper_cycle_id == paper_cycle_id,
                        risk_incidents.c.code == trigger,
                        risk_incidents.c.status == "open",
                    )
                ).scalar_one_or_none()
                if existing_incident is None:
                    inserted_incident = conn.execute(
                        risk_incidents.insert().values(
                            created_at=_utc_naive(at),
                            strategy_version=account["strategy_version"],
                            environment=self.environment,
                            account_ref=account_ref,
                            paper_cycle_id=paper_cycle_id,
                            code=trigger,
                            severity="CRITICAL",
                            trigger_value=trigger_value,
                            details_json={
                                "nav": nav,
                                "high_water": high_water,
                                "valuation_session": valuation_session,
                                "triggered_at": _aware_utc(at).isoformat(),
                            },
                            notification_status="PENDING",
                            notification_attempts=0,
                        )
                    )
                    incident_id = int(inserted_incident.inserted_primary_key[0])
                else:
                    incident_id = int(existing_incident)
            conn.execute(
                paper_accounts.update()
                .where(paper_accounts.c.id == account["id"])
                .values(
                    nav=nav,
                    available_cash=float(account["settled_cash"]) + pending,
                    positions_json={**positions, "_meta": account_metadata},
                    high_water=high_water,
                    risk_state=risk_state,
                    last_valuation_session=valuation_session,
                    version=int(account["version"]) + 1,
                    updated_at=_utc_naive(at),
                )
            )
        return self.get_account(account_ref), trigger, trigger_value, incident_id

    def open_order_ids(self, *, paper_cycle_id: int | None = None) -> tuple[str, ...]:
        stmt = select(order_intents.c.client_order_id).where(
            order_intents.c.environment == self.environment,
            order_intents.c.status.in_(
                [OrderState.SUBMITTED.value, OrderState.PARTIAL.value]
            ),
        )
        if paper_cycle_id is not None:
            stmt = stmt.where(order_intents.c.paper_cycle_id == paper_cycle_id)
        with self.engine.connect() as conn:
            return tuple(str(value) for value in conn.execute(stmt).scalars())

    def save_reconciliation(
        self,
        *,
        account_ref: str,
        nav: float,
        result: ReconciliationResult,
        paper_cycle_id: int | None = None,
    ) -> int:
        details = asdict(result)
        if result.reconciled_at is not None:
            details["reconciled_at"] = result.reconciled_at.isoformat()
        with self.engine.begin() as conn:
            inserted = conn.execute(
                reconciliations.insert().values(
                    environment=self.environment,
                    account_ref=account_ref,
                    paper_cycle_id=paper_cycle_id,
                    status="matched" if result.matched else "locked",
                    nav=nav,
                    difference_value=result.difference_value,
                    details_json=details,
                )
            )
            return int(inserted.inserted_primary_key[0])

    def record_incident(
        self,
        *,
        strategy_version: str,
        code: str,
        severity: str,
        trigger_value: float | None,
        details: Mapping[str, object],
        account_ref: str | None = None,
        paper_cycle_id: int | None = None,
        reconciliation_id: int | None = None,
        order_intent_id: int | None = None,
    ) -> int:
        """Persist before notification; identical open incidents are reused."""
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(risk_incidents.c.id).where(
                    risk_incidents.c.strategy_version == strategy_version,
                    risk_incidents.c.environment == self.environment,
                    risk_incidents.c.account_ref.is_(account_ref)
                    if account_ref is None
                    else risk_incidents.c.account_ref == account_ref,
                    risk_incidents.c.paper_cycle_id.is_(paper_cycle_id)
                    if paper_cycle_id is None
                    else risk_incidents.c.paper_cycle_id == paper_cycle_id,
                    risk_incidents.c.order_intent_id.is_(order_intent_id)
                    if order_intent_id is None
                    else risk_incidents.c.order_intent_id == order_intent_id,
                    risk_incidents.c.code == code,
                    risk_incidents.c.status == "open",
                )
            ).scalar_one_or_none()
            if existing is not None:
                return int(existing)
            inserted = conn.execute(
                risk_incidents.insert().values(
                    strategy_version=strategy_version,
                    environment=self.environment,
                    account_ref=account_ref,
                    paper_cycle_id=paper_cycle_id,
                    reconciliation_id=reconciliation_id,
                    order_intent_id=order_intent_id,
                    code=code,
                    severity=severity,
                    trigger_value=trigger_value,
                    details_json=dict(details),
                    notification_status="PENDING",
                    notification_attempts=0,
                )
            )
            return int(inserted.inserted_primary_key[0])

    def pending_notifications(self, *, limit: int = 20) -> tuple[IncidentNotification, ...]:
        with self.engine.connect() as conn:
            rows = tuple(
                conn.execute(
                    select(risk_incidents)
                    .where(
                        risk_incidents.c.environment == self.environment,
                        risk_incidents.c.notification_status.in_(["PENDING", "FAILED"]),
                    )
                    .order_by(risk_incidents.c.id)
                    .limit(limit)
                ).mappings()
            )
        return tuple(
            IncidentNotification(
                incident_id=int(row["id"]),
                code=str(row["code"]),
                severity=str(row["severity"]),
                strategy_version=str(row["strategy_version"]),
                account_ref=(str(row["account_ref"]) if row["account_ref"] else None),
                details=dict(row["details_json"] or {}),
                attempts=int(row["notification_attempts"] or 0),
            )
            for row in rows
        )

    def mark_notification_sent(self, incident_id: int, request_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                risk_incidents.update()
                .where(
                    risk_incidents.c.id == incident_id,
                    risk_incidents.c.notification_status.in_(["PENDING", "FAILED"]),
                )
                .values(
                    notification_status="SENT",
                    notification_request_id=request_id,
                    notification_attempts=risk_incidents.c.notification_attempts + 1,
                    notification_last_error=None,
                )
            )

    def mark_notification_failed(self, incident_id: int, error: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                risk_incidents.update()
                .where(
                    risk_incidents.c.id == incident_id,
                    risk_incidents.c.notification_status.in_(["PENDING", "FAILED"]),
                )
                .values(
                    notification_status="FAILED",
                    notification_attempts=risk_incidents.c.notification_attempts + 1,
                    notification_last_error=error[:500],
                )
            )

    def authorize_risk_recovery(
        self,
        *,
        account_ref: str,
        reconciliation_id: int,
        authorized_by: str,
        note: str,
        at: datetime | None = None,
    ) -> AccountSnapshot:
        if not authorized_by.strip() or not note.strip():
            raise ValueError("Risk recovery requires an operator and explanation.")
        effective_at = at or datetime.now(timezone.utc)
        if effective_at.tzinfo is None or effective_at.utcoffset() is None:
            raise ValueError("Risk recovery timestamp must include a UTC offset.")
        now = _utc_naive(effective_at)
        calendar = NyseCalendar()
        with self.engine.begin() as conn:
            account = conn.execute(
                select(paper_accounts).where(
                    paper_accounts.c.environment == self.environment,
                    paper_accounts.c.account_ref == account_ref,
                )
            ).mappings().one_or_none()
            if account is None:
                raise KeyError(account_ref)
            risk_state = str(account["risk_state"])
            if risk_state not in {"DRAWDOWN_HALTED", "DAILY_LOSS_HALTED"}:
                raise ValueError("Only drawdown or daily-loss halts may be recovered.")
            incident = conn.execute(
                select(risk_incidents)
                .where(
                    risk_incidents.c.environment == self.environment,
                    risk_incidents.c.account_ref == account_ref,
                    risk_incidents.c.strategy_version == account["strategy_version"],
                    risk_incidents.c.code == risk_state,
                    risk_incidents.c.status == "open",
                )
                .order_by(risk_incidents.c.id.desc())
                .limit(1)
            ).mappings().one_or_none()
            if incident is None:
                raise ValueError("Risk recovery requires the persisted trigger incident.")
            trigger_session = str(
                dict(incident["details_json"] or {}).get("valuation_session") or ""
            )
            if not trigger_session:
                raise ValueError("Risk incident is missing its trigger session.")
            if risk_state == "DRAWDOWN_HALTED":
                earliest = calendar.next_month_end_session(trigger_session).date()
                if self._decode_positions(account["positions_json"]):
                    raise ValueError("Drawdown recovery requires a fully liquidated account.")
            else:
                earliest = calendar.next_session(trigger_session).date()
            if effective_at.astimezone(NEW_YORK).date() < earliest:
                raise ValueError(
                    f"Risk recovery is not eligible before {earliest.isoformat()}."
                )
            unfinished = tuple(
                conn.execute(
                    select(order_intents.c.client_order_id).where(
                        order_intents.c.environment == self.environment,
                        order_intents.c.strategy_version == account["strategy_version"],
                        order_intents.c.status.in_(
                            [
                                OrderState.DRAFT.value,
                                OrderState.APPROVED.value,
                                OrderState.SUBMITTED.value,
                                OrderState.PARTIAL.value,
                            ]
                        ),
                    )
                ).scalars()
            )
            if unfinished:
                raise ValueError("Risk recovery requires no unfinished orders.")
            reconciliation = conn.execute(
                select(reconciliations).where(
                    reconciliations.c.id == reconciliation_id,
                    reconciliations.c.environment == self.environment,
                    reconciliations.c.account_ref == account_ref,
                    reconciliations.c.status == "matched",
                )
            ).mappings().one_or_none()
            if reconciliation is None:
                raise ValueError("Risk recovery requires a matched reconciliation.")
            if (
                incident["paper_cycle_id"] is not None
                and reconciliation["paper_cycle_id"] != incident["paper_cycle_id"]
            ):
                raise ValueError("Risk recovery reconciliation belongs to another cycle.")
            reconciled_raw = dict(reconciliation["details_json"] or {}).get(
                "reconciled_at"
            )
            try:
                reconciled_at = datetime.fromisoformat(str(reconciled_raw))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "Risk recovery reconciliation lacks a trustworthy timestamp."
                ) from exc
            if reconciled_at.tzinfo is None or reconciled_at.utcoffset() is None:
                raise ValueError("Risk recovery reconciliation timestamp must be timezone-aware.")
            if effective_at.astimezone(timezone.utc) < reconciled_at.astimezone(
                timezone.utc
            ):
                raise ValueError("Risk recovery cannot predate its reconciliation.")
            if reconciled_at.astimezone(timezone.utc) < _aware_utc(account["updated_at"]):
                raise ValueError("Risk recovery requires reconciliation of the latest account state.")
            account_updates: dict[str, object] = {
                "risk_state": "NORMAL",
                "updated_at": now,
            }
            if risk_state == "DRAWDOWN_HALTED":
                account_updates["high_water"] = float(account["nav"])
            result = conn.execute(
                paper_accounts.update()
                .where(
                    paper_accounts.c.id == account["id"],
                    paper_accounts.c.risk_state == risk_state,
                )
                .values(**account_updates)
            )
            if result.rowcount != 1:
                raise RuntimeError("Concurrent risk-state change detected.")
            conn.execute(
                risk_incidents.update()
                .where(
                    risk_incidents.c.id == incident["id"],
                    risk_incidents.c.status == "open",
                )
                .values(
                    status="resolved",
                    resolved_at=now,
                    reconciliation_id=reconciliation_id,
                    resolution_note=note.strip(),
                    recovery_authorized_by=authorized_by.strip(),
                    recovery_authorized_at=now,
                )
            )
        return self.get_account(account_ref)
