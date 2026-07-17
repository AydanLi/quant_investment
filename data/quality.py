from __future__ import annotations

import hashlib
import json
from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from config.settings import Config
from data.calendar import NyseCalendar
from data.models import (
    CorporateAction,
    DataQualityIssue,
    DataQualityReport,
    DataQualityStatus,
    ProviderPayload,
    QualitySeverity,
)


def dataset_content_hash(
    bars: Mapping[str, pd.DataFrame],
    actions: Iterable[CorporateAction],
    *,
    source: str | None = None,
) -> str:
    digest = hashlib.sha256()
    if source:
        digest.update(source.lower().encode("utf-8"))
    for ticker in sorted(bars):
        frame = bars[ticker].copy().sort_index()
        frame.index = pd.DatetimeIndex(frame.index).tz_localize(None).normalize()
        columns = [
            column for column in ("Open", "High", "Low", "Close", "Volume")
            if column in frame
        ]
        frame = frame[columns].astype(float)
        digest.update(ticker.encode("utf-8"))
        digest.update(
            pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes()
        )
    canonical_actions = [
        {
            "ticker": item.normalized().ticker,
            "date": str(item.normalized().ex_date.date()),
            "type": item.normalized().action_type,
            "cash": item.normalized().cash_amount,
            "split": item.normalized().split_factor,
            "status": item.normalized().status,
            "source": item.normalized().source,
        }
        for item in actions
    ]
    digest.update(
        json.dumps(
            sorted(canonical_actions, key=lambda item: tuple(item.values())),
            sort_keys=True,
        ).encode("utf-8")
    )
    return digest.hexdigest()


def _action_key(action: CorporateAction) -> tuple[str, str, str]:
    action = action.normalized()
    return action.ticker, str(action.ex_date.date()), action.action_type


def _quality_snapshot_hash(
    primary: ProviderPayload,
    secondary: ProviderPayload | None,
    *,
    status: DataQualityStatus,
    expected_session: str,
    latest_session: str | None,
    stale_sessions: int | None,
    issues: Sequence[DataQualityIssue],
) -> str:
    material = {
        "primary_hash": dataset_content_hash(
            primary.bars, primary.actions, source=primary.source
        ),
        "secondary_hash": (
            None
            if secondary is None
            else dataset_content_hash(
                secondary.bars, secondary.actions, source=secondary.source
            )
        ),
        "status": status.value,
        "expected_session": expected_session,
        "latest_session": latest_session,
        "stale_sessions": stale_sessions,
        "issues": sorted(
            [
                {
                    "severity": issue.severity.value,
                    "code": issue.code,
                    "ticker": issue.ticker,
                    "session": issue.session,
                    "value": issue.value,
                    "message": issue.message,
                }
                for issue in issues
            ],
            key=lambda item: (
                item["code"],
                item["ticker"] or "",
                item["session"] or "",
                float(item["value"] or 0.0),
            ),
        ),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _compare_actions(
    primary: Iterable[CorporateAction],
    secondary: Iterable[CorporateAction],
) -> list[DataQualityIssue]:
    primary_map = {_action_key(item): item.normalized() for item in primary}
    secondary_map = {_action_key(item): item.normalized() for item in secondary}
    issues: list[DataQualityIssue] = []
    for key in sorted(set(primary_map).union(secondary_map)):
        left = primary_map.get(key)
        right = secondary_map.get(key)
        if left is None or right is None:
            issues.append(
                DataQualityIssue(
                    QualitySeverity.BLOCK,
                    "CORPORATE_ACTION_MISMATCH",
                    f"Corporate action {key} exists in only one provider.",
                    ticker=key[0],
                    session=key[1],
                )
            )
            continue
        if left.action_type == "dividend":
            # Yahoo currently publishes ETF distributions to three decimals,
            # while Tiingo retains six. Differences no larger than one half of
            # Yahoo's last published decimal are representation rounding, not
            # an economic corporate-action conflict.
            mismatch = not np.isclose(
                left.cash_amount, right.cash_amount, rtol=0.0, atol=0.0005
            )
        else:
            mismatch = not np.isclose(left.split_factor, right.split_factor, rtol=0.0, atol=1e-8)
        if mismatch:
            issues.append(
                DataQualityIssue(
                    QualitySeverity.BLOCK,
                    "CORPORATE_ACTION_VALUE_MISMATCH",
                    f"Corporate action values disagree for {key}.",
                    ticker=key[0],
                    session=key[1],
                )
            )
    return issues


def assess_market_data_quality(
    primary: ProviderPayload,
    secondary: ProviderPayload | None,
    *,
    required_tickers: Sequence[str],
    config: Config,
    calendar: NyseCalendar | None = None,
    as_of: pd.Timestamp | None = None,
) -> DataQualityReport:
    calendar = calendar or NyseCalendar()
    issues: list[DataQualityIssue] = []
    expected = calendar.latest_completed_session(as_of)
    latest_sessions: list[pd.Timestamp] = []

    for ticker in required_tickers:
        frame = primary.bars.get(ticker)
        if frame is None or frame.empty or "Close" not in frame:
            issues.append(
                DataQualityIssue(
                    QualitySeverity.BLOCK,
                    "PRIMARY_BAR_MISSING",
                    f"Primary provider has no usable close for {ticker}.",
                    ticker=ticker,
                )
            )
            continue
        latest_sessions.append(pd.Timestamp(frame.index.max()).tz_localize(None).normalize())

    latest = min(latest_sessions) if latest_sessions else None
    freshness = calendar.freshness(latest, as_of=as_of) if latest is not None else None
    if freshness and freshness.stale_sessions > config.actionable_staleness_sessions:
        severity = (
            QualitySeverity.WARNING
            if freshness.stale_sessions <= config.diagnostic_staleness_sessions
            else QualitySeverity.BLOCK
        )
        issues.append(
            DataQualityIssue(
                severity,
                "STALE_DATA",
                f"Market data is {freshness.stale_sessions} NYSE session(s) stale.",
                session=str(freshness.latest_data_session.date()),
                value=float(freshness.stale_sessions),
            )
        )

    if secondary is None:
        issues.append(
            DataQualityIssue(
                QualitySeverity.BLOCK,
                "SECONDARY_SOURCE_MISSING",
                "Actionable signals require an independent secondary source.",
            )
        )
    else:
        for ticker in required_tickers:
            left = primary.bars.get(ticker)
            right = secondary.bars.get(ticker)
            if left is None or right is None or left.empty or right.empty:
                issues.append(
                    DataQualityIssue(
                        QualitySeverity.BLOCK,
                        "SECONDARY_BAR_MISSING",
                        f"Cannot cross-check {ticker} across both providers.",
                        ticker=ticker,
                    )
                )
                continue
            aligned = pd.concat(
                [left["Close"].rename("primary"), right["Close"].rename("secondary")],
                axis=1,
                join="inner",
            ).dropna()
            if aligned.empty:
                issues.append(
                    DataQualityIssue(
                        QualitySeverity.BLOCK,
                        "NO_OVERLAPPING_CLOSE",
                        f"No overlapping raw close exists for {ticker}.",
                        ticker=ticker,
                    )
                )
                continue
            difference_bps = (
                (aligned["primary"] / aligned["secondary"] - 1.0).abs() * 10000.0
            )
            for session, value in difference_bps[difference_bps > config.source_warning_bps].items():
                severity = (
                    QualitySeverity.BLOCK
                    if value > config.source_block_bps
                    else QualitySeverity.WARNING
                )
                issues.append(
                    DataQualityIssue(
                        severity,
                        "CROSS_SOURCE_CLOSE_MISMATCH",
                        f"{ticker} raw closes differ by {value:.2f} bp.",
                        ticker=ticker,
                        session=str(pd.Timestamp(session).date()),
                        value=float(value),
                    )
                )

            primary_returns = left["Close"].astype(float).pct_change(fill_method=None)
            secondary_returns = right["Close"].astype(float).pct_change(fill_method=None)
            # The 10% confirmation gate is an ETF rule. VIX is a volatility
            # index, where moves of this size are routine; it remains subject
            # to the independent CBOE/Yahoo close-difference checks above.
            extreme = (
                pd.Series(dtype=float)
                if ticker == config.fear_gauge
                else primary_returns[
                    primary_returns.abs() > config.unconfirmed_return_threshold
                ]
            )
            action_keys = {
                (item.normalized().ticker, item.normalized().ex_date)
                for item in tuple(primary.actions) + tuple(secondary.actions)
            }
            for session, value in extreme.items():
                normalized_session = pd.Timestamp(session).tz_localize(None).normalize()
                secondary_value = secondary_returns.get(session)
                source_confirmed = (
                    secondary_value is not None
                    and pd.notna(secondary_value)
                    and np.sign(float(secondary_value)) == np.sign(float(value))
                    and abs(float(secondary_value) - float(value)) <= 0.002
                )
                action_confirmed = (ticker.upper(), normalized_session) in action_keys
                issues.append(
                    DataQualityIssue(
                        QualitySeverity.WARNING if source_confirmed or action_confirmed else QualitySeverity.BLOCK,
                        "CONFIRMED_EXTREME_RETURN" if source_confirmed or action_confirmed else "UNCONFIRMED_EXTREME_RETURN",
                        (
                            f"{ticker} extreme raw return was independently confirmed."
                            if source_confirmed or action_confirmed
                            else f"{ticker} extreme raw return lacks source or corporate-action confirmation."
                        ),
                        ticker=ticker,
                        session=str(normalized_session.date()),
                        value=float(value),
                    )
                )
        issues.extend(_compare_actions(primary.actions, secondary.actions))

    if any(issue.severity == QualitySeverity.BLOCK for issue in issues):
        status = DataQualityStatus.BLOCKED
    elif issues:
        status = DataQualityStatus.WARNING
    else:
        status = DataQualityStatus.TRUSTED
    expected_text = str(expected.date())
    latest_text = None if latest is None else str(latest.date())
    return DataQualityReport(
        status=status,
        primary_source=primary.source,
        secondary_source=None if secondary is None else secondary.source,
        expected_session=expected_text,
        latest_session=latest_text,
        stale_sessions=None if freshness is None else freshness.stale_sessions,
        issues=tuple(issues),
        content_hash=_quality_snapshot_hash(
            primary,
            secondary,
            status=status,
            expected_session=expected_text,
            latest_session=latest_text,
            stale_sessions=None if freshness is None else freshness.stale_sessions,
            issues=issues,
        ),
    )
