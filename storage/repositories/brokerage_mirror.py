"""Persistence for read-only brokerage position snapshots."""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

import pandas as pd
from sqlalchemy import and_, desc, select

from storage.repositories.base import BaseRepository
from storage.schema import brokerage_mirror_positions, brokerage_mirror_snapshots


_MASKED_ACCOUNT_REF = re.compile(
    r"^(?:[*xX•]{2,}[- ]*)?([A-Za-z0-9]{4})$"
)
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-^=]{0,19}$")
_LABEL = re.compile(r"^[a-z0-9][a-z0-9_. -]{0,39}$")
_POSITION_TYPES = {"long", "short"}


def _masked_account_ref(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("account_ref must be a string containing masked data.")
    raw = value.strip()
    match = _MASKED_ACCOUNT_REF.fullmatch(raw)
    if match is None:
        raise ValueError(
            "account_ref must contain only the last four characters or a "
            "masked prefix followed by the last four characters."
        )
    return match.group(1).upper()


def _label(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string.")
    normalized = value.strip().lower()
    if not _LABEL.fullmatch(normalized):
        raise ValueError(f"{field} is empty or contains unsupported characters.")
    return normalized


def _number(
    value: Any,
    field: str,
    *,
    required: bool = True,
) -> Optional[float]:
    if value in (None, ""):
        if required:
            raise ValueError(f"Required numeric field {field} is missing.")
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite non-negative number.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{field} must be a finite non-negative number."
        ) from exc
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{field} must be a finite non-negative number.")
    return number


def _position_row(row: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise ValueError("Every brokerage position must be an object.")
    raw_symbol = row.get("symbol")
    if not isinstance(raw_symbol, str):
        raise ValueError("Every brokerage position symbol must be a string.")
    symbol = raw_symbol.strip().upper()
    if not _SYMBOL.fullmatch(symbol):
        raise ValueError(f"Invalid brokerage position symbol: {symbol!r}.")
    position_type = str(row.get("type", "long")).strip().lower()
    if position_type not in _POSITION_TYPES:
        raise ValueError("Brokerage position type must be 'long' or 'short'.")
    quantity = _number(row.get("quantity"), "quantity")
    shares_available = _number(
        row.get("shares_available_for_sells"),
        "shares_available_for_sells",
    )
    if shares_available > quantity:
        raise ValueError(
            "shares_available_for_sells cannot exceed the position quantity."
        )
    return {
        "symbol": symbol,
        "quantity": quantity,
        "average_buy_price": _number(
            row.get("average_buy_price"),
            "average_buy_price",
            required=False,
        ),
        "shares_available_for_sells": shares_available,
        "position_type": position_type,
    }


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
        position_rows = [_position_row(row) for row in rows]
        symbols = [row["symbol"] for row in position_rows]
        if len(symbols) != len(set(symbols)):
            raise ValueError("A brokerage snapshot cannot contain duplicate symbols.")

        captured_at = captured_at or datetime.now(timezone.utc)
        if not isinstance(captured_at, datetime):
            raise ValueError("captured_at must be a datetime.")
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise ValueError("captured_at must include a timezone.")
        header = {
            "provider": _label(provider, "provider"),
            "account_ref": _masked_account_ref(account_ref),
            "account_type": _label(account_type, "account_type"),
            "captured_at": captured_at,
            "position_count": len(position_rows),
        }
        with self.engine.begin() as conn:
            result = conn.execute(brokerage_mirror_snapshots.insert().values(header))
            snapshot_id = int(result.inserted_primary_key[0])
            if position_rows:
                conn.execute(
                    brokerage_mirror_positions.insert(),
                    [
                        {
                            "snapshot_id": snapshot_id,
                            **row,
                        }
                        for row in position_rows
                    ],
                )
        return snapshot_id

    def get_latest(self, provider: str, account_ref: str) -> pd.DataFrame:
        normalized_provider = _label(provider, "provider")
        normalized_account_ref = _masked_account_ref(account_ref)
        latest_id = (
            select(brokerage_mirror_snapshots.c.id)
            .where(
                and_(
                    brokerage_mirror_snapshots.c.provider == normalized_provider,
                    brokerage_mirror_snapshots.c.account_ref
                    == normalized_account_ref,
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
