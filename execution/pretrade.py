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
    if local_time >= time(9, 25):
        reasons.append("APPROVAL_DEADLINE_MISSED")
    if not quality.actionable:
        reasons.append("DATA_NOT_ACTIONABLE")
    if not reconciliation.matched:
        reasons.append("ACCOUNT_NOT_RECONCILED")
    if risk_state.upper() not in {"NORMAL", "WARNING", "DRIFT_REVIEW"} or account.risk_state.upper() not in {
        "NORMAL",
        "WARNING",
        "DRIFT_REVIEW",
    }:
        reasons.append("RISK_HALTED")
    if account.settled_cash < -1e-9 or account.available_cash < -1e-9:
        reasons.append("NEGATIVE_CASH_OR_FINANCING")
    if account.buying_power > account.nav + 1e-6:
        reasons.append("LEVERAGE_DETECTED")
    if any(position.quantity < -1e-9 for position in account.positions.values()):
        reasons.append("SHORT_POSITION")
    captured_account = pd.Timestamp(account.captured_at)
    if captured_account.tzinfo is None:
        captured_account = captured_account.tz_localize("UTC")
    account_age = (now.tz_convert("UTC") - captured_account.tz_convert("UTC")).total_seconds()
    if account_age < -1.0 or account_age > maximum_quote_age_seconds:
        reasons.append("STALE_ACCOUNT")
    if reconciliation.reconciled_at is None:
        reasons.append("STALE_RECONCILIATION")
    else:
        reconciled_at = pd.Timestamp(reconciliation.reconciled_at)
        if reconciled_at.tzinfo is None:
            reconciled_at = reconciled_at.tz_localize("UTC")
        reconciliation_age = (
            now.tz_convert("UTC") - reconciled_at.tz_convert("UTC")
        ).total_seconds()
        if reconciliation_age < -1.0 or reconciliation_age > maximum_quote_age_seconds:
            reasons.append("STALE_RECONCILIATION")
        if reconciled_at < captured_account:
            reasons.append("RECONCILIATION_PREDATES_ACCOUNT")
    required_tickers = {
        ticker
        for ticker in set(decision.target_weights).union(decision.current_weights)
        if abs(
            float(decision.target_weights.get(ticker, 0.0))
            - float(decision.current_weights.get(ticker, 0.0))
        )
        > 1e-9
    }
    missing_quotes = sorted(required_tickers - set(quotes))
    reasons.extend(f"MISSING_QUOTE:{ticker}" for ticker in missing_quotes)
    for ticker, quote in quotes.items():
        captured = pd.Timestamp(quote.captured_at)
        if captured.tzinfo is None:
            captured = captured.tz_localize("UTC")
        age = (now.tz_convert("UTC") - captured.tz_convert("UTC")).total_seconds()
        if age < -1.0 or age > maximum_quote_age_seconds:
            reasons.append(f"STALE_QUOTE:{ticker}")
    return PreTradeVerification(
        verified_at=now.to_pydatetime(),
        passed=not reasons,
        reasons=tuple(dict.fromkeys(reasons)),
    )
