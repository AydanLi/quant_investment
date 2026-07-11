"""Daily portfolio state: equity curve plus per-asset weights.

The backtest produces a DataFrame indexed by date with columns including
``equity, gross_return, daily_return, regime, turnover, est_cost`` and one
``w_<ticker>`` column per universe member. The old store dropped the weight
columns entirely; here they are persisted in long format in
``portfolio_weights`` so the historical allocation can be reconstructed for
any run. ``daily_return`` and ``est_cost`` use net-return fraction units.
"""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from storage.repositories.base import BaseRepository
from storage.schema import portfolio_daily, portfolio_weights

# Weights below this are treated as "not held" and not stored (a missing
# ticker on a given day reconstructs as weight 0).
_WEIGHT_EPSILON = 1e-9


def _date_str(value: Any) -> str:
    return str(value.date()) if hasattr(value, "date") else str(value)


def _opt_float(value: Any) -> Optional[float]:
    return float(value) if value is not None and pd.notna(value) else None


class PortfolioRepository(BaseRepository):
    def save(self, run_id: int, portfolio: pd.DataFrame) -> None:
        """Persist daily rows and per-asset weights in one transaction."""
        if portfolio.empty:
            return

        weight_columns = [c for c in portfolio.columns if str(c).startswith("w_")]

        daily_rows = []
        weight_rows = []
        for date, row in portfolio.iterrows():
            date_str = _date_str(date)
            daily_rows.append(
                {
                    "run_id": run_id,
                    "date": date_str,
                    "equity": _opt_float(row.get("equity")),
                    "daily_return": _opt_float(row.get("daily_return")),
                    "regime": row.get("regime"),
                    "turnover": _opt_float(row.get("turnover")),
                    "est_cost": _opt_float(row.get("est_cost")),
                }
            )
            for col in weight_columns:
                weight = row.get(col)
                if weight is None or not pd.notna(weight) or abs(weight) <= _WEIGHT_EPSILON:
                    continue
                weight_rows.append(
                    {
                        "run_id": run_id,
                        "date": date_str,
                        "ticker": str(col)[2:],  # strip "w_"
                        "weight": float(weight),
                    }
                )

        with self.engine.begin() as conn:
            conn.execute(portfolio_daily.insert(), daily_rows)
            if weight_rows:
                conn.execute(portfolio_weights.insert(), weight_rows)

    def get_daily(self, run_id: int) -> pd.DataFrame:
        """Daily equity/return/regime/turnover rows for a run, ordered by date."""
        stmt = (
            portfolio_daily.select()
            .where(portfolio_daily.c.run_id == run_id)
            .order_by(portfolio_daily.c.date)
        )
        with self.engine.connect() as conn:
            return pd.read_sql(stmt, conn)

    def get_weights(self, run_id: int, wide: bool = False) -> pd.DataFrame:
        """Per-asset weights for a run.

        Long format by default; ``wide=True`` pivots to date x ticker with
        absent (unheld) positions filled as 0.
        """
        stmt = (
            portfolio_weights.select()
            .where(portfolio_weights.c.run_id == run_id)
            .order_by(portfolio_weights.c.date)
        )
        with self.engine.connect() as conn:
            df = pd.read_sql(stmt, conn)
        if not wide or df.empty:
            return df
        return (
            df.pivot(index="date", columns="ticker", values="weight")
            .fillna(0.0)
            .sort_index()
        )
