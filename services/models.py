from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Mapping


class SignalStatus(StrEnum):
    DIAGNOSTIC = "DIAGNOSTIC"
    ACTIONABLE = "ACTIONABLE"
    BLOCKED = "BLOCKED"
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
