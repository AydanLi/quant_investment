from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd
from sqlalchemy import and_, select

from data.models import CorporateAction, DataQualityReport, ProviderPayload
from storage.repositories.base import BaseRepository, upsert
from storage.schema import (
    corporate_actions,
    data_revisions,
    dataset_snapshots,
    dataset_snapshot_actions,
    dataset_snapshot_bars,
    raw_market_data,
    security_master,
)


_BAR_MAP = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}
_BAR_FIELDS = tuple(_BAR_MAP.values())


def _date_str(value: object) -> str:
    return str(pd.Timestamp(value).date())


def _opt_float(value: object) -> float | None:
    return float(value) if value is not None and pd.notna(value) else None


def _different(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left != right
    return not np.isclose(float(left), float(right), rtol=0.0, atol=1e-12)


class TrustedMarketDataRepository(BaseRepository):
    def upsert_metadata(
        self,
        metadata: Mapping[str, Mapping[str, object]],
        *,
        source: str,
    ) -> int:
        now = datetime.now(timezone.utc)
        rows = []
        for ticker, item in metadata.items():
            raw = dict(item)
            rows.append(
                {
                    "ticker": ticker.upper(),
                    "source": source.lower(),
                    "name": raw.get("name") or raw.get("description"),
                    "asset_type": raw.get("assetType") or raw.get("asset_type"),
                    "exchange": raw.get("exchangeCode") or raw.get("exchange"),
                    "listing_date": raw.get("startDate") or raw.get("listing_date"),
                    "delisting_date": raw.get("endDate") or raw.get("delisting_date"),
                    "leveraged_or_inverse": int(bool(raw.get("leveraged_or_inverse", False))),
                    "metadata_json": raw,
                    "fetched_at": now,
                }
            )
        if not rows:
            return 0
        with self.engine.begin() as conn:
            upsert(
                conn,
                security_master,
                rows,
                index_elements=["ticker", "source"],
                update_columns=[
                    "name", "asset_type", "exchange", "listing_date",
                    "delisting_date", "leveraged_or_inverse", "metadata_json",
                    "fetched_at",
                ],
            )
        return len(rows)

    def upsert_raw_bars(self, bars: Mapping[str, pd.DataFrame], *, source: str) -> int:
        source = source.lower()
        now = datetime.now(timezone.utc)
        proposed: list[dict[str, object]] = []
        keys: list[tuple[str, str]] = []
        for ticker, frame in bars.items():
            for session, row in frame.sort_index().iterrows():
                record: dict[str, object] = {
                    "ticker": ticker.upper(),
                    "date": _date_str(session),
                    "source": source,
                    "fetched_at": now,
                }
                for frame_column, db_column in _BAR_MAP.items():
                    record[db_column] = _opt_float(row.get(frame_column))
                proposed.append(record)
                keys.append((ticker.upper(), _date_str(session)))
        if not proposed:
            return 0

        tickers = sorted({key[0] for key in keys})
        dates = sorted({key[1] for key in keys})
        stmt = select(raw_market_data).where(
            and_(
                raw_market_data.c.source == source,
                raw_market_data.c.ticker.in_(tickers),
                raw_market_data.c.date.in_(dates),
            )
        )
        with self.engine.begin() as conn:
            existing = {
                (row.ticker, row.date): row._mapping
                for row in conn.execute(stmt)
            }
            revisions: list[dict[str, object]] = []
            for record in proposed:
                key = (str(record["ticker"]), str(record["date"]))
                previous = existing.get(key)
                revision = 1
                if previous is not None:
                    changed = False
                    for field in _BAR_FIELDS:
                        if _different(previous[field], record[field]):
                            changed = True
                            revisions.append(
                                {
                                    "dataset_table": "raw_market_data",
                                    "ticker": record["ticker"],
                                    "date": record["date"],
                                    "field": field,
                                    "old_value": previous[field],
                                    "new_value": record[field],
                                    "source": source,
                                    "detected_at": now,
                                }
                            )
                    revision = int(previous["revision"]) + (1 if changed else 0)
                record["revision"] = revision
            if revisions:
                conn.execute(data_revisions.insert(), revisions)
            upsert(
                conn,
                raw_market_data,
                proposed,
                index_elements=["ticker", "date", "source"],
                update_columns=[*_BAR_FIELDS, "revision", "fetched_at"],
            )
        return len(proposed)

    def upsert_actions(self, actions: Iterable[CorporateAction]) -> int:
        now = datetime.now(timezone.utc)
        rows = []
        for raw in actions:
            action = raw.normalized()
            rows.append(
                {
                    "ticker": action.ticker,
                    "ex_date": str(action.ex_date.date()),
                    "action_type": action.action_type,
                    "cash_amount": action.cash_amount,
                    "split_factor": action.split_factor,
                    "status": action.status,
                    "source": action.source,
                    "fetched_at": now,
                }
            )
        if not rows:
            return 0
        with self.engine.begin() as conn:
            keys = {
                (str(row["ticker"]), str(row["ex_date"]), str(row["action_type"]), str(row["source"]))
                for row in rows
            }
            tickers = sorted({item[0] for item in keys})
            dates = sorted({item[1] for item in keys})
            existing = {
                (row.ticker, row.ex_date, row.action_type, row.source): row._mapping
                for row in conn.execute(
                    select(corporate_actions).where(
                        and_(
                            corporate_actions.c.ticker.in_(tickers),
                            corporate_actions.c.ex_date.in_(dates),
                        )
                    )
                )
            }
            action_revisions: list[dict[str, object]] = []
            for row in rows:
                key = (
                    row["ticker"], row["ex_date"], row["action_type"], row["source"]
                )
                previous = existing.get(key)
                if previous is None:
                    continue
                for field in ("cash_amount", "split_factor"):
                    if _different(previous[field], row[field]):
                        action_revisions.append(
                            {
                                "dataset_table": "corporate_actions",
                                "ticker": row["ticker"],
                                "date": row["ex_date"],
                                "field": field,
                                "old_value": previous[field],
                                "new_value": row[field],
                                "source": row["source"],
                                "detected_at": now,
                            }
                        )
            if action_revisions:
                conn.execute(data_revisions.insert(), action_revisions)
            upsert(
                conn,
                corporate_actions,
                rows,
                index_elements=["ticker", "ex_date", "action_type", "source"],
                update_columns=["cash_amount", "split_factor", "status", "fetched_at"],
            )
        return len(rows)

    def get_raw_bars(
        self,
        tickers: Sequence[str],
        *,
        source: str,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, pd.DataFrame]:
        stmt = select(raw_market_data).where(
            and_(
                raw_market_data.c.source == source.lower(),
                raw_market_data.c.ticker.in_([ticker.upper() for ticker in tickers]),
            )
        )
        if start:
            stmt = stmt.where(raw_market_data.c.date >= start)
        if end:
            stmt = stmt.where(raw_market_data.c.date <= end)
        stmt = stmt.order_by(raw_market_data.c.ticker, raw_market_data.c.date)
        with self.engine.connect() as conn:
            frame = pd.read_sql(stmt, conn)
        result: dict[str, pd.DataFrame] = {}
        if frame.empty:
            return result
        for ticker, group in frame.groupby("ticker"):
            item = group[["date", *_BAR_FIELDS]].copy()
            item["date"] = pd.to_datetime(item["date"])
            item = item.set_index("date").rename(columns={value: key for key, value in _BAR_MAP.items()})
            result[str(ticker)] = item[[column for column in _BAR_MAP if column in item]]
        return result

    def get_actions(
        self,
        tickers: Sequence[str],
        *,
        source: str,
        start: str | None = None,
        end: str | None = None,
    ) -> tuple[CorporateAction, ...]:
        stmt = select(corporate_actions).where(
            and_(
                corporate_actions.c.source == source.lower(),
                corporate_actions.c.ticker.in_([ticker.upper() for ticker in tickers]),
            )
        )
        if start:
            stmt = stmt.where(corporate_actions.c.ex_date >= start)
        if end:
            stmt = stmt.where(corporate_actions.c.ex_date <= end)
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return tuple(
            CorporateAction(
                ticker=row["ticker"],
                ex_date=pd.Timestamp(row["ex_date"]),
                action_type=row["action_type"],
                cash_amount=row["cash_amount"],
                split_factor=row["split_factor"],
                status=row["status"],
                source=row["source"],
            )
            for row in rows
        )

    def create_snapshot(
        self,
        report: DataQualityReport,
        *,
        as_of: str,
        start_date: str | None,
        end_date: str | None,
        bars: Mapping[str, pd.DataFrame],
        actions: Iterable[CorporateAction],
        source_by_ticker: Mapping[str, str],
        secondary_payload: ProviderPayload | None = None,
    ) -> int:
        if not report.content_hash:
            raise ValueError("A dataset snapshot requires a content hash.")
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(dataset_snapshots.c.id).where(
                    dataset_snapshots.c.content_hash == report.content_hash
                )
            ).scalar_one_or_none()
            if existing is not None:
                return int(existing)
            result = conn.execute(
                dataset_snapshots.insert().values(
                    as_of=as_of,
                    start_date=start_date,
                    end_date=end_date,
                    primary_source=report.primary_source,
                    secondary_source=report.secondary_source,
                    content_hash=report.content_hash,
                    status=report.status.value,
                    quality_json=report.to_dict(),
                )
            )
            snapshot_id = int(result.inserted_primary_key[0])
            bar_rows: list[dict[str, object]] = []
            action_rows: list[dict[str, object]] = []

            def append_payload(
                payload_bars: Mapping[str, pd.DataFrame],
                payload_actions: Iterable[CorporateAction],
                *,
                role: str,
                sources: Mapping[str, str],
            ) -> None:
                for ticker, frame in payload_bars.items():
                    for session, row in frame.sort_index().iterrows():
                        bar_rows.append(
                            {
                                "snapshot_id": snapshot_id,
                                "ticker": ticker.upper(),
                                "date": _date_str(session),
                                "role": role,
                                "source": sources[ticker].lower(),
                                **{
                                    db_column: _opt_float(row.get(frame_column))
                                    for frame_column, db_column in _BAR_MAP.items()
                                },
                            }
                        )
                for raw_action in payload_actions:
                    action = raw_action.normalized()
                    action_rows.append(
                        {
                            "snapshot_id": snapshot_id,
                            "ticker": action.ticker,
                            "ex_date": str(action.ex_date.date()),
                            "action_type": action.action_type,
                            "role": role,
                            "cash_amount": action.cash_amount,
                            "split_factor": action.split_factor,
                            "status": action.status,
                            "source": action.source,
                        }
                    )

            append_payload(
                bars, actions, role="primary", sources=source_by_ticker
            )
            if secondary_payload is not None:
                secondary_sources = {
                    ticker: str(
                        secondary_payload.metadata.get(ticker, {}).get(
                            "source", secondary_payload.source
                        )
                    )
                    for ticker in secondary_payload.bars
                }
                append_payload(
                    secondary_payload.bars,
                    secondary_payload.actions,
                    role="secondary",
                    sources=secondary_sources,
                )
            for start in range(0, len(bar_rows), 1000):
                conn.execute(dataset_snapshot_bars.insert(), bar_rows[start:start + 1000])
            if action_rows:
                conn.execute(dataset_snapshot_actions.insert(), action_rows)
            return snapshot_id

    def load_snapshot(
        self, snapshot_id: int, *, role: str = "primary"
    ) -> ProviderPayload:
        if role not in {"primary", "secondary"}:
            raise ValueError("Snapshot role must be primary or secondary.")
        with self.engine.connect() as conn:
            snapshot = conn.execute(
                select(dataset_snapshots).where(dataset_snapshots.c.id == snapshot_id)
            ).mappings().one_or_none()
            if snapshot is None:
                raise KeyError(f"Unknown dataset snapshot {snapshot_id}.")
            bar_rows = conn.execute(
                select(dataset_snapshot_bars)
                .where(
                    dataset_snapshot_bars.c.snapshot_id == snapshot_id,
                    dataset_snapshot_bars.c.role == role,
                )
                .order_by(dataset_snapshot_bars.c.ticker, dataset_snapshot_bars.c.date)
            ).mappings().all()
            action_rows = conn.execute(
                select(dataset_snapshot_actions)
                .where(
                    dataset_snapshot_actions.c.snapshot_id == snapshot_id,
                    dataset_snapshot_actions.c.role == role,
                )
                .order_by(dataset_snapshot_actions.c.ticker, dataset_snapshot_actions.c.ex_date)
            ).mappings().all()
        grouped: dict[str, list[Mapping[str, object]]] = {}
        for row in bar_rows:
            grouped.setdefault(str(row["ticker"]), []).append(row)
        bars: dict[str, pd.DataFrame] = {}
        metadata: dict[str, dict[str, object]] = {}
        for ticker, rows in grouped.items():
            frame = pd.DataFrame(rows)
            frame["date"] = pd.to_datetime(frame["date"])
            frame = frame.set_index("date").rename(
                columns={value: key for key, value in _BAR_MAP.items()}
            )
            bars[ticker] = frame[list(_BAR_MAP)].astype(float)
            metadata[ticker] = {"source": rows[0]["source"]}
        actions = tuple(
            CorporateAction(
                ticker=str(row["ticker"]),
                ex_date=pd.Timestamp(row["ex_date"]),
                action_type=str(row["action_type"]),
                cash_amount=float(row["cash_amount"]),
                split_factor=float(row["split_factor"]),
                status=str(row["status"]),
                source=str(row["source"]),
            )
            for row in action_rows
        )
        return ProviderPayload(
            bars=bars,
            actions=actions,
            metadata=metadata,
            source=str(snapshot[f"{role}_source"]),
        )

    def load_snapshot_sources(self, snapshot_id: int) -> dict[str, ProviderPayload]:
        with self.engine.connect() as conn:
            roles = conn.execute(
                select(dataset_snapshot_bars.c.role)
                .where(dataset_snapshot_bars.c.snapshot_id == snapshot_id)
                .distinct()
            ).scalars().all()
        return {role: self.load_snapshot(snapshot_id, role=role) for role in roles}

    def revisions(self, ticker: str | None = None) -> pd.DataFrame:
        stmt = select(data_revisions).order_by(data_revisions.c.id)
        if ticker:
            stmt = stmt.where(data_revisions.c.ticker == ticker.upper())
        with self.engine.connect() as conn:
            return pd.read_sql(stmt, conn)
