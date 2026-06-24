"""Latest target-allocation snapshot for a run."""
from __future__ import annotations

from typing import Any, Mapping

import pandas as pd

from storage.repositories.base import BaseRepository
from storage.schema import signals


class SignalRepository(BaseRepository):
    def save(self, run_id: int, latest_signal: Mapping[str, Any]) -> None:
        """Persist the latest signal. ``latest_signal`` carries
        ``date``, ``regime`` and a ``weights`` ticker->weight mapping."""
        weights = latest_signal.get("weights") or {}
        if not weights:
            return

        signal_date = latest_signal.get("date")
        regime = latest_signal.get("regime")
        rows = [
            {
                "run_id": run_id,
                "signal_date": signal_date,
                "regime": regime,
                "ticker": ticker,
                "weight": float(weight),
            }
            for ticker, weight in weights.items()
        ]

        with self.engine.begin() as conn:
            conn.execute(signals.insert(), rows)

    def get(self, run_id: int) -> pd.DataFrame:
        """The signal rows for a run, in insertion order."""
        stmt = signals.select().where(signals.c.run_id == run_id).order_by(signals.c.id)
        with self.engine.connect() as conn:
            return pd.read_sql(stmt, conn)
