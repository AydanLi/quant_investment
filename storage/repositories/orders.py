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
                    "signal_date": str(row.get("signal_date")),
                    "ticker": row.get("ticker"),
                    "side": row.get("side"),
                    "weight_change": _opt_float(row.get("weight_change")),
                    "quantity": _opt_float(row.get("quantity")),
                    "notional": _opt_float(row.get("notional")),
                    "price": _opt_float(row.get("price")),
                    "est_cost": _opt_float(row.get("est_cost")),
                    "trading_cost_dollars": _opt_float(row.get("trading_cost_dollars")),
                    "slippage_dollars": _opt_float(row.get("slippage_dollars")),
                    "impact_cost_dollars": _opt_float(row.get("impact_cost_dollars")),
                    "adv_fraction": _opt_float(row.get("adv_fraction")),
                    "average_entry_cost": _opt_float(row.get("average_entry_cost")),
                    "gross_realized_pnl": _opt_float(row.get("gross_realized_pnl")),
                    "realized_pnl": _opt_float(row.get("realized_pnl")),
                }
            )

        with self.engine.begin() as conn:
            conn.execute(orders.insert(), rows)

    def get(self, run_id: int) -> pd.DataFrame:
        """All orders for a run, in insertion order."""
        stmt = orders.select().where(orders.c.run_id == run_id).order_by(orders.c.id)
        with self.engine.connect() as conn:
            return pd.read_sql(stmt, conn)
