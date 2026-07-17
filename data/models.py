from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Mapping

import pandas as pd


class QualitySeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    BLOCK = "BLOCK"


class DataQualityStatus(StrEnum):
    TRUSTED = "TRUSTED"
    WARNING = "WARNING"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class CorporateAction:
    ticker: str
    ex_date: pd.Timestamp
    action_type: str
    cash_amount: float = 0.0
    split_factor: float = 1.0
    status: str = "active"
    source: str = "unknown"

    def normalized(self) -> "CorporateAction":
        return CorporateAction(
            ticker=self.ticker.upper(),
            ex_date=pd.Timestamp(self.ex_date).tz_localize(None).normalize(),
            action_type=self.action_type.lower(),
            cash_amount=float(self.cash_amount),
            split_factor=float(self.split_factor),
            status=self.status.lower(),
            source=self.source.lower(),
        )


@dataclass(frozen=True)
class DataQualityIssue:
    severity: QualitySeverity
    code: str
    message: str
    ticker: str | None = None
    session: str | None = None
    value: float | None = None


@dataclass(frozen=True)
class DataQualityReport:
    status: DataQualityStatus
    primary_source: str
    secondary_source: str | None
    expected_session: str | None
    latest_session: str | None
    stale_sessions: int | None
    issues: tuple[DataQualityIssue, ...] = field(default_factory=tuple)
    content_hash: str | None = None

    @property
    def actionable(self) -> bool:
        # Warnings are surfaced to the operator but only BLOCKED conditions
        # prevent an order draft.  This preserves the documented 5 bp warning
        # versus 20 bp block distinction.
        return self.status != DataQualityStatus.BLOCKED and self.stale_sessions == 0

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["issues"] = [
            {**asdict(issue), "severity": issue.severity.value} for issue in self.issues
        ]
        return payload


@dataclass(frozen=True)
class ProviderPayload:
    bars: Mapping[str, pd.DataFrame]
    actions: tuple[CorporateAction, ...]
    metadata: Mapping[str, Mapping[str, object]]
    source: str
