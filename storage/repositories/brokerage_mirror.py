"""Persistence for read-only brokerage position snapshots."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

import pandas as pd
from sqlalchemy import and_, desc, select

from storage.repositories.base import BaseRepository
from storage.schema import brokerage_mirror_positions, brokerage_mirror_snapshots


def _number(value: Any, *, required: bool = True) -> Optional[float]:
    if value in (None, ""):
        if required:
            raise ValueError("Required numeric brokerage position field is missing.")
        return None
    return float(value)


class BrokerageMirrorRepository(BaseRepository):
    """Store immutable snapshots; no order-submission methods exist here."""

    def save_snapshot(
        self,
        *,
        provider: str,
        account_ref: str,
        account_type: str,
        positions: Iterable[Mapping[str, Any]],
        captured_at: Optional[datetime] = None,
    ) -> int:
        rows = list(positions)
        symbols = [str(row["symbol"]).strip().upper() for row in rows]
        if any(not symbol for symbol in symbols):
            raise ValueError("Every brokerage position must have a symbol.")
        if len(symbols) != len(set(symbols)):
            raise ValueError("A brokerage snapshot cannot contain duplicate symbols.")

        captured_at = captured_at or datetime.now(timezone.utc)
        header = {
            "provider": provider.strip().lower(),
            "account_ref": account_ref.strip(),
            "account_type": account_type.strip().lower(),
            "captured_at": captured_at,
            "position_count": len(rows),
        }
        with self.engine.begin() as conn:
            result = conn.execute(brokerage_mirror_snapshots.insert().values(header))
            snapshot_id = int(result.inserted_primary_key[0])
            if rows:
                conn.execute(
                    brokerage_mirror_positions.insert(),
                    [
                        {
                            "snapshot_id": snapshot_id,
                            "symbol": symbol,
                            "quantity": _number(row.get("quantity")),
                            "average_buy_price": _number(
                                row.get("average_buy_price"), required=False
                            ),
                            "shares_available_for_sells": _number(
                                row.get("shares_available_for_sells")
                            ),
                            "position_type": str(row.get("type", "long")).lower(),
                        }
                        for symbol, row in zip(symbols, rows)
                    ],
                )
        return snapshot_id

    def get_latest(self, provider: str, account_ref: str) -> pd.DataFrame:
        latest_id = (
            select(brokerage_mirror_snapshots.c.id)
            .where(
                and_(
                    brokerage_mirror_snapshots.c.provider == provider.lower(),
                    brokerage_mirror_snapshots.c.account_ref == account_ref,
                )
            )
            .order_by(desc(brokerage_mirror_snapshots.c.captured_at))
            .limit(1)
            .scalar_subquery()
        )
        stmt = (
            select(
                brokerage_mirror_positions,
                brokerage_mirror_snapshots.c.provider,
                brokerage_mirror_snapshots.c.account_ref,
                brokerage_mirror_snapshots.c.account_type,
                brokerage_mirror_snapshots.c.captured_at,
            )
            .join(
                brokerage_mirror_snapshots,
                brokerage_mirror_positions.c.snapshot_id
                == brokerage_mirror_snapshots.c.id,
            )
            .where(brokerage_mirror_positions.c.snapshot_id == latest_id)
            .order_by(brokerage_mirror_positions.c.symbol)
        )
        with self.engine.connect() as conn:
            return pd.read_sql(stmt, conn)
