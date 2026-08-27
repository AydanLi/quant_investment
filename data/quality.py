from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from config.settings import Config
from data.calendar import NyseCalendar
from data.models import (
    CorporateAction,
    DataQualityDecision,
    DataQualityDisposition,
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


def raw_market_data_hash(
    primary: ProviderPayload,
    secondary: ProviderPayload | None,
) -> str:
    """Hash both immutable vendor payloads, independent of QA conclusions."""
    material = {
        "primary": dataset_content_hash(
            primary.bars, primary.actions, source=primary.source
        ),
        "secondary": (
            None
            if secondary is None
            else dataset_content_hash(
                secondary.bars, secondary.actions, source=secondary.source
            )
        ),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _action_key(action: CorporateAction) -> tuple[str, str, str]:
    action = action.normalized()
    return action.ticker, str(action.ex_date.date()), action.action_type


def _quality_snapshot_hash(
    *,
    status: DataQualityStatus,
    expected_session: str,
    latest_session: str | None,
    stale_sessions: int | None,
    issues: Sequence[DataQualityIssue],
    raw_data_hash: str,
) -> str:
    material = {
        "raw_data_hash": raw_data_hash,
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
                    "fingerprint": issue.fingerprint,
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


def apply_data_quality_decisions(
    report: DataQualityReport,
    decisions: Sequence[DataQualityDecision],
    *,
    source_snapshot_id: int,
    primary: ProviderPayload | None = None,
    secondary: ProviderPayload | None = None,
    config: Config | None = None,
) -> DataQualityReport:
    """Apply exact, immutable issue decisions without mutating the source report."""
    if not decisions:
        return report
    if report.status != DataQualityStatus.BLOCKED:
        raise ValueError("Data quality decisions can be applied only to a blocked snapshot.")
    if not report.raw_data_hash:
        raise ValueError("The source snapshot has no raw_data_hash; decisions cannot apply.")

    blocked = {
        issue.fingerprint: issue
        for issue in report.issues
        if issue.severity == QualitySeverity.BLOCK
    }
    accepted: set[str] = set()
    decision_hashes: set[str] = set()
    for decision in decisions:
        if decision.source_snapshot_id != int(source_snapshot_id):
            raise ValueError("Decision source_snapshot_id does not match the source snapshot.")
        if decision.raw_data_hash != report.raw_data_hash:
            raise ValueError("Decision raw_data_hash is stale for this snapshot.")
        issue = blocked.get(decision.issue_fingerprint)
        if issue is None:
            raise ValueError("Decision fingerprint does not identify a current blocking issue.")
        if (
            issue.code != decision.issue_code
            or issue.ticker != decision.ticker
            or issue.session is None
            or not decision.start_date <= issue.session <= decision.end_date
        ):
            raise ValueError("Decision code, ticker, or date scope does not match its issue.")
        decision_hashes.add(decision.decision_hash)
        if decision.disposition == DataQualityDisposition.ACCEPTED_EXCEPTION:
            accepted.add(issue.fingerprint)
            accepted.update(
                _bounded_close_normalization_matches(
                    report,
                    decision,
                    issue,
                    primary=primary,
                    secondary=secondary,
                    config=config,
                )
            )

    decision_set_hash = hashlib.sha256(
        json.dumps(sorted(decision_hashes), separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    unresolved = set(blocked).difference(accepted)
    status = (
        DataQualityStatus.BLOCKED
        if unresolved
        else DataQualityStatus.TRUSTED_WITH_EXCEPTIONS
    )
    content_hash = hashlib.sha256(
        json.dumps(
            {
                "source_content_hash": report.content_hash,
                "source_snapshot_id": int(source_snapshot_id),
                "raw_data_hash": report.raw_data_hash,
                "decision_set_hash": decision_set_hash,
                "status": status.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return replace(
        report,
        status=status,
        content_hash=content_hash,
        source_snapshot_id=int(source_snapshot_id),
        decision_set_hash=decision_set_hash,
        adjudicated_issue_fingerprints=tuple(sorted(accepted)),
    )


def _bounded_close_normalization_matches(
    report: DataQualityReport,
    decision: DataQualityDecision,
    source_issue: DataQualityIssue,
    *,
    primary: ProviderPayload | None,
    secondary: ProviderPayload | None,
    config: Config | None,
) -> set[str]:
    normalization = decision.normalization
    if normalization.get("kind") != "bounded_close_factor":
        return set()
    if primary is None or secondary is None or config is None:
        raise ValueError("Bounded close normalization requires both raw sources and config.")
    if decision.end_date != source_issue.session:
        raise ValueError("Close normalization must end at its corporate-action issue date.")
    transform_start = pd.Timestamp(normalization["start_date"])
    transform_end = pd.Timestamp(normalization["end_date"])
    event_date = pd.Timestamp(source_issue.session)
    if not 1 <= (event_date - transform_end).days <= 7:
        raise ValueError("Close normalization must stop immediately before its event.")
    if report.latest_session is None or pd.Timestamp(report.latest_session) <= event_date:
        raise ValueError("Close normalization requires post-event observations.")

    ticker = decision.ticker
    if ticker not in primary.bars or ticker not in secondary.bars:
        raise ValueError("Close normalization ticker is missing from a raw source.")
    left = _split_normalized_close(primary, ticker).copy()
    right = _split_normalized_close(secondary, ticker).copy()
    target = left if normalization["role"] == "primary" else right
    mask = (target.index >= transform_start) & (target.index <= transform_end)
    if not mask.any() or mask.all():
        raise ValueError("Close normalization must cover a finite pre-event subset.")
    factor = float(normalization["factor"])
    if normalization["operation"] == "multiply":
        target.loc[mask] *= factor
    else:
        target.loc[mask] /= factor

    aligned = pd.concat(
        [left.rename("primary"), right.rename("secondary")], axis=1, join="inner"
    ).dropna()
    difference_bps = (
        (aligned["primary"] / aligned["secondary"] - 1.0).abs() * 10000.0
    )
    derivative_issues = [
        issue
        for issue in report.issues
        if issue.severity == QualitySeverity.BLOCK
        and issue.code == "CROSS_SOURCE_CLOSE_MISMATCH"
        and issue.ticker == ticker
        and issue.session is not None
        and str(transform_start.date()) <= issue.session <= str(transform_end.date())
    ]
    if not derivative_issues:
        raise ValueError("Close normalization has no bounded derivative mismatch issues.")
    return {
        issue.fingerprint
        for issue in derivative_issues
        if pd.Timestamp(issue.session) in difference_bps.index
        and float(difference_bps.loc[pd.Timestamp(issue.session)])
        <= config.source_block_bps
    }


def _split_normalized_close(payload: ProviderPayload, ticker: str) -> pd.Series:
    """Return a distribution-unadjusted close on the current share basis.

    Tiingo preserves as-traded historical prices while Yahoo rewrites OHLC for
    later splits. Comparing either representation directly produces thousands
    of false discrepancies. The transformation is used only for QA; immutable
    vendor rows remain untouched.
    """
    close = payload.bars[ticker]["Close"].astype(float).copy()
    basis = payload.metadata.get(ticker, {}).get("price_split_basis")
    if basis != "as_traded":
        return close
    for raw_action in payload.actions:
        action = raw_action.normalized()
        if action.ticker != ticker.upper() or action.action_type != "split":
            continue
        if action.split_factor <= 0.0:
            continue
        close.loc[close.index < action.ex_date] /= action.split_factor
    return close


def _cash_on_current_share_basis(
    action: CorporateAction,
    all_actions: Iterable[CorporateAction],
    *,
    basis: object,
) -> float:
    value = float(action.cash_amount)
    if basis != "as_traded":
        return value
    for raw_split in all_actions:
        split = raw_split.normalized()
        if (
            split.ticker == action.ticker
            and split.action_type == "split"
            and split.ex_date > action.ex_date
            and split.split_factor > 0.0
        ):
            value /= split.split_factor
    return value


def _compare_actions(
    primary: ProviderPayload,
    secondary: ProviderPayload,
) -> list[DataQualityIssue]:
    primary_actions = tuple(item.normalized() for item in primary.actions)
    secondary_actions = tuple(item.normalized() for item in secondary.actions)
    primary_splits = tuple(
        item for item in primary_actions if item.action_type == "split"
    )
    secondary_splits = tuple(
        item for item in secondary_actions if item.action_type == "split"
    )
    primary_map = {_action_key(item): item for item in primary_actions}
    secondary_map = {_action_key(item): item for item in secondary_actions}
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
                    context={"action_type": key[2]},
                )
            )
            continue
        if left.action_type == "dividend":
            # Yahoo currently publishes ETF distributions to three decimals,
            # while Tiingo retains six. Differences no larger than one half of
            # Yahoo's last published decimal are representation rounding, not
            # an economic corporate-action conflict.
            left_cash = _cash_on_current_share_basis(
                left,
                primary_splits,
                basis=primary.metadata.get(left.ticker, {}).get(
                    "dividend_split_basis"
                ),
            )
            right_cash = _cash_on_current_share_basis(
                right,
                secondary_splits,
                basis=secondary.metadata.get(right.ticker, {}).get(
                    "dividend_split_basis"
                ),
            )
            # Add a sub-micro-dollar guard for binary floating representation
            # at the exact half-mill boundary; economically larger differences
            # remain blocked.
            mismatch = not np.isclose(
                left_cash, right_cash, rtol=0.0, atol=0.0005001
            )
        else:
            # Vendors can serialize a 3-for-1 action as 3 or 3.000003. A
            # one-part-per-million relative tolerance accepts representation
            # noise without accepting economically different split factors.
            mismatch = not np.isclose(
                left.split_factor,
                right.split_factor,
                rtol=1e-6,
                atol=1e-8,
            )
        if mismatch:
            issues.append(
                DataQualityIssue(
                    QualitySeverity.BLOCK,
                    "CORPORATE_ACTION_VALUE_MISMATCH",
                    f"Corporate action values disagree for {key}.",
                    ticker=key[0],
                    session=key[1],
                    context={"action_type": key[2]},
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
                "Actionable signals require an approved secondary publication source.",
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
                [
                    _split_normalized_close(primary, ticker).rename("primary"),
                    _split_normalized_close(secondary, ticker).rename("secondary"),
                ],
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
                        f"{ticker} split-normalized closes differ by {value:.2f} bp.",
                        ticker=ticker,
                        session=str(pd.Timestamp(session).date()),
                        value=float(value),
                    )
                )

            primary_returns = left["Close"].astype(float).pct_change(fill_method=None)
            secondary_returns = right["Close"].astype(float).pct_change(fill_method=None)
            # The 10% confirmation gate is an ETF rule. VIX is a volatility
            # index, where moves of this size are routine; it remains subject
            # to the CBOE/FRED VIXCLS close-difference checks above. FRED is an
            # independent publication path, but its stated underlying source
            # is still CBOE, so this is not represented as an independent
            # calculation of the index.
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
        issues.extend(_compare_actions(primary, secondary))

    if any(issue.severity == QualitySeverity.BLOCK for issue in issues):
        status = DataQualityStatus.BLOCKED
    elif issues:
        status = DataQualityStatus.WARNING
    else:
        status = DataQualityStatus.TRUSTED
    expected_text = str(expected.date())
    latest_text = None if latest is None else str(latest.date())
    raw_data_hash = raw_market_data_hash(primary, secondary)
    return DataQualityReport(
        status=status,
        primary_source=primary.source,
        secondary_source=None if secondary is None else secondary.source,
        expected_session=expected_text,
        latest_session=latest_text,
        stale_sessions=None if freshness is None else freshness.stale_sessions,
        issues=tuple(issues),
        raw_data_hash=raw_data_hash,
        content_hash=_quality_snapshot_hash(
            status=status,
            expected_session=expected_text,
            latest_session=latest_text,
            stale_sessions=None if freshness is None else freshness.stale_sessions,
            issues=issues,
            raw_data_hash=raw_data_hash,
        ),
    )
