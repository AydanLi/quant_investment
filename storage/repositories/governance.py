from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Mapping, Sequence

import numpy as np
import pandas as pd
from sqlalchemy import select, update

from config.universe import UniverseVersion
from storage.repositories.base import BaseRepository, upsert
from storage.schema import (
    admission_runs,
    experiment_runs,
    parameter_trials,
    strategy_versions,
    universe_versions,
)


class GovernanceRepository(BaseRepository):
    def save_universe_version(
        self,
        version: UniverseVersion,
        *,
        eligibility: Sequence[Mapping[str, object]] = (),
    ) -> None:
        now = datetime.now(timezone.utc)
        row = {
            "version": version.version,
            "effective_date": version.effective_date,
            "status": "approved" if version.approved else "draft",
            "seed_tickers_json": list(version.seed_tickers),
            "rules_json": _jsonable(asdict(version.rules)),
            "eligibility_json": _jsonable(list(eligibility)),
            "approved_at": now if version.approved else None,
            "approved_by": version.approved_by,
            "historical_universe_integrity": int(version.historical_universe_integrity),
        }
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(universe_versions).where(
                    universe_versions.c.version == version.version
                )
            ).mappings().one_or_none()
            if existing is not None:
                immutable = {
                    "effective_date": row["effective_date"],
                    "seed_tickers_json": row["seed_tickers_json"],
                    "rules_json": row["rules_json"],
                    "historical_universe_integrity": row["historical_universe_integrity"],
                }
                if any(existing[key] != value for key, value in immutable.items()):
                    raise ValueError(
                        "Universe versions are immutable; create a new version for changes."
                    )
                if version.approved and existing["status"] != "approved":
                    conn.execute(
                        update(universe_versions)
                        .where(universe_versions.c.version == version.version)
                        .values(
                            status="approved",
                            approved_at=now,
                            approved_by=version.approved_by,
                            eligibility_json=_jsonable(list(eligibility)),
                        )
                    )
                return
            upsert(
                conn,
                universe_versions,
                [row],
                index_elements=["version"],
                update_columns=[
                    "effective_date", "status", "seed_tickers_json", "rules_json",
                    "eligibility_json", "approved_at", "approved_by",
                    "historical_universe_integrity",
                ],
            )

    def save_strategy_version(
        self,
        *,
        version: str,
        universe_version: str,
        protocol: Mapping[str, object],
        dataset_snapshot_id: int | None = None,
        code_commit: str | None = None,
        frozen: bool = False,
    ) -> None:
        now = datetime.now(timezone.utc)
        row = {
            "version": version,
            "status": "frozen" if frozen else "draft",
            "frozen_at": now if frozen else None,
            "universe_version": universe_version,
            "dataset_snapshot_id": dataset_snapshot_id,
            "code_commit": code_commit,
            "protocol_json": _jsonable(dict(protocol)),
        }
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(strategy_versions).where(
                    strategy_versions.c.version == version
                )
            ).mappings().one_or_none()
            if existing is not None and existing["status"] == "frozen":
                immutable = {
                    "universe_version": universe_version,
                    "dataset_snapshot_id": dataset_snapshot_id,
                    "code_commit": code_commit,
                    "protocol_json": _jsonable(dict(protocol)),
                }
                if any(existing[key] != value for key, value in immutable.items()):
                    raise ValueError(
                        "Frozen strategy versions are immutable; create a new strategy version."
                    )
                return
            upsert(
                conn,
                strategy_versions,
                [row],
                index_elements=["version"],
                update_columns=[
                    "status", "frozen_at", "universe_version", "dataset_snapshot_id",
                    "code_commit", "protocol_json",
                ],
            )

    def is_universe_approved(self, version: str) -> bool:
        with self.engine.connect() as conn:
            status = conn.execute(
                select(universe_versions.c.status).where(
                    universe_versions.c.version == version
                )
            ).scalar_one_or_none()
        return status == "approved"

    def is_strategy_frozen(self, version: str) -> bool:
        with self.engine.connect() as conn:
            status = conn.execute(
                select(strategy_versions.c.status).where(
                    strategy_versions.c.version == version
                )
            ).scalar_one_or_none()
        return status == "frozen"

    def start_paper_clock(self, version: str, *, started_at: datetime | None = None) -> None:
        started_at = started_at or datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            status = conn.execute(
                select(strategy_versions.c.status).where(
                    strategy_versions.c.version == version
                )
            ).scalar_one_or_none()
            if status != "frozen":
                raise ValueError("Paper clock can start only for a frozen strategy version.")
            conn.execute(
                update(strategy_versions)
                .where(strategy_versions.c.version == version)
                .values(paper_start=started_at, paper_clock_restart_reason=None)
            )

    def restart_paper_clock(self, version: str, *, reason: str) -> None:
        if not reason.strip():
            raise ValueError("A material strategy change requires a restart reason.")
        with self.engine.begin() as conn:
            conn.execute(
                update(strategy_versions)
                .where(strategy_versions.c.version == version)
                .values(
                    paper_start=datetime.now(timezone.utc),
                    paper_clock_restart_reason=reason.strip(),
                )
            )

    def save_admission(
        self,
        *,
        strategy_version: str,
        methodology: str,
        status: str,
        results: Mapping[str, object],
        trials: Sequence[Mapping[str, object]],
    ) -> int:
        if bool(results.get("selection_uses_future_holdout", False)):
            raise ValueError("Future paper holdout cannot be used for candidate selection.")
        with self.engine.begin() as conn:
            result = conn.execute(
                admission_runs.insert().values(
                    strategy_version=strategy_version,
                    methodology=methodology,
                    status=status,
                    selection_uses_future_holdout=0,
                    results_json=_jsonable(dict(results)),
                )
            )
            admission_id = int(result.inserted_primary_key[0])
            rows = [
                {
                    "admission_run_id": admission_id,
                    "label": trial["label"],
                    "parameters_json": _jsonable(trial.get("parameters", {})),
                    "folds_json": _jsonable(trial.get("folds", [])),
                    "status": trial.get("status", "evaluated"),
                    "score": _jsonable(trial.get("score")),
                }
                for trial in trials
            ]
            if rows:
                conn.execute(parameter_trials.insert(), rows)
            return admission_id

    def invalidate_legacy_experiments(self) -> int:
        with self.engine.begin() as conn:
            result = conn.execute(
                update(experiment_runs)
                .where(experiment_runs.c.dataset_snapshot_id.is_(None))
                .values(
                    status="invalid_data_v1",
                    admissible=0,
                    invalidated_reason="Legacy adjusted-price cache may contain batch-boundary dividend loss.",
                )
            )
            return int(result.rowcount or 0)


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value
