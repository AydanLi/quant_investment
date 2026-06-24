"""One-off ETL: migrate the legacy SQLiteStore database into the v2 schema.

The legacy ``quant_research.db`` was written by ``storage.sqlite_store`` with a
hand-rolled 4-table schema. This script builds a fresh v2 database (the schema
in ``storage.schema``, at Alembic head) and copies the legacy data across,
transforming column names and reconstructing the new ``config_json`` /
``config_hash`` from the promoted fields the old runs stored.

Deliberately NON-destructive: it writes a new file (default
``quant_research_v2.db``) and leaves the live ``quant_research.db`` untouched so
the app — still on SQLiteStore until the Phase 4 cutover — keeps working. The
swap happens in Phase 4.

Known limitation: per-day ``portfolio_weights`` cannot be backfilled (the legacy
schema never stored daily weights), so that table starts empty for migrated
runs. ``market_data`` likewise starts empty.

Usage:
    python scripts/migrate_legacy_to_v2.py
    python scripts/migrate_legacy_to_v2.py --old-db quant_research.db --new-db quant_research_v2.db
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Make the project importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.db import create_db_engine  # noqa: E402
from storage.repositories.experiments import serialize_config  # noqa: E402
from storage.schema import (  # noqa: E402
    experiment_runs,
    orders,
    portfolio_daily,
    signals,
)

# Legacy experiment_runs fields that together describe the strategy config.
_LEGACY_CONFIG_FIELDS = (
    "start_date",
    "rebalance_frequency",
    "top_n",
    "min_momentum_threshold",
    "target_annual_vol",
    "max_asset_weight",
    "risk_off_cash_weight",
    "vix_risk_off_threshold",
    "vix_high_threshold",
    "trading_cost_bps",
)

# Promoted columns carried straight across to the v2 experiment_runs row.
_PROMOTED = _LEGACY_CONFIG_FIELDS + (
    "scenario_name",
    "start_equity",
    "end_equity",
    "total_return",
    "cagr",
    "annual_vol",
    "sharpe",
    "sortino",
    "max_drawdown",
    "avg_turnover",
    "latest_signal_date",
    "latest_regime",
)


def _parse_created_at(run_time: str | None) -> datetime:
    if not run_time:
        return datetime.utcnow()
    try:
        return datetime.fromisoformat(run_time)
    except ValueError:
        return datetime.utcnow()


def _build_v2_schema(new_db: Path) -> None:
    """Create the new database at Alembic head via the real migration path."""
    from alembic import command
    from alembic.config import Config as AlembicConfig

    os.environ["QUANT_DB_URL"] = f"sqlite:///{new_db}"
    alembic_cfg = AlembicConfig(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    command.upgrade(alembic_cfg, "head")


def migrate(old_db: Path, new_db: Path) -> dict[str, int]:
    if not old_db.exists():
        raise FileNotFoundError(f"Legacy database not found: {old_db}")
    if new_db.exists():
        new_db.unlink()

    _build_v2_schema(new_db)

    src = sqlite3.connect(old_db)
    src.row_factory = sqlite3.Row
    engine = create_db_engine(f"sqlite:///{new_db}")

    counts = {"experiment_runs": 0, "portfolio_daily": 0, "orders": 0, "signals": 0}

    with engine.begin() as conn:
        # experiment_runs — preserve ids so child FKs line up; rebuild config.
        for row in src.execute("SELECT * FROM experiment_runs ORDER BY id"):
            config_dict = {
                f: row[f] for f in _LEGACY_CONFIG_FIELDS if row[f] is not None
            }
            _, config_hash = serialize_config(config_dict)
            values = {
                "id": row["id"],
                "created_at": _parse_created_at(row["run_time"]),
                "config_json": config_dict,
                "config_hash": config_hash,
                "status": "complete",
                "notes": "Migrated from legacy SQLiteStore schema",
                "tags": "legacy",
            }
            for field in _PROMOTED:
                values[field] = row[field]
            conn.execute(experiment_runs.insert().values(**values))
            counts["experiment_runs"] += 1

        # portfolio_daily — same shape minus est_cost (unknown for legacy runs).
        daily_rows = [
            {
                "run_id": r["run_id"],
                "date": r["date"],
                "equity": r["equity"],
                "daily_return": r["daily_return"],
                "regime": r["regime"],
                "turnover": r["turnover"],
                "est_cost": None,
            }
            for r in src.execute("SELECT * FROM portfolio_daily ORDER BY id")
        ]
        if daily_rows:
            conn.execute(portfolio_daily.insert(), daily_rows)
            counts["portfolio_daily"] = len(daily_rows)

        # orders — order_time -> order_date.
        order_rows = [
            {
                "run_id": r["run_id"],
                "order_date": r["order_time"],
                "ticker": r["ticker"],
                "side": r["side"],
                "weight_change": r["weight_change"],
                "price": None,
                "est_cost": None,
            }
            for r in src.execute("SELECT * FROM orders ORDER BY id")
        ]
        if order_rows:
            conn.execute(orders.insert(), order_rows)
            counts["orders"] = len(order_rows)

        # signals — identical shape.
        signal_rows = [
            {
                "run_id": r["run_id"],
                "signal_date": r["signal_date"],
                "regime": r["regime"],
                "ticker": r["ticker"],
                "weight": r["weight"],
            }
            for r in src.execute("SELECT * FROM signals ORDER BY id")
        ]
        if signal_rows:
            conn.execute(signals.insert(), signal_rows)
            counts["signals"] = len(signal_rows)

    src.close()
    return counts


def _legacy_counts(old_db: Path) -> dict[str, int]:
    src = sqlite3.connect(old_db)
    out = {}
    for t in ("experiment_runs", "portfolio_daily", "orders", "signals"):
        out[t] = src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    src.close()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-db", default="quant_research.db", type=Path)
    parser.add_argument("--new-db", default="quant_research_v2.db", type=Path)
    args = parser.parse_args()

    before = _legacy_counts(args.old_db)
    migrated = migrate(args.old_db, args.new_db)

    print(f"Migrated legacy DB '{args.old_db}' -> v2 DB '{args.new_db}'\n")
    ok = True
    for table in ("experiment_runs", "portfolio_daily", "orders", "signals"):
        match = "OK" if before[table] == migrated[table] else "MISMATCH"
        ok = ok and before[table] == migrated[table]
        print(f"  {table:<16} legacy={before[table]:>6}  migrated={migrated[table]:>6}  [{match}]")
    print(f"  {'portfolio_weights':<16} legacy={'n/a':>6}  migrated={0:>6}  [not backfillable]")
    print(f"  {'market_data':<16} legacy={'n/a':>6}  migrated={0:>6}  [starts empty]")

    print("\nResult:", "all row counts match" if ok else "ROW COUNT MISMATCH — investigate")
    print(f"Live '{args.old_db}' left untouched; swap happens in Phase 4.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
