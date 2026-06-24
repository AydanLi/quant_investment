"""Rebalance orders emitted by the (mock) broker during a backtest."""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from storage.repositories.base import BaseRepository
from storage.schema import orders


def _opt_float(value: Any) -> Optional[float]:
    return float(value) if value is not None and pd.notna(value) else None


class OrderRepository(BaseRepository):
    def save(self, run_id: int, order_df: pd.DataFrame) -> None:
        """Persist the broker order log. Expects columns
        ``date, ticker, side, weight_change`` (``price``/``est_cost`` optional)."""
        if order_df.empty:
            return

        rows = []
        for _, row in order_df.iterrows():
            rows.append(
                {
                    "run_id": run_id,
                    "order_date": str(row.get("date")),
                    "ticker": row.get("ticker"),
                    "side": row.get("side"),
                    "weight_change": _opt_float(row.get("weight_change")),
                    "price": _opt_float(row.get("price")),
                    "est_cost": _opt_float(row.get("est_cost")),
                }
            )

        with self.engine.begin() as conn:
            conn.execute(orders.insert(), rows)

    def get(self, run_id: int) -> pd.DataFrame:
        """All orders for a run, in insertion order."""
        stmt = orders.select().where(orders.c.run_id == run_id).order_by(orders.c.id)
        with self.engine.connect() as conn:
            return pd.read_sql(stmt, conn)
