from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
from typing import Mapping, Sequence

import numpy as np
import pandas as pd
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from config.universe import UniverseVersion
from data.models import DATA_QUALITY_MODEL_VERSION
from research.runtime import FrozenRuntimeManifest, assert_code_identity, assert_runtime_matches, canonical_hash
from storage.repositories.base import BaseRepository, upsert
from storage.schema import (
    admission_runs,
    dataset_snapshots,
    experiment_runs,
    parameter_trials,
    strategy_versions,
    universe_versions,
    validation_runs,
)


class GovernanceRepository(BaseRepository):
    def start_validation_run(
        self, version: str, *, account_ref: str,
        environment: str = "PAPER", execution_model: str = "REPLAY_OPEN",
    ) -> int:
        """Start a prospective observation clock, never backdate to research freeze."""
        if not account_ref.strip():
            raise ValueError("Validation requires an account reference.")
        if environment != "PAPER" or execution_model != "REPLAY_OPEN":
            raise ValueError("Only the implemented PAPER/REPLAY_OPEN validation is supported.")
        manifest = self.load_frozen_runtime(version)
        active = select(validation_runs).where(
            validation_runs.c.strategy_version == version,
            validation_runs.c.account_ref == account_ref,
            validation_runs.c.environment == environment,
            validation_runs.c.execution_model == execution_model,
            validation_runs.c.status == "active",
        )
        with self.engine.begin() as conn:
            row = conn.execute(active).mappings().one_or_none()
            if row is not None:
                if row["runtime_hash"] != manifest.runtime_hash:
                    raise ValueError("Active validation is bound to a different runtime.")
                return int(row["id"])
            try:
                with conn.begin_nested():
                    result = conn.execute(validation_runs.insert().values(
                        strategy_version=version, runtime_hash=manifest.runtime_hash,
                        account_ref=account_ref, environment=environment,
                        execution_model=execution_model,
                        started_at=datetime.now(timezone.utc).replace(tzinfo=None),
                        status="active",
                    ))
            except IntegrityError:
                row = conn.execute(active).mappings().one_or_none()
                if row is None or row["runtime_hash"] != manifest.runtime_hash:
                    raise
                return int(row["id"])
            return int(result.inserted_primary_key[0])

    def create_universe_draft(
        self,
        version: UniverseVersion,
        *,
        eligibility: Sequence[Mapping[str, object]] = (),
    ) -> None:
        if version.approved or version.approved_by:
            raise ValueError(
                "Universe creation accepts drafts only; approve in a separate action."
            )
        row = {
            "version": version.version,
            "effective_date": version.effective_date,
            "status": "draft",
            "seed_tickers_json": list(version.seed_tickers),
            "rules_json": _jsonable(asdict(version.rules)),
            "eligibility_json": _jsonable(list(eligibility)),
            "approved_at": None,
            "approved_by": None,
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
                return
            conn.execute(universe_versions.insert().values(**row))

    def save_universe_version(
        self,
        version: UniverseVersion,
        *,
        eligibility: Sequence[Mapping[str, object]] = (),
    ) -> None:
        """Compatibility wrapper; creation remains draft-only."""
        self.create_universe_draft(version, eligibility=eligibility)

    def approve_universe_version(self, version: str, *, approved_by: str) -> None:
        if not approved_by.strip():
            raise ValueError("Universe approval requires an operator identity.")
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            row = conn.execute(
                select(universe_versions).where(
                    universe_versions.c.version == version
                )
            ).mappings().one_or_none()
            if row is None:
                raise ValueError(f"Unknown universe version {version}.")
            if row["status"] == "approved":
                return
            if row["status"] != "draft":
                raise ValueError("Only a draft universe version can be approved.")
            conn.execute(
                update(universe_versions)
                .where(universe_versions.c.version == version)
                .values(
                    status="approved",
                    approved_at=now,
                    approved_by=approved_by.strip(),
                )
            )

    def create_strategy_version(
        self,
        *,
        version: str,
        universe_version: str,
        protocol: Mapping[str, object],
        dataset_snapshot_id: int,
        code_commit: str | None = None,
    ) -> None:
        row = {
            "version": version,
            "status": "draft",
            "frozen_at": None,
            "universe_version": universe_version,
            "dataset_snapshot_id": int(dataset_snapshot_id),
            "code_commit": code_commit,
            "protocol_json": _jsonable(dict(protocol)),
        }
        with self.engine.begin() as conn:
            self._require_strategy_references(
                conn,
                universe_version=universe_version,
                dataset_snapshot_id=int(dataset_snapshot_id),
            )
            universe = conn.execute(select(universe_versions).where(
                universe_versions.c.version == universe_version)).mappings().one()
            if isinstance(protocol.get("base_config"), Mapping) and bool(
                protocol["base_config"].get("historical_universe_integrity", False)
            ) != bool(universe["historical_universe_integrity"]):
                raise ValueError("Historical universe integrity must match the approved universe evidence.")
            existing = conn.execute(
                select(strategy_versions).where(
                    strategy_versions.c.version == version
                )
            ).mappings().one_or_none()
            if existing is not None:
                immutable = {
                    "universe_version": universe_version,
                    "dataset_snapshot_id": dataset_snapshot_id,
                    "code_commit": code_commit,
                    "protocol_json": _jsonable(dict(protocol)),
                }
                if any(existing[key] != value for key, value in immutable.items()):
                    raise ValueError(
                        "Strategy versions are immutable; create a new strategy version."
                    )
                return
            conn.execute(strategy_versions.insert().values(**row))

    def save_strategy_version(
        self,
        *,
        version: str,
        universe_version: str,
        protocol: Mapping[str, object],
        dataset_snapshot_id: int | None = None,
        code_commit: str | None = None,
        frozen: bool = False,
        approved_by: str | None = None,
    ) -> None:
        """Thin compatibility wrapper around the explicit lifecycle."""
        if dataset_snapshot_id is None:
            raise ValueError("Strategy versions require a dataset snapshot.")
        if frozen:
            with self.engine.connect() as conn:
                existing = conn.execute(
                    select(strategy_versions.c.version).where(
                        strategy_versions.c.version == version
                    )
                ).scalar_one_or_none()
            if existing is None:
                raise ValueError("Create the strategy draft before freezing it.")
            self.create_strategy_version(
                version=version,
                universe_version=universe_version,
                protocol=protocol,
                dataset_snapshot_id=dataset_snapshot_id,
                code_commit=code_commit,
            )
            self.freeze_strategy_version(version, approved_by=approved_by or "")
            return
        self.create_strategy_version(
            version=version,
            universe_version=universe_version,
            protocol=protocol,
            dataset_snapshot_id=dataset_snapshot_id,
            code_commit=code_commit,
        )

    def freeze_strategy_version(
        self, version: str, *, admission_run_id: int | None = None, approved_by: str,
    ) -> None:
        if not approved_by.strip():
            raise ValueError("Strategy approval requires nonempty approved_by.")
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            strategy = self._strategy(conn, version)
            self._require_strategy_references(
                conn,
                universe_version=str(strategy["universe_version"]),
                dataset_snapshot_id=int(strategy["dataset_snapshot_id"]),
            )
            admission = self._admitted_run(
                conn, version, admission_run_id=admission_run_id
            )
            if admission is None:
                raise ValueError(
                    "Strategy freezing requires an admitted AdmissionRun."
                )
            self._require_complete_final_trials(conn, admission)
            manifest_data = (admission["results_json"] or {}).get("runtime_manifest")
            if not isinstance(manifest_data, Mapping):
                raise ValueError("Strategy freezing requires a verified runtime manifest.")
            manifest = FrozenRuntimeManifest.from_dict(manifest_data)
            universe_integrity = conn.execute(select(universe_versions.c.historical_universe_integrity).where(
                universe_versions.c.version == strategy["universe_version"])).scalar_one()
            if bool(manifest.config["historical_universe_integrity"]) != bool(universe_integrity):
                raise ValueError("Frozen runtime cannot override the approved historical universe evidence.")
            if (manifest.config["strategy_version"] != version
                    or manifest.dataset_snapshot_id != int(strategy["dataset_snapshot_id"])
                    or manifest.config["universe_version"] != strategy["universe_version"]):
                raise ValueError("Runtime manifest does not match the strategy identity.")
            if (manifest.protocol_hash != canonical_hash(strategy["protocol_json"])
                    or manifest.code_identity.get("code_commit") != strategy["code_commit"]):
                raise ValueError("Runtime manifest is not bound to the admitted protocol/code.")
            protocol = strategy["protocol_json"]
            if not isinstance(protocol.get("base_config"), Mapping):
                raise ValueError("Freezing requires a complete preregistered base configuration.")
            expected_base = replace(manifest, config=protocol["base_config"]).to_config()
            expected_config = replace(expected_base, strategy_version=version)
            if protocol.get("protocol_version") == "risk-extension-v1":
                from research.risk_model_protocol import dynamic_factor_candidate_grid
                selected = next((candidate for candidate in dynamic_factor_candidate_grid()
                                 if candidate.label == manifest.selected_label), None)
                if selected is None or manifest.parent_runtime_hash != protocol.get("parent_runtime_hash"):
                    raise ValueError("Risk runtime must bind the selected candidate and frozen parent.")
                expected_config = replace(expected_config, risk_model="dynamic_factor",
                    ewma_half_life_days=selected.half_life_days, pca_stress_multiplier=selected.stress_multiplier)
            elif protocol.get("candidates"):
                from research.protocol import ResearchProtocol, apply_candidate
                core_protocol = ResearchProtocol.from_dict(dict(protocol))
                selected = next((candidate for candidate in core_protocol.candidates
                                 if candidate.label == manifest.selected_label), None)
                if selected is None:
                    raise ValueError("Runtime must bind the selected preregistered core candidate.")
                expected_config = replace(apply_candidate(expected_base, selected), strategy_version=version)
            assert_runtime_matches(expected_config, manifest, verify_code=False)
            assert_code_identity(manifest.code_identity)
            if strategy["status"] == "frozen":
                if strategy["runtime_hash"] != manifest.runtime_hash:
                    raise ValueError("Frozen runtime is immutable; create a new version.")
                return
            if strategy["status"] != "draft":
                raise ValueError("Only a draft strategy version can be frozen.")
            conn.execute(
                update(strategy_versions)
                .where(strategy_versions.c.version == version)
                .values(status="frozen", frozen_at=now,
                        approved_by=approved_by.strip(), approved_at=now,
                        runtime_manifest_json=manifest.to_dict(),
                        runtime_hash=manifest.runtime_hash)
            )

    def load_frozen_runtime(
        self, version: str, *, verify_code: bool = True,
    ) -> FrozenRuntimeManifest:
        """Load complete admitted policy; old name-only freezes fail closed."""
        with self.engine.connect() as conn:
            row = self._strategy(conn, version)
        if row["status"] != "frozen" or not row["runtime_manifest_json"]:
            raise ValueError("Strategy has no verified frozen runtime manifest.")
        if not str(row["approved_by"] or "").strip() or row["approved_at"] is None:
            raise ValueError("Frozen runtime requires explicit human strategy-version approval.")
        manifest = FrozenRuntimeManifest.from_dict(row["runtime_manifest_json"])
        if manifest.runtime_hash != row["runtime_hash"]:
            raise ValueError("Stored frozen runtime hash does not match its manifest.")
        if manifest.config["strategy_version"] != version:
            raise ValueError("Frozen runtime configuration belongs to another version.")
        if verify_code:
            assert_code_identity(manifest.code_identity)
        return manifest

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

    def start_local_sim_clock(self, version: str) -> None:
        self.load_frozen_runtime(version)
        started_at = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            strategy = self._strategy(conn, version)
            if strategy["status"] != "frozen":
                raise ValueError(
                    "Local simulation clock can start only for a frozen strategy version."
                )
            admission = self._admitted_run(conn, version)
            if admission is None:
                raise ValueError("Local simulation requires an admitted AdmissionRun.")
            self._require_complete_final_trials(conn, admission)
            if strategy["local_sim_start"] is not None:
                return
            conn.execute(
                update(strategy_versions)
                .where(strategy_versions.c.version == version)
                .values(
                    local_sim_start=started_at,
                    paper_clock_restart_reason=None,
                )
            )

    def start_paper_clock(self, version: str) -> None:
        """Compatibility alias; caller-supplied historical timestamps are forbidden."""
        self.start_local_sim_clock(version)

    def restart_paper_clock(self, version: str, *, account_ref: str, reason: str) -> int:
        if not reason.strip():
            raise ValueError("Observation restart requires a reason; policy changes require a new version.")
        manifest = self.load_frozen_runtime(version)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with self.engine.begin() as conn:
            current = conn.execute(select(validation_runs).where(
                validation_runs.c.strategy_version == version,
                validation_runs.c.account_ref == account_ref,
                validation_runs.c.environment == "PAPER",
                validation_runs.c.execution_model == "REPLAY_OPEN",
                validation_runs.c.status == "active",
            )).mappings().one_or_none()
            if current is None:
                raise ValueError("No active validation stage exists for this account/version.")
            conn.execute(update(validation_runs).where(validation_runs.c.id == current["id"]).values(
                status="restarted", ended_at=now, restart_reason=reason.strip()))
            inserted = conn.execute(validation_runs.insert().values(
                strategy_version=version, runtime_hash=manifest.runtime_hash,
                account_ref=account_ref, environment="PAPER", execution_model="REPLAY_OPEN",
                started_at=now, status="active", restart_reason=reason.strip()))
            conn.execute(
                update(strategy_versions)
                .where(strategy_versions.c.version == version)
                .values(
                    local_sim_start=now,
                    paper_clock_restart_reason=reason.strip(),
                )
            )
            return int(inserted.inserted_primary_key[0])

    def start_admission(
        self,
        *,
        strategy_version: str,
        methodology: str,
        protocol_hash: str | None = None,
        results: Mapping[str, object] | None = None,
    ) -> int:
        results = dict(results or {})
        if bool(results.get("selection_uses_future_holdout", False)):
            raise ValueError("Future paper holdout cannot be used for candidate selection.")
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            strategy = self._strategy(conn, strategy_version)
            if strategy["status"] != "draft":
                raise ValueError("Admission can start only for a draft strategy version.")
            stored_protocol_hash = hashlib.sha256(
                json.dumps(
                    strategy["protocol_json"],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if protocol_hash is not None and protocol_hash != stored_protocol_hash:
                raise ValueError("Admission protocol hash does not match the strategy draft.")
            supplied_result_hash = results.get("protocol_hash")
            if (
                supplied_result_hash is not None
                and str(supplied_result_hash) != stored_protocol_hash
            ):
                raise ValueError("Admission results protocol hash does not match the draft.")
            results["protocol_hash"] = stored_protocol_hash
            running = conn.execute(
                select(admission_runs.c.id).where(
                    admission_runs.c.strategy_version == strategy_version,
                    admission_runs.c.methodology == methodology,
                    admission_runs.c.status == "running",
                )
            ).scalar_one_or_none()
            if running is not None:
                return int(running)
            if methodology == "nested_risk_extension_v1":
                protocol = strategy["protocol_json"]
                parent_hash = protocol.get("parent_runtime_hash")
                if not parent_hash or results.get("parent_runtime_hash") != parent_hash:
                    raise ValueError("Risk admission must bind the preregistered parent runtime.")
                parent = conn.execute(select(strategy_versions).where(
                    strategy_versions.c.runtime_hash == parent_hash,
                    strategy_versions.c.status == "frozen",
                )).mappings().one_or_none()
                if parent is None:
                    raise ValueError("Risk admission requires a real frozen parent runtime.")
                parent_manifest = FrozenRuntimeManifest.from_dict(parent["runtime_manifest_json"])
                if (protocol.get("base_config") != dict(parent_manifest.config)
                        or protocol.get("code_identity") != dict(parent_manifest.code_identity)):
                    raise ValueError("Risk protocol changes the frozen core configuration/code.")
                previous = conn.execute(select(admission_runs, dataset_snapshots.c.end_date).join(
                    strategy_versions, admission_runs.c.strategy_version == strategy_versions.c.version
                ).join(dataset_snapshots, strategy_versions.c.dataset_snapshot_id == dataset_snapshots.c.id).where(
                    admission_runs.c.parent_runtime_hash == parent_hash,
                    admission_runs.c.evidence_role == "post_core_holdout",
                )).mappings().all()
                for prior in previous:
                    prior_result = prior["results_json"] or {}
                    # An insufficient run exits before any protected trial is
                    # evaluated. Every other attempt consumes its holdout,
                    # including failed/abandoned attempts, not just winners.
                    if prior_result.get("status") == "INSUFFICIENT_EVIDENCE":
                        continue
                    if pd.Timestamp(protocol["holdout_start"]) <= pd.Timestamp(prior["end_date"]):
                        raise ValueError("This parent already consumed or reserved the requested holdout; use new observations.")
            result = conn.execute(
                admission_runs.insert().values(
                    strategy_version=strategy_version,
                    methodology=methodology,
                    status="running",
                    selection_uses_future_holdout=0,
                    results_json=_jsonable(results),
                    updated_at=now,
                    completed_at=None,
                    error_message=None,
                    runtime_hash=results.get("runtime_hash"),
                    parent_runtime_hash=results.get("parent_runtime_hash"),
                    evidence_role=results.get("evidence_role", "research_selector"),
                )
            )
            return int(result.inserted_primary_key[0])

    def save_admission_trial(
        self,
        admission_run_id: int,
        *,
        stage: str,
        fold_key: str,
        label: str,
        parameters: Mapping[str, object],
        folds: Sequence[Mapping[str, object]],
        status: str = "evaluated",
        score: object = None,
    ) -> int:
        if not stage.strip() or not fold_key.strip() or not label.strip():
            raise ValueError("Admission trial stage, fold_key, and label are required.")
        now = datetime.now(timezone.utc)
        with self.engine.begin() as conn:
            run = conn.execute(
                select(admission_runs).where(
                    admission_runs.c.id == int(admission_run_id)
                )
            ).mappings().one_or_none()
            if run is None:
                raise ValueError(f"Unknown AdmissionRun {admission_run_id}.")
            if run["status"] != "running":
                raise ValueError("Trials can be saved only while AdmissionRun is running.")
            row = {
                "admission_run_id": int(admission_run_id),
                "stage": stage.strip(),
                "fold_key": fold_key.strip(),
                "label": label.strip(),
                "parameters_json": _jsonable(dict(parameters)),
                "folds_json": _jsonable(list(folds)),
                "status": status,
                "score": _jsonable(score),
                "updated_at": now,
            }
            upsert(
                conn,
                parameter_trials,
                [row],
                index_elements=["admission_run_id", "stage", "fold_key", "label"],
                update_columns=[
                    "parameters_json",
                    "folds_json",
                    "status",
                    "score",
                    "updated_at",
                ],
            )
            conn.execute(
                update(admission_runs)
                .where(admission_runs.c.id == int(admission_run_id))
                .values(updated_at=now)
            )
            return int(
                conn.execute(
                    select(parameter_trials.c.id).where(
                        parameter_trials.c.admission_run_id == int(admission_run_id),
                        parameter_trials.c.stage == stage.strip(),
                        parameter_trials.c.fold_key == fold_key.strip(),
                        parameter_trials.c.label == label.strip(),
                    )
                ).scalar_one()
            )

    def finish_admission(
        self,
        admission_run_id: int,
        *,
        status: str,
        results: Mapping[str, object],
        error_message: str | None = None,
    ) -> None:
        status = status.lower()
        if status not in {"admitted", "rejected", "failed"}:
            raise ValueError("Admission final status must be admitted, rejected, or failed.")
        if bool(results.get("selection_uses_future_holdout", False)):
            raise ValueError("Future paper holdout cannot be used for candidate selection.")
        gates = results.get("gates")
        if status == "admitted" and (
            results.get("admitted") is not True
            or not isinstance(gates, Mapping)
            or not gates
            or not all(bool(value) for value in gates.values())
        ):
            raise ValueError("Admitted status requires explicit passing admission gates.")
        now = datetime.now(timezone.utc)
        normalized_results = _jsonable(dict(results))
        normalized_error = None if error_message is None else error_message.strip() or None
        with self.engine.begin() as conn:
            run = conn.execute(
                select(admission_runs).where(
                    admission_runs.c.id == int(admission_run_id)
                )
            ).mappings().one_or_none()
            if run is None:
                raise ValueError(f"Unknown AdmissionRun {admission_run_id}.")
            if run["status"] != "running":
                if (
                    run["status"] == status
                    and run["results_json"] == normalized_results
                    and run["error_message"] == normalized_error
                ):
                    return
                raise ValueError("Completed AdmissionRuns are immutable.")
            if status == "admitted":
                self._require_complete_final_trials(conn, run)
            conn.execute(
                update(admission_runs)
                .where(admission_runs.c.id == int(admission_run_id))
                .values(
                    status=status,
                    results_json=normalized_results,
                    updated_at=now,
                    completed_at=now,
                    error_message=normalized_error,
                    runtime_hash=results.get("runtime_hash", run["runtime_hash"]),
                    parent_runtime_hash=results.get("parent_runtime_hash", run["parent_runtime_hash"]),
                    evidence_role=results.get("evidence_role", run["evidence_role"]),
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
        """Compatibility wrapper over the resumable admission lifecycle."""
        admission_id = self.start_admission(
            strategy_version=strategy_version,
            methodology=methodology,
            results={
                "selection_uses_future_holdout": bool(
                    results.get("selection_uses_future_holdout", False)
                )
            },
        )
        for trial in trials:
            self.save_admission_trial(
                admission_id,
                stage=str(trial.get("stage", "final")),
                fold_key=str(trial.get("fold_key", "ALL")),
                label=str(trial["label"]),
                parameters=trial.get("parameters", {}),
                folds=trial.get("folds", []),
                status=str(trial.get("status", "evaluated")),
                score=trial.get("score"),
            )
        self.finish_admission(
            admission_id,
            status=status,
            results=results,
            error_message=(
                str(results.get("error_message"))
                if results.get("error_message") is not None
                else None
            ),
        )
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

    @staticmethod
    def _strategy(conn, version: str) -> Mapping[str, object]:
        row = conn.execute(
            select(strategy_versions).where(strategy_versions.c.version == version)
        ).mappings().one_or_none()
        if row is None:
            raise ValueError(f"Unknown strategy version {version}.")
        return row

    @staticmethod
    def _snapshot_is_actionable(snapshot: Mapping[str, object]) -> bool:
        quality = snapshot["quality_json"] or {}
        if quality.get("quality_model_version") != DATA_QUALITY_MODEL_VERSION:
            return False
        status = str(snapshot["status"])
        if status not in {"TRUSTED", "WARNING", "TRUSTED_WITH_EXCEPTIONS"}:
            return False
        content_hash = str(snapshot.get("content_hash") or "")
        raw_data_hash = str(quality.get("raw_data_hash") or "")
        def valid_hash(value: str) -> bool:
            return len(value) == 64 and all(
                character in "0123456789abcdef" for character in value.lower()
            )
        if not valid_hash(content_hash) or not valid_hash(raw_data_hash):
            return False
        if quality.get("status") != status or quality.get("content_hash") != content_hash:
            return False
        if quality.get("primary_source") != snapshot.get("primary_source"):
            return False
        if quality.get("secondary_source") != snapshot.get("secondary_source"):
            return False
        if quality.get("stale_sessions") != 0:
            return False
        if not quality.get("latest_session") or (
            quality.get("expected_session") != quality.get("latest_session")
        ):
            return False
        if status == "TRUSTED_WITH_EXCEPTIONS":
            decision_set_hash = str(snapshot.get("decision_set_hash") or "")
            if (
                not valid_hash(decision_set_hash)
                or quality.get("decision_set_hash") != decision_set_hash
                or not quality.get("source_snapshot_id")
                or not quality.get("adjudicated_issue_fingerprints")
            ):
                return False
        elif snapshot.get("decision_set_hash") or quality.get("decision_set_hash"):
            return False
        return True

    def _require_strategy_references(
        self,
        conn,
        *,
        universe_version: str,
        dataset_snapshot_id: int,
    ) -> None:
        universe = conn.execute(
            select(universe_versions).where(
                universe_versions.c.version == universe_version
            )
        ).mappings().one_or_none()
        if universe is None or universe["status"] != "approved":
            raise ValueError("Strategy requires a real approved universe version.")
        snapshot = conn.execute(
            select(dataset_snapshots).where(
                dataset_snapshots.c.id == int(dataset_snapshot_id)
            )
        ).mappings().one_or_none()
        if snapshot is None or not self._snapshot_is_actionable(snapshot):
            raise ValueError("Strategy requires an actionable dataset snapshot.")

    @staticmethod
    def _admitted_run(conn, version: str, *, admission_run_id: int | None = None):
        stmt = select(admission_runs).where(
            admission_runs.c.strategy_version == version,
            admission_runs.c.status == "admitted",
            admission_runs.c.completed_at.is_not(None),
            admission_runs.c.selection_uses_future_holdout == 0,
        )
        if admission_run_id is not None:
            stmt = stmt.where(admission_runs.c.id == int(admission_run_id))
        else:
            stmt = stmt.order_by(admission_runs.c.id.desc()).limit(1)
        return conn.execute(stmt).mappings().one_or_none()

    def _require_complete_final_trials(self, conn, run: Mapping[str, object]) -> None:
        strategy = self._strategy(conn, str(run["strategy_version"]))
        protocol = strategy["protocol_json"] or {}
        candidates = protocol.get("candidates")
        expected_labels: set[str] | None = None
        if isinstance(candidates, list) and candidates:
            expected_labels = {
                str(candidate.get("label"))
                for candidate in candidates
                if isinstance(candidate, Mapping) and candidate.get("label")
            }
            expected_count = len(candidates)
        else:
            expected_count = int(protocol.get("candidate_count", 135))
        final_rows = conn.execute(
            select(parameter_trials).where(
                parameter_trials.c.admission_run_id == int(run["id"]),
                parameter_trials.c.stage == "final_selection_summary",
            )
        ).mappings().all()
        labels = {str(row["label"]) for row in final_rows}
        if (
            expected_count <= 0
            or len(final_rows) != expected_count
            or len(labels) != expected_count
            or (expected_labels is not None and labels != expected_labels)
            or any(str(row["status"]).lower() != "evaluated" for row in final_rows)
        ):
            raise ValueError(
                "Admitted status requires the complete evaluated final candidate trial set."
            )
        statuses = conn.execute(
            select(parameter_trials.c.status).where(
                parameter_trials.c.admission_run_id == int(run["id"])
            )
        ).scalars().all()
        if any(
            str(value).lower() in {"pending", "running", "failed", "error", "blocked"}
            for value in statuses
        ):
            raise ValueError("Admission has pending or failed trial rows.")


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
