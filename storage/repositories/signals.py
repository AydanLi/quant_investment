"""Legacy run signals plus immutable executable signal decisions."""
from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any, Mapping

import pandas as pd
from sqlalchemy import and_, select

from data.calendar import NEW_YORK, NyseCalendar
from services.models import PaperCycleStatus, SignalDecision, StoredSignalDecision
from storage.repositories.base import BaseRepository
from storage.repositories.governance import GovernanceRepository
from research.runtime import FrozenRuntimeManifest, assert_code_identity
from storage.schema import (
    dataset_snapshots,
    order_intents,
    paper_cycles,
    signal_decisions,
    signals,
    strategy_versions,
    universe_versions,
)


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class SignalRepository(BaseRepository):
    @staticmethod
    def _is_sha256(value: object) -> bool:
        text = str(value or "")
        return len(text) == 64 and all(character in "0123456789abcdef" for character in text.lower())

    @classmethod
    def _validate_current_snapshot(
        cls,
        snapshot: Mapping[str, object],
        decision: SignalDecision,
    ) -> None:
        quality = dict(snapshot["quality_json"] or {})
        data_as_of = pd.Timestamp(decision.data_as_of).normalize().date().isoformat()
        if quality.get("latest_session") != data_as_of or quality.get(
            "expected_session"
        ) != data_as_of:
            raise ValueError(
                "SignalDecision snapshot sessions do not match decision data_as_of."
            )
        if quality.get("stale_sessions") != 0:
            raise ValueError("SignalDecision snapshot must have zero stale sessions.")
        if quality.get("status") != snapshot["status"]:
            raise ValueError("SignalDecision snapshot quality status is inconsistent.")
        if (
            quality.get("content_hash") != snapshot["content_hash"]
            or not cls._is_sha256(snapshot["content_hash"])
            or not cls._is_sha256(quality.get("raw_data_hash"))
        ):
            raise ValueError("SignalDecision snapshot content hashes are inconsistent.")
        if quality.get("decision_set_hash") != snapshot["decision_set_hash"]:
            raise ValueError("SignalDecision snapshot decision hash is inconsistent.")
        if snapshot["decision_set_hash"] is not None and not cls._is_sha256(
            snapshot["decision_set_hash"]
        ):
            raise ValueError("SignalDecision snapshot decision hash is invalid.")

    @staticmethod
    def _validate_actionable_timing(decision: SignalDecision) -> None:
        calendar = NyseCalendar()
        try:
            signal_session = pd.Timestamp(decision.signal_session).normalize()
            next_session = pd.Timestamp(decision.next_rebalance_session).normalize()
            data_as_of = pd.Timestamp(decision.data_as_of).normalize()
            generated = pd.Timestamp(decision.generated_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("SignalDecision contains an invalid session timestamp.") from exc
        if generated.tzinfo is None:
            raise ValueError("SignalDecision generated_at must include a UTC offset.")
        if not calendar.is_month_end_session(signal_session):
            raise ValueError("ACTIONABLE signal_session must be an NYSE month-end session.")
        expected_next = calendar.next_session(signal_session)
        if next_session != expected_next:
            raise ValueError("ACTIONABLE next_rebalance_session must be the next NYSE session.")
        if data_as_of != signal_session:
            raise ValueError("ACTIONABLE data_as_of must equal signal_session.")
        generated_et = generated.tz_convert(NEW_YORK)
        window_start = pd.Timestamp(
            datetime.combine(signal_session.date(), time(20, 30), tzinfo=NEW_YORK)
        )
        window_end = pd.Timestamp(decision.approval_deadline)
        if not window_start <= generated_et < window_end:
            raise ValueError(
                "ACTIONABLE generated_at must be between T 20:30 ET and T+1 09:25 ET."
            )

    def _require_local_sim_ready(
        self,
        conn,
        *,
        strategy_version: str,
        universe_version: str | None = None,
        dataset_snapshot_id: int | None = None,
        decision: SignalDecision | None = None,
    ) -> None:
        governance = GovernanceRepository(engine=self.engine)
        strategy = conn.execute(
            select(strategy_versions).where(
                strategy_versions.c.version == strategy_version
            )
        ).mappings().one_or_none()
        if strategy is None:
            raise ValueError(f"Unknown strategy version {strategy_version}.")
        if strategy["status"] != "frozen" or strategy["frozen_at"] is None:
            raise ValueError("Local paper requires a frozen strategy version.")
        if not str(strategy["approved_by"] or "").strip() or strategy["approved_at"] is None:
            raise ValueError("Local paper requires explicit human strategy-version approval.")
        if strategy["local_sim_start"] is None:
            raise ValueError("Local paper requires a started local simulation clock.")
        if not strategy["runtime_manifest_json"]:
            raise ValueError("Local paper requires a verified runtime manifest.")
        manifest = FrozenRuntimeManifest.from_dict(strategy["runtime_manifest_json"])
        if strategy["runtime_hash"] != manifest.runtime_hash:
            raise ValueError("Local paper runtime hash does not match its manifest.")
        assert_code_identity(manifest.code_identity)
        if decision is not None and decision.runtime_hash != manifest.runtime_hash:
            raise ValueError("SignalDecision runtime hash differs from the frozen strategy.")

        baseline_universe_version = str(strategy["universe_version"])
        baseline_snapshot_id = int(strategy["dataset_snapshot_id"])
        baseline_universe = conn.execute(
            select(universe_versions).where(
                universe_versions.c.version == baseline_universe_version
            )
        ).mappings().one_or_none()
        if (
            baseline_universe is None
            or baseline_universe["status"] != "approved"
            or baseline_universe["approved_at"] is None
            or not str(baseline_universe["approved_by"] or "").strip()
        ):
            raise ValueError(
                "Local paper requires an approved baseline universe with audit identity."
            )
        baseline_snapshot = conn.execute(
            select(dataset_snapshots).where(
                dataset_snapshots.c.id == baseline_snapshot_id
            )
        ).mappings().one_or_none()
        if baseline_snapshot is None or not governance._snapshot_is_actionable(
            baseline_snapshot
        ):
            raise ValueError(
                "Local paper requires an actionable strategy admission snapshot."
            )

        current_universe_version = universe_version or baseline_universe_version
        current_universe = conn.execute(
            select(universe_versions).where(
                universe_versions.c.version == current_universe_version
            )
        ).mappings().one_or_none()
        if (
            current_universe is None
            or current_universe["status"] != "approved"
            or current_universe["approved_at"] is None
            or not str(current_universe["approved_by"] or "").strip()
        ):
            raise ValueError(
                "SignalDecision requires an approved universe with audit identity."
            )
        if any(
            current_universe[name] != baseline_universe[name]
            for name in (
                "seed_tickers_json",
                "rules_json",
                "historical_universe_integrity",
            )
        ):
            raise ValueError(
                "SignalDecision universe changes the frozen universe policy."
            )
        if decision is not None and pd.Timestamp(
            current_universe["effective_date"]
        ).normalize() > pd.Timestamp(decision.signal_session).normalize():
            raise ValueError("SignalDecision universe is not yet effective.")

        current_snapshot_id = (
            int(dataset_snapshot_id)
            if dataset_snapshot_id is not None
            else baseline_snapshot_id
        )
        current_snapshot = conn.execute(
            select(dataset_snapshots).where(
                dataset_snapshots.c.id == current_snapshot_id
            )
        ).mappings().one_or_none()
        if current_snapshot is None or not governance._snapshot_is_actionable(
            current_snapshot
        ):
            raise ValueError(
                "SignalDecision requires an actionable current dataset snapshot."
            )
        if decision is not None:
            self._validate_current_snapshot(current_snapshot, decision)

        admission = governance._admitted_run(conn, strategy_version)
        if admission is None:
            raise ValueError("Local paper requires a completed ADMITTED run.")
        results = dict(admission["results_json"] or {})
        gates = results.get("gates")
        if (
            admission["error_message"] is not None
            or results.get("admitted") is not True
            or not isinstance(gates, Mapping)
            or not gates
            or not all(bool(value) for value in gates.values())
        ):
            raise ValueError("Local paper requires explicit passing admission gates.")
        governance._require_complete_final_trials(conn, admission)

        if decision is not None:
            if not decision.actionable:
                raise ValueError("Only ACTIONABLE decisions may enter local paper.")
            self._validate_actionable_timing(decision)

    def assert_local_sim_ready(
        self,
        strategy_version: str,
        *,
        universe_version: str | None = None,
        dataset_snapshot_id: int | None = None,
        decision: SignalDecision | None = None,
    ) -> None:
        with self.engine.connect() as conn:
            self._require_local_sim_ready(
                conn,
                strategy_version=strategy_version,
                universe_version=universe_version,
                dataset_snapshot_id=dataset_snapshot_id,
                decision=decision,
            )

    def save(self, run_id: int, latest_signal: Mapping[str, Any], *, connection=None) -> None:
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

        with self.transaction(connection) as conn:
            conn.execute(signals.insert(), rows)

    def get(self, run_id: int) -> pd.DataFrame:
        """The signal rows for a run, in insertion order."""
        stmt = signals.select().where(signals.c.run_id == run_id).order_by(signals.c.id)
        with self.engine.connect() as conn:
            return pd.read_sql(stmt, conn)

    def save_decision(
        self,
        decision: SignalDecision,
        *,
        environment: str = "PAPER",
    ) -> SignalDecision:
        """Insert once; the same strategy/session may never be silently replaced."""
        environment = environment.upper()
        if environment not in {"RESEARCH", "PAPER", "LIVE"}:
            raise ValueError("Unknown signal-decision environment.")
        if decision.dataset_snapshot_id is None:
            raise ValueError("An executable SignalDecision requires a dataset snapshot.")
        generated_at = pd.Timestamp(decision.generated_at)
        if generated_at.tzinfo is None:
            generated_at = generated_at.tz_localize("UTC")
        else:
            generated_at = generated_at.tz_convert("UTC")
        identity = and_(
            signal_decisions.c.environment == environment,
            signal_decisions.c.strategy_version == decision.strategy_version,
            signal_decisions.c.signal_session == decision.signal_session,
        )
        with self.engine.begin() as conn:
            if decision.actionable:
                self._require_local_sim_ready(
                    conn, strategy_version=decision.strategy_version,
                    universe_version=decision.universe_version,
                    dataset_snapshot_id=decision.dataset_snapshot_id, decision=decision,
                )
            existing = conn.execute(
                select(signal_decisions).where(identity)
            ).mappings().one_or_none()
            if existing is not None:
                if existing["decision_key"] != decision.decision_key:
                    raise ValueError(
                        "SignalDecision is immutable for an environment, strategy, and signal session."
                    )
                return decision.with_id(int(existing["id"]))
            inserted = conn.execute(
                signal_decisions.insert().values(
                    decision_key=decision.decision_key,
                    environment=environment,
                    strategy_version=decision.strategy_version,
                    universe_version=decision.universe_version,
                    dataset_snapshot_id=decision.dataset_snapshot_id,
                    signal_session=decision.signal_session,
                    data_as_of=decision.data_as_of,
                    generated_at=generated_at.to_pydatetime().replace(tzinfo=None),
                    next_rebalance_session=decision.next_rebalance_session,
                    status=decision.status.value,
                    runtime_hash=decision.runtime_hash,
                    regime=decision.regime,
                    decision_json=decision.immutable_payload(),
                )
            )
            return decision.with_id(int(inserted.inserted_primary_key[0]))

    def get_decision(self, decision_id: int) -> SignalDecision:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(signal_decisions).where(signal_decisions.c.id == decision_id)
            ).mappings().one_or_none()
        if row is None:
            raise KeyError(f"Unknown SignalDecision {decision_id}.")
        return SignalDecision.from_dict(row["decision_json"]).with_id(int(row["id"]))

    def ensure_paper_cycle(
        self,
        decision_id: int,
        *,
        recorded_at: datetime | None = None,
    ) -> int:
        decision = self.get_decision(decision_id)
        if not decision.actionable:
            raise ValueError("Only ACTIONABLE decisions may create a paper cycle.")
        self.assert_local_sim_ready(
            decision.strategy_version,
            universe_version=decision.universe_version,
            dataset_snapshot_id=decision.dataset_snapshot_id,
            decision=decision,
        )
        now = recorded_at or datetime.now(timezone.utc)
        now_utc = _as_aware_utc(now)
        deadline = decision.approval_deadline
        generated = pd.Timestamp(decision.generated_at)
        if generated.tzinfo is None:
            generated = generated.tz_localize("UTC")
        else:
            generated = generated.tz_convert("UTC")
        initial_status = (
            PaperCycleStatus.MISSED
            if now_utc >= deadline.astimezone(timezone.utc)
            or generated.to_pydatetime() >= deadline.astimezone(timezone.utc)
            else PaperCycleStatus.PENDING
        )
        missed_reason = (
            "DECISION_AFTER_0925_ET"
            if initial_status == PaperCycleStatus.MISSED
            else None
        )
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(paper_cycles.c.id).where(
                    paper_cycles.c.signal_decision_id == decision_id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return int(existing)
            inserted = conn.execute(
                paper_cycles.insert().values(
                    environment="PAPER",
                    strategy_version=decision.strategy_version,
                    signal_decision_id=decision_id,
                    signal_session=decision.signal_session,
                    execution_session=decision.next_rebalance_session,
                    approval_deadline=_utc_naive(deadline),
                    status=initial_status.value,
                    missed_reason=missed_reason,
                    updated_at=_utc_naive(now_utc),
                )
            )
            return int(inserted.inserted_primary_key[0])

    def get_stored_decision(self, decision_id: int) -> StoredSignalDecision:
        decision = self.get_decision(decision_id)
        with self.engine.connect() as conn:
            cycle = conn.execute(
                select(paper_cycles).where(
                    paper_cycles.c.signal_decision_id == decision_id
                )
            ).mappings().one_or_none()
            if cycle is None:
                raise KeyError(f"SignalDecision {decision_id} has no paper cycle.")
            approval = conn.execute(
                select(order_intents.c.approved_at, order_intents.c.approved_by)
                .where(
                    order_intents.c.paper_cycle_id == cycle["id"],
                    order_intents.c.approved_at.is_not(None),
                )
                .order_by(order_intents.c.approved_at)
                .limit(1)
            ).mappings().one_or_none()
        deadline = _as_aware_utc(cycle["approval_deadline"])
        approved_at = None
        approved_by = None
        if approval is not None:
            approved_at = _as_aware_utc(approval["approved_at"])
            approved_by = approval["approved_by"]
        return StoredSignalDecision(
            decision=decision,
            paper_cycle_id=int(cycle["id"]),
            cycle_status=PaperCycleStatus(cycle["status"]),
            approval_deadline=deadline,
            approved_at=approved_at,
            approved_by=approved_by,
            missed_reason=cycle["missed_reason"],
        )

    def transition_cycle(
        self,
        cycle_id: int,
        target: PaperCycleStatus,
        *,
        expected: tuple[PaperCycleStatus, ...],
        at: datetime | None = None,
        missed_reason: str | None = None,
    ) -> PaperCycleStatus:
        now = _utc_naive(at or datetime.now(timezone.utc))
        with self.engine.begin() as conn:
            current = conn.execute(
                select(paper_cycles.c.status).where(paper_cycles.c.id == cycle_id)
            ).scalar_one_or_none()
            if current is None:
                raise KeyError(f"Unknown paper cycle {cycle_id}.")
            current_status = PaperCycleStatus(current)
            if current_status == target:
                return target
            if current_status not in set(expected):
                raise ValueError(
                    f"Invalid paper-cycle transition {current_status.value} -> {target.value}."
                )
            values: dict[str, object] = {"status": target.value, "updated_at": now}
            if target == PaperCycleStatus.MISSED:
                values["missed_reason"] = missed_reason or "APPROVAL_DEADLINE_MISSED"
            result = conn.execute(
                paper_cycles.update()
                .where(
                    paper_cycles.c.id == cycle_id,
                    paper_cycles.c.status == current_status.value,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                raise RuntimeError("Concurrent paper-cycle state change detected.")
        return target

    def mark_executed(self, decision_id: int, *, at: datetime) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                signal_decisions.update()
                .where(
                    signal_decisions.c.id == decision_id,
                    signal_decisions.c.executed_at.is_(None),
                )
                .values(executed_at=_utc_naive(at))
            )
