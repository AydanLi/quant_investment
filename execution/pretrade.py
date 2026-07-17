from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from data.models import DataQualityReport
from execution.models import (
    AccountSnapshot,
    Quote,
    ReconciliationResult,
)
from services.models import SignalDecision, SignalStatus


NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class PreTradeVerification:
    verified_at: datetime
    passed: bool
    reasons: tuple[str, ...]


def verify_pre_open(
    *,
    decision: SignalDecision,
    verified_at: datetime,
    account: AccountSnapshot,
    quality: DataQualityReport,
    reconciliation: ReconciliationResult,
    quotes: Mapping[str, Quote],
    risk_state: str,
    maximum_quote_age_seconds: float = 60.0,
) -> PreTradeVerification:
    now = pd.Timestamp(verified_at)
    if now.tzinfo is None:
        now = now.tz_localize(NEW_YORK)
    else:
        now = now.tz_convert(NEW_YORK)
    reasons: list[str] = []
    if decision.status != SignalStatus.ACTIONABLE:
        reasons.append("SIGNAL_NOT_ACTIONABLE")
    if now.date().isoformat() != decision.next_rebalance_session:
        reasons.append("WRONG_EXECUTION_SESSION")
    local_time = now.timetz().replace(tzinfo=None)
    if local_time < time(9, 30) or local_time >= time(16, 0):
        reasons.append("OUTSIDE_EXECUTION_WINDOW")
    if not quality.actionable:
        reasons.append("DATA_NOT_ACTIONABLE")
    if not reconciliation.matched:
        reasons.append("ACCOUNT_NOT_RECONCILED")
    if risk_state.upper() not in {"NORMAL", "WARNING", "DRIFT_REVIEW"}:
        reasons.append("RISK_HALTED")
    if account.settled_cash < -1e-9 or account.available_cash < -1e-9:
        reasons.append("NEGATIVE_CASH_OR_FINANCING")
    if account.buying_power > account.nav + 1e-6:
        reasons.append("LEVERAGE_DETECTED")
    if any(position.quantity < -1e-9 for position in account.positions.values()):
        reasons.append("SHORT_POSITION")
    for ticker, quote in quotes.items():
        captured = pd.Timestamp(quote.captured_at)
        if captured.tzinfo is None:
            captured = captured.tz_localize("UTC")
        age = abs((now.tz_convert("UTC") - captured.tz_convert("UTC")).total_seconds())
        if age > maximum_quote_age_seconds:
            reasons.append(f"STALE_QUOTE:{ticker}")
    return PreTradeVerification(
        verified_at=now.to_pydatetime(),
        passed=not reasons,
        reasons=tuple(dict.fromkeys(reasons)),
    )
