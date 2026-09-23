from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
import hashlib
import json
import math
from types import MappingProxyType
from typing import Mapping

import pandas as pd


DATA_QUALITY_MODEL_VERSION = "raw_ohlcv_actions_v2"


class QualitySeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    BLOCK = "BLOCK"


class DataQualityStatus(StrEnum):
    TRUSTED = "TRUSTED"
    TRUSTED_WITH_EXCEPTIONS = "TRUSTED_WITH_EXCEPTIONS"
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
    payment_date: pd.Timestamp | None = None
    payment_source: str | None = None

    def normalized(self) -> "CorporateAction":
        return CorporateAction(
            ticker=self.ticker.upper(),
            ex_date=pd.Timestamp(self.ex_date).tz_localize(None).normalize(),
            action_type=self.action_type.lower(),
            cash_amount=float(self.cash_amount),
            split_factor=float(self.split_factor),
            status=self.status.lower(),
            source=self.source.lower(),
            payment_date=(None if self.payment_date is None else pd.Timestamp(self.payment_date).tz_localize(None).normalize()),
            payment_source=None if self.payment_source is None else (self.payment_source.strip() or None),
        )

    @property
    def action_key(self) -> str:
        action = self.normalized()
        return f"{action.ticker}|{action.ex_date.date()}|{action.action_type}"

    @property
    def revision_hash(self) -> str:
        action = self.normalized()
        payload = {
            "key": action.action_key, "cash_amount": action.cash_amount,
            "split_factor": action.split_factor, "status": action.status,
            "payment_date": None if action.payment_date is None else str(action.payment_date.date()),
            # Evidence changes cash eligibility, so adding it after accrual
            # requires replay just like changing the stated payment date.
            "payment_source": action.payment_source,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class DataQualityIssue:
    severity: QualitySeverity
    code: str
    message: str
    ticker: str | None = None
    session: str | None = None
    value: float | None = None
    context: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", self.code.strip().upper())
        if self.ticker is not None:
            object.__setattr__(self, "ticker", self.ticker.strip().upper())
        if self.session is not None:
            object.__setattr__(self, "session", _date_text(self.session))
        if self.value is not None:
            object.__setattr__(self, "value", float(self.value))
        object.__setattr__(self, "context", _freeze_json(self.context))

    @property
    def fingerprint(self) -> str:
        """Stable issue identity; message wording is deliberately excluded."""
        return _sha256(
            {
                "severity": self.severity.value,
                "code": self.code,
                "ticker": self.ticker,
                "session": self.session,
                "value": None if self.value is None else format(self.value, ".17g"),
                "context": _thaw_json(self.context),
            }
        )


class DataQualityDisposition(StrEnum):
    ACCEPTED_EXCEPTION = "ACCEPTED_EXCEPTION"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class DataQualityDecision:
    """Immutable, single-issue adjudication tied to one raw snapshot."""

    source_snapshot_id: int
    issue_fingerprint: str
    issue_code: str
    ticker: str
    start_date: str
    end_date: str
    raw_data_hash: str
    disposition: DataQualityDisposition
    normalization: Mapping[str, object]
    evidence: Mapping[str, object]
    reason: str
    decided_by: str
    decided_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_snapshot_id", int(self.source_snapshot_id))
        object.__setattr__(self, "issue_fingerprint", self.issue_fingerprint.lower())
        object.__setattr__(self, "issue_code", self.issue_code.strip().upper())
        object.__setattr__(self, "ticker", self.ticker.strip().upper())
        object.__setattr__(self, "start_date", _date_text(self.start_date))
        object.__setattr__(self, "end_date", _date_text(self.end_date))
        object.__setattr__(self, "raw_data_hash", self.raw_data_hash.lower())
        object.__setattr__(self, "disposition", DataQualityDisposition(self.disposition))
        object.__setattr__(self, "normalization", _freeze_json(self.normalization))
        object.__setattr__(self, "evidence", _freeze_json(self.evidence))
        decided_at = pd.Timestamp(self.decided_at)
        if decided_at.tzinfo is None:
            raise ValueError("Data quality decisions require a timezone-aware decided_at.")
        object.__setattr__(self, "decided_at", decided_at.tz_convert("UTC").to_pydatetime())

        if self.source_snapshot_id <= 0:
            raise ValueError("Data quality decisions require a positive source snapshot id.")
        for name, value in (
            ("issue_fingerprint", self.issue_fingerprint),
            ("raw_data_hash", self.raw_data_hash),
        ):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{name} must be a 64-character SHA-256 hex digest.")
        if not self.issue_code or "*" in self.issue_code:
            raise ValueError("A decision requires one exact issue code.")
        if not self.ticker or self.ticker in {"*", "ALL"} or "*" in self.ticker:
            raise ValueError("Broad all-ticker data quality decisions are forbidden.")
        if self.start_date > self.end_date:
            raise ValueError("Decision start_date cannot be after end_date.")
        if not self.reason.strip() or not self.decided_by.strip():
            raise ValueError("A decision requires a reason and operator identity.")
        if not self.evidence:
            raise ValueError("A decision requires explicit evidence.")
        evidence_uris = self.evidence.get("uris", self.evidence.get("uri"))
        if isinstance(evidence_uris, str):
            evidence_uris = (evidence_uris,)
        if not evidence_uris or not all(
            isinstance(uri, str) and uri.strip() for uri in evidence_uris
        ):
            raise ValueError("Decision evidence requires at least one explicit URI.")
        if (
            self.disposition == DataQualityDisposition.ACCEPTED_EXCEPTION
            and not self.normalization
        ):
            raise ValueError("Accepted exceptions require an explicit normalization record.")
        if self.disposition == DataQualityDisposition.ACCEPTED_EXCEPTION:
            kind = self.normalization.get("kind")
            if kind not in {"issue_exception", "bounded_close_factor"}:
                raise ValueError("Unknown accepted-exception normalization kind.")
            if kind == "issue_exception" and self.start_date != self.end_date:
                raise ValueError("Single-issue exceptions require one exact session.")
            if kind == "bounded_close_factor":
                if self.issue_code not in {
                    "CORPORATE_ACTION_MISMATCH",
                    "CORPORATE_ACTION_VALUE_MISMATCH",
                }:
                    raise ValueError(
                        "A close-factor transform requires an exact corporate-action conflict."
                    )
                role = self.normalization.get("role")
                operation = self.normalization.get("operation")
                factor = float(self.normalization.get("factor", float("nan")))
                if not self.normalization.get("start_date") or not self.normalization.get(
                    "end_date"
                ):
                    raise ValueError("Close-factor start_date and end_date are required.")
                transform_start = _date_text(self.normalization["start_date"])
                transform_end = _date_text(self.normalization["end_date"])
                if role not in {"primary", "secondary"}:
                    raise ValueError("Close-factor role must be primary or secondary.")
                if operation not in {"multiply", "divide"}:
                    raise ValueError("Close-factor operation must be multiply or divide.")
                if not math.isfinite(factor) or not 0.1 <= factor <= 10.0:
                    raise ValueError("Close-factor must be finite and between 0.1 and 10.")
                if (
                    transform_start != self.start_date
                    or transform_end > self.end_date
                    or transform_start > transform_end
                    or transform_end >= self.end_date
                ):
                    raise ValueError(
                        "Close-factor dates must be a bounded pre-event subset of the decision scope."
                    )

    @property
    def decision_hash(self) -> str:
        return _sha256(self.to_dict(include_hash=False))

    def matches(self, issue: DataQualityIssue, *, raw_data_hash: str) -> bool:
        return (
            self.disposition == DataQualityDisposition.ACCEPTED_EXCEPTION
            and self.raw_data_hash == raw_data_hash
            and issue.fingerprint == self.issue_fingerprint
            and issue.code == self.issue_code
            and issue.ticker == self.ticker
            and issue.session is not None
            and self.start_date <= issue.session <= self.end_date
        )

    def to_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "source_snapshot_id": self.source_snapshot_id,
            "issue_fingerprint": self.issue_fingerprint,
            "issue_code": self.issue_code,
            "ticker": self.ticker,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "raw_data_hash": self.raw_data_hash,
            "disposition": self.disposition.value,
            "normalization_json": _thaw_json(self.normalization),
            "evidence_json": _thaw_json(self.evidence),
            "reason": self.reason.strip(),
            "decided_by": self.decided_by.strip(),
            "decided_at": self.decided_at.isoformat(),
        }
        if include_hash:
            payload["decision_hash"] = self.decision_hash
        return payload

    @classmethod
    def from_record(cls, row: Mapping[str, object]) -> "DataQualityDecision":
        decided_at = pd.Timestamp(row["decided_at"])
        if decided_at.tzinfo is None:
            decided_at = decided_at.tz_localize("UTC")
        return cls(
            source_snapshot_id=int(row["source_snapshot_id"]),
            issue_fingerprint=str(row["issue_fingerprint"]),
            issue_code=str(row["issue_code"]),
            ticker=str(row["ticker"]),
            start_date=str(row["start_date"]),
            end_date=str(row["end_date"]),
            raw_data_hash=str(row["raw_data_hash"]),
            disposition=DataQualityDisposition(str(row["disposition"])),
            normalization=row.get("normalization_json") or {},
            evidence=row.get("evidence_json") or {},
            reason=str(row["reason"]),
            decided_by=str(row["decided_by"]),
            decided_at=decided_at.to_pydatetime(),
        )


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
    raw_data_hash: str | None = None
    source_snapshot_id: int | None = None
    decision_set_hash: str | None = None
    adjudicated_issue_fingerprints: tuple[str, ...] = field(default_factory=tuple)
    quality_model_version: str | None = None

    @property
    def actionable(self) -> bool:
        # Warnings remain usable only after the current QA has examined the
        # payload. Legacy TRUSTED statuses cannot bypass newly added checks.
        return (
            self.status != DataQualityStatus.BLOCKED
            and self.stale_sessions == 0
            and self.quality_model_version == DATA_QUALITY_MODEL_VERSION
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "primary_source": self.primary_source,
            "secondary_source": self.secondary_source,
            "expected_session": self.expected_session,
            "latest_session": self.latest_session,
            "stale_sessions": self.stale_sessions,
            "issues": [
                {
                    "severity": issue.severity.value,
                    "code": issue.code,
                    "message": issue.message,
                    "ticker": issue.ticker,
                    "session": issue.session,
                    "value": issue.value,
                    "context": _thaw_json(issue.context),
                    "fingerprint": issue.fingerprint,
                }
                for issue in self.issues
            ],
            "content_hash": self.content_hash,
            "raw_data_hash": self.raw_data_hash,
            "source_snapshot_id": self.source_snapshot_id,
            "decision_set_hash": self.decision_set_hash,
            "quality_model_version": self.quality_model_version,
            "adjudicated_issue_fingerprints": list(
                self.adjudicated_issue_fingerprints
            ),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "DataQualityReport":
        issues = tuple(
            DataQualityIssue(
                severity=QualitySeverity(str(item["severity"])),
                code=str(item["code"]),
                message=str(item["message"]),
                ticker=None if item.get("ticker") is None else str(item["ticker"]),
                session=None if item.get("session") is None else str(item["session"]),
                value=None if item.get("value") is None else float(item["value"]),
                context=item.get("context") or {},
            )
            for item in payload.get("issues", ())
        )
        return cls(
            status=DataQualityStatus(str(payload["status"])),
            primary_source=str(payload["primary_source"]),
            secondary_source=(
                None
                if payload.get("secondary_source") is None
                else str(payload["secondary_source"])
            ),
            expected_session=(
                None
                if payload.get("expected_session") is None
                else str(payload["expected_session"])
            ),
            latest_session=(
                None
                if payload.get("latest_session") is None
                else str(payload["latest_session"])
            ),
            stale_sessions=(
                None
                if payload.get("stale_sessions") is None
                else int(payload["stale_sessions"])
            ),
            issues=issues,
            content_hash=(
                None if payload.get("content_hash") is None else str(payload["content_hash"])
            ),
            raw_data_hash=(
                None
                if payload.get("raw_data_hash") is None
                else str(payload["raw_data_hash"])
            ),
            source_snapshot_id=(
                None
                if payload.get("source_snapshot_id") is None
                else int(payload["source_snapshot_id"])
            ),
            decision_set_hash=(
                None
                if payload.get("decision_set_hash") is None
                else str(payload["decision_set_hash"])
            ),
            adjudicated_issue_fingerprints=tuple(
                str(value)
                for value in payload.get("adjudicated_issue_fingerprints", ())
            ),
            quality_model_version=(
                None if payload.get("quality_model_version") is None
                else str(payload["quality_model_version"])
            ),
        )


@dataclass(frozen=True)
class ProviderPayload:
    bars: Mapping[str, pd.DataFrame]
    actions: tuple[CorporateAction, ...]
    metadata: Mapping[str, Mapping[str, object]]
    source: str


def _date_text(value: object) -> str:
    return str(pd.Timestamp(value).tz_localize(None).date())


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in sorted(value.items())}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported decision JSON value: {type(value).__name__}")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _sha256(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()
