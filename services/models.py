from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, time
from enum import StrEnum
from hashlib import sha256
import json
from typing import Mapping
from zoneinfo import ZoneInfo


class SignalStatus(StrEnum):
    DIAGNOSTIC = "DIAGNOSTIC"
    ACTIONABLE = "ACTIONABLE"
    BLOCKED = "BLOCKED"
    HALTED = "HALTED"


class PaperCycleStatus(StrEnum):
    PENDING = "PENDING"
    DRAFTED = "DRAFTED"
    APPROVED = "APPROVED"
    FILLED = "FILLED"
    RECONCILED = "RECONCILED"
    COMPLETED = "COMPLETED"
    MISSED = "MISSED"
    HALTED = "HALTED"


@dataclass(frozen=True)
class SignalDecision:
    strategy_version: str
    universe_version: str
    dataset_snapshot_id: int | None
    signal_session: str
    data_as_of: str
    generated_at: str
    next_rebalance_session: str
    status: SignalStatus
    regime: str
    target_weights: Mapping[str, float]
    current_weights: Mapping[str, float]
    weight_deltas: Mapping[str, float]
    dollar_deltas: Mapping[str, float]
    estimated_cost_dollars: float
    data_issues: tuple[Mapping[str, object], ...] = field(default_factory=tuple)
    risk_state: str = "NORMAL"
    block_reasons: tuple[str, ...] = field(default_factory=tuple)
    decision_id: int | None = None
    runtime_hash: str | None = None

    @property
    def actionable(self) -> bool:
        return self.status == SignalStatus.ACTIONABLE

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["status"] = self.status.value
        # Compatibility aliases for old read-only dashboards/repositories.
        payload["date"] = self.signal_session
        payload["weights"] = dict(self.target_weights)
        return payload

    def immutable_payload(self) -> dict[str, object]:
        payload = self.to_dict()
        payload.pop("decision_id", None)
        payload.pop("date", None)
        payload.pop("weights", None)
        return payload

    @property
    def decision_key(self) -> str:
        material = json.dumps(
            self.immutable_payload(), sort_keys=True, separators=(",", ":"), default=str
        )
        return sha256(material.encode("utf-8")).hexdigest()

    @property
    def approval_deadline(self) -> datetime:
        return datetime.combine(
            datetime.fromisoformat(self.next_rebalance_session).date(),
            time(9, 25),
            tzinfo=ZoneInfo("America/New_York"),
        )

    def with_id(self, decision_id: int) -> "SignalDecision":
        return replace(self, decision_id=int(decision_id))

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "SignalDecision":
        data = dict(payload)
        data.pop("date", None)
        data.pop("weights", None)
        data["status"] = SignalStatus(str(data["status"]))
        for name in (
            "target_weights",
            "current_weights",
            "weight_deltas",
            "dollar_deltas",
        ):
            data[name] = {
                str(key): float(value)
                for key, value in dict(data.get(name) or {}).items()
            }
        data["data_issues"] = tuple(data.get("data_issues") or ())
        data["block_reasons"] = tuple(data.get("block_reasons") or ())
        return cls(**data)


@dataclass(frozen=True)
class StoredSignalDecision:
    decision: SignalDecision
    paper_cycle_id: int
    cycle_status: PaperCycleStatus
    approval_deadline: datetime
    approved_at: datetime | None = None
    approved_by: str | None = None
    missed_reason: str | None = None
