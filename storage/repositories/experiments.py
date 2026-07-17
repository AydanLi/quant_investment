"""Experiment-run persistence: the top-level record for each backtest."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any, Mapping, Optional

import pandas as pd
from sqlalchemy import delete, select

from storage.repositories.base import BaseRepository
from storage.schema import experiment_runs

# Config fields promoted to their own queryable columns (mirrored from config_json).
_PROMOTED_CONFIG_FIELDS = (
    "start_date",
    "end_date",
    "benchmark",
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

# Map summary-Series labels to experiment_runs columns.
_SUMMARY_TO_COLUMN = {
    "Start Equity": "start_equity",
    "End Equity": "end_equity",
    "Total Return": "total_return",
    "CAGR": "cagr",
    "Annual Vol": "annual_vol",
    "Sharpe": "sharpe",
    "Sortino": "sortino",
    "Max Drawdown": "max_drawdown",
    "Avg Turnover": "avg_turnover",
}

# Infra, not strategy: excluded from the reproducibility hash and snapshot so the
# same strategy hashes identically regardless of where its data lives.
_NON_STRATEGY_FIELDS = {"db_url"}


def serialize_config(config: Any) -> tuple[dict, str]:
    """Return ``(config_dict, config_hash)`` for a Config dataclass or mapping.

    The hash is a stable SHA-256 over the canonical JSON of the strategy
    parameters, enabling dedup / lookup of identical configurations.
    """
    if dataclasses.is_dataclass(config) and not isinstance(config, type):
        raw = dataclasses.asdict(config)
    elif isinstance(config, Mapping):
        raw = dict(config)
    else:  # last resort: pull public attributes
        raw = {k: v for k, v in vars(config).items() if not k.startswith("_")}

    config_dict = {k: v for k, v in raw.items() if k not in _NON_STRATEGY_FIELDS}
    canonical = json.dumps(config_dict, sort_keys=True, default=str)
    config_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return config_dict, config_hash


def _opt_float(value: Any) -> Optional[float]:
    return float(value) if value is not None and pd.notna(value) else None


class ExperimentRepository(BaseRepository):
    def save_run(
        self,
        *,
        scenario_name: str,
        config: Any,
        summary: pd.Series,
        latest_signal: Mapping[str, Any],
        status: str = "complete",
        notes: Optional[str] = None,
        tags: Optional[str] = None,
        dataset_snapshot_id: int | None = None,
        universe_version: str | None = None,
        strategy_version: str | None = None,
        admissible: bool = False,
        invalidated_reason: str | None = None,
    ) -> int:
        """Insert one experiment_runs row; returns the new run id."""
        config_dict, config_hash = serialize_config(config)

        frequency = config_dict.get("rebalance_frequency")
        if frequency in {"D", "W"}:
            status = "exploratory_only"
            admissible = False
        if dataset_snapshot_id is None:
            status = "invalid_data_v1"
            admissible = False
            invalidated_reason = invalidated_reason or "No trusted dataset snapshot."
        strategy_version = strategy_version or config_dict.get("strategy_version")
        universe_version = universe_version or config_dict.get("universe_version")
        if strategy_version in {None, "UNFROZEN"}:
            admissible = False

        values: dict[str, Any] = {
            "scenario_name": scenario_name,
            "config_json": config_dict,
            "config_hash": config_hash,
            "latest_signal_date": latest_signal.get("date"),
            "latest_regime": latest_signal.get("regime"),
            "status": status,
            "notes": notes,
            "tags": tags,
            "dataset_snapshot_id": dataset_snapshot_id,
            "universe_version": universe_version,
            "strategy_version": strategy_version,
            "admissible": int(admissible),
            "invalidated_reason": invalidated_reason,
        }
        for field in _PROMOTED_CONFIG_FIELDS:
            values[field] = config_dict.get(field)
        for label, column in _SUMMARY_TO_COLUMN.items():
            values[column] = _opt_float(summary.get(label))

        with self.engine.begin() as conn:
            result = conn.execute(experiment_runs.insert().values(**values))
            return int(result.inserted_primary_key[0])

    def get_runs(self, limit: int = 20, scenario_name: Optional[str] = None) -> pd.DataFrame:
        """Most-recent runs first, optionally filtered by scenario name."""
        stmt = select(experiment_runs).order_by(experiment_runs.c.id.desc())
        if scenario_name is not None:
            stmt = stmt.where(experiment_runs.c.scenario_name == scenario_name)
        stmt = stmt.limit(int(limit))
        with self.engine.connect() as conn:
            return pd.read_sql(stmt, conn)

    def get_run(self, run_id: int) -> Optional[pd.Series]:
        """A single run as a Series, or None if it doesn't exist."""
        stmt = select(experiment_runs).where(experiment_runs.c.id == run_id)
        with self.engine.connect() as conn:
            df = pd.read_sql(stmt, conn)
        return None if df.empty else df.iloc[0]

    def find_by_config_hash(self, config_hash: str) -> pd.DataFrame:
        """All runs sharing a config hash — for spotting duplicate experiments."""
        stmt = (
            select(experiment_runs)
            .where(experiment_runs.c.config_hash == config_hash)
            .order_by(experiment_runs.c.id.desc())
        )
        with self.engine.connect() as conn:
            return pd.read_sql(stmt, conn)

    def delete_run(self, run_id: int) -> bool:
        """Delete a run and (via ON DELETE CASCADE) all its child rows.

        Returns True if a row was deleted. SQLite cascade relies on the
        PRAGMA enabled in storage.db; Postgres/MySQL enforce it natively.
        """
        with self.engine.begin() as conn:
            result = conn.execute(
                delete(experiment_runs).where(experiment_runs.c.id == run_id)
            )
            return result.rowcount > 0
