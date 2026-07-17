from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Mapping

from sqlalchemy import select

from execution.models import ExecutionFill, OrderIntent, ReconciliationResult
from storage.repositories.base import BaseRepository, upsert
from storage.schema import (
    execution_fills,
    order_intents,
    reconciliations,
    risk_incidents,
)


class ExecutionRepository(BaseRepository):
    """Environment-scoped execution journal.

    A repository instance is pinned to one environment so paper and live rows
    cannot be accidentally written through the same runtime object.
    """

    def __init__(self, *args: object, environment: str = "PAPER", **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.environment = environment.upper()
        if self.environment not in {"RESEARCH", "PAPER", "LIVE"}:
            raise ValueError("Unknown execution environment.")

    def save_intent(self, intent: OrderIntent) -> int:
        if intent.environment.value != self.environment:
            raise ValueError("Execution repository environment mismatch.")
        row = {
            "client_order_id": intent.client_order_id,
            "environment": intent.environment.value,
            "strategy_version": intent.strategy_version,
            "created_at": intent.created_at or datetime.now(timezone.utc),
            "approved_at": intent.approved_at,
            "approved_by": intent.approved_by,
            "status": intent.state.value,
            "ticker": intent.ticker,
            "side": intent.side.value,
            "quantity": intent.quantity,
            "limit_price": intent.limit_price,
            "arrival_bid": intent.arrival_quote.bid,
            "arrival_ask": intent.arrival_quote.ask,
            "arrival_mid": intent.arrival_quote.mid,
            "adv_fraction": intent.adv_fraction,
            "broker_order_id": intent.broker_order_id,
            "submitted_at": intent.submitted_at,
            "metadata_json": {
                "signal_session": intent.signal_session,
                "estimated_impact_bps": intent.estimated_impact_bps,
                "notes": list(intent.notes),
            },
        }
        with self.engine.begin() as conn:
            upsert(
                conn,
                order_intents,
                [row],
                index_elements=["environment", "client_order_id"],
                update_columns=[
                    "approved_at", "approved_by", "status", "limit_price",
                    "broker_order_id", "submitted_at", "metadata_json",
                ],
            )
            return int(
                conn.execute(
                    select(order_intents.c.id).where(
                        order_intents.c.client_order_id == intent.client_order_id,
                        order_intents.c.environment == self.environment,
                    )
                ).scalar_one()
            )

    def save_fill(self, order_intent_id: int, fill: ExecutionFill) -> None:
        with self.engine.begin() as conn:
            order_environment = conn.execute(
                select(order_intents.c.environment).where(
                    order_intents.c.id == order_intent_id
                )
            ).scalar_one_or_none()
            if order_environment != self.environment:
                raise ValueError(
                    "Fill order does not belong to this execution environment."
                )
            existing = conn.execute(
                select(execution_fills.c.id).where(
                    execution_fills.c.environment == self.environment,
                    execution_fills.c.broker_execution_id
                    == fill.broker_execution_id,
                )
            ).scalar_one_or_none()
            if existing is None:
                conn.execute(
                    execution_fills.insert().values(**{
                    "order_intent_id": order_intent_id,
                    "environment": self.environment,
                    "filled_at": fill.filled_at,
                    "quantity": fill.quantity,
                    "price": fill.price,
                    "commission": fill.commission,
                    "implementation_shortfall_bps": fill.implementation_shortfall_bps,
                    "broker_execution_id": fill.broker_execution_id,
                    })
                )

    def save_reconciliation(
        self,
        *,
        account_ref: str,
        nav: float,
        result: ReconciliationResult,
    ) -> int:
        with self.engine.begin() as conn:
            inserted = conn.execute(
                reconciliations.insert().values(
                    environment=self.environment,
                    account_ref=account_ref,
                    status="matched" if result.matched else "locked",
                    nav=nav,
                    difference_value=result.difference_value,
                    details_json=asdict(result),
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
    ) -> int:
        with self.engine.begin() as conn:
            inserted = conn.execute(
                risk_incidents.insert().values(
                    strategy_version=strategy_version,
                    code=code,
                    severity=severity,
                    trigger_value=trigger_value,
                    details_json={**dict(details), "environment": self.environment},
                )
            )
            return int(inserted.inserted_primary_key[0])
