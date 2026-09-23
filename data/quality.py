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
    DATA_QUALITY_MODEL_VERSION,
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
        try:
            frame = frame[columns].astype(float)
        except (TypeError, ValueError):
            # Invalid vendor fields still need a reproducible blocked snapshot.
            frame = frame[columns].astype(str)
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
            **({"payment_date": str(item.normalized().payment_date.date()),
                "payment_source": item.normalized().payment_source}
               if item.normalized().payment_date is not None else {}),
        }
        for item in actions
    ]
    digest.update(
        json.dumps(
            sorted(canonical_actions, key=lambda item: (
                item["ticker"], item["date"], item["type"], item["cash"], item["split"],
                item["status"], item["source"], item.get("payment_date") or "", item.get("payment_source") or "",
            )),
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
    quality_model_version: str | None = None,
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
    if quality_model_version is not None:
        material["quality_model_version"] = quality_model_version
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
    return _split_normalized_field(payload, ticker, "Close")


def _split_normalized_field(payload: ProviderPayload, ticker: str, field: str) -> pd.Series:
    close = pd.to_numeric(payload.bars[ticker][field], errors="coerce").copy()
    close = close[~close.index.duplicated(keep="last")]
    basis = payload.metadata.get(ticker, {}).get("price_split_basis")
    if basis != "as_traded":
        return close
    for raw_action in payload.actions:
        action = raw_action.normalized()
        if action.ticker != ticker.upper() or action.action_type != "split" or action.status != "active":
            continue
        if action.split_factor <= 0.0:
            continue
        close.loc[close.index < action.ex_date] /= action.split_factor
    return close


def _validate_raw_payload(
    payload: ProviderPayload, *, required_tickers: Sequence[str],
    config: Config, calendar: NyseCalendar, role: str,
) -> list[DataQualityIssue]:
    """Validate each publication before cross-source intersections can hide rows."""
    issues: list[DataQualityIssue] = []
    for ticker in required_tickers:
        frame = payload.bars.get(ticker)
        if frame is None or frame.empty:
            continue  # The existing missing-provider issue supplies this evidence.
        if role == "primary" and ticker != config.fear_gauge and payload.metadata.get(ticker, {}).get("price_split_basis") == "current_share_basis":
            issues.append(DataQualityIssue(
                QualitySeverity.BLOCK, "PRIMARY_PRICE_BASIS_NOT_AS_TRADED",
                "Raw-share execution requires as-traded primary prices; current-share-basis history is validation-only.",
                ticker=ticker, context={"role": role},
            ))
        index = pd.DatetimeIndex(frame.index).tz_localize(None).normalize()
        def block(code: str, message: str, session: object | None = None, field: str | None = None) -> None:
            issues.append(DataQualityIssue(
                QualitySeverity.BLOCK, code, message, ticker=ticker,
                session=None if session is None or pd.isna(session) else str(pd.Timestamp(session).date()),
                context={"role": role, "field": field},
            ))
        if index.hasnans:
            block("INVALID_BAR_DATE", f"{role} {ticker} contains an invalid session date.")
            continue
        for date in index[index.duplicated()].unique():
            block("DUPLICATE_BAR_DATE", f"{role} {ticker} has duplicate daily bars.", date)
        sessions = calendar.sessions(index.min(), index.max())
        for date in sessions.difference(index):
            block("MISSING_TRADING_SESSION", f"{role} {ticker} is missing an in-range NYSE session.", date)
        if ticker != config.fear_gauge:
            for date in index.difference(sessions):
                block("NON_TRADING_SESSION", f"{role} {ticker} contains a non-NYSE bar.", date)
        # FRED redistributes VIX close only. It is not a tradable instrument and
        # its zero volume is meaningful, not an ETF liquidity observation.
        fields = ("Close",) if ticker == config.fear_gauge else ("Open", "High", "Low", "Close", "Volume")
        for field in fields:
            if field not in frame:
                block("REQUIRED_BAR_FIELD_MISSING", f"{role} {ticker} lacks {field}.", field=field)
                continue
            values = pd.to_numeric(frame[field], errors="coerce")
            invalid = ~np.isfinite(values) | (values.lt(0.) if field == "Volume" else values.le(0.))
            for date in frame.index[invalid]:
                block("INVALID_BAR_FIELD", f"{role} {ticker} has invalid {field}.", date, field)
        if ticker != config.fear_gauge and set(("Open", "High", "Low", "Close")).issubset(frame):
            prices = frame[["Open", "High", "Low", "Close"]].apply(pd.to_numeric, errors="coerce")
            inconsistent = (
                prices["Low"].gt(prices[["Open", "Close"]].min(axis=1))
                | prices["High"].lt(prices[["Open", "Close"]].max(axis=1))
                | prices["Low"].gt(prices["High"])
            )
            for date in frame.index[inconsistent]:
                block("INCONSISTENT_OHLC", f"{role} {ticker} violates daily OHLC bounds.", date, "OHLC")
    seen_actions: set[str] = set()
    for raw in payload.actions:
        action = raw.normalized()
        invalid = (pd.isna(action.ex_date) or not np.isfinite(action.cash_amount)
                   or action.cash_amount < 0. or not np.isfinite(action.split_factor)
                   or action.split_factor <= 0. or action.action_type not in {"dividend", "split"}
                   or (action.payment_date is not None and (pd.isna(action.payment_date) or action.payment_date < action.ex_date)))
        if invalid or action.action_key in seen_actions:
            issues.append(DataQualityIssue(
                QualitySeverity.BLOCK, "INVALID_CORPORATE_ACTION" if invalid else "DUPLICATE_CORPORATE_ACTION",
                f"{role} contains an invalid or duplicated corporate action.", ticker=action.ticker,
                session=None if pd.isna(action.ex_date) else str(action.ex_date.date()), context={"role": role},
            ))
        seen_actions.add(action.action_key)
    return issues


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
            and split.status == "active"
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
        if left.status != right.status:
            mismatch = True
        elif left.action_type == "dividend":
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
    issues.extend(_validate_raw_payload(primary, required_tickers=required_tickers, config=config, calendar=calendar, role="primary"))
    if secondary is not None:
        issues.extend(_validate_raw_payload(secondary, required_tickers=required_tickers, config=config, calendar=calendar, role="secondary"))

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
        close = pd.to_numeric(frame["Close"], errors="coerce")
        valid = frame.index[np.isfinite(close) & close.gt(0.)]
        if len(valid):
            latest_sessions.append(pd.Timestamp(valid.max()).tz_localize(None).normalize())

    latest = min(latest_sessions) if latest_sessions else None
    if any(session > expected for session in latest_sessions):
        issues.append(DataQualityIssue(
            QualitySeverity.BLOCK, "INCOMPLETE_OR_FUTURE_SESSION",
            "A primary source contains prices after the latest completed NYSE session.",
            session=str(max(latest_sessions).date()),
        ))
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
            if left is None or right is None or left.empty or right.empty or "Close" not in left or "Close" not in right:
                issues.append(
                    DataQualityIssue(
                        QualitySeverity.BLOCK,
                        "SECONDARY_BAR_MISSING",
                        f"Cannot cross-check {ticker} across both providers.",
                        ticker=ticker,
                    )
                )
                continue
            common_start = max(left.index.min(), right.index.min())
            comparison_dates = left.index.union(right.index)
            for session in comparison_dates[comparison_dates >= common_start]:
                if session not in left.index or session not in right.index:
                    issues.append(DataQualityIssue(
                        QualitySeverity.BLOCK, "CROSS_SOURCE_SESSION_MISSING",
                        f"{ticker} has no matching session across both providers.",
                        ticker=ticker, session=str(pd.Timestamp(session).date()),
                    ))
            if ticker != config.fear_gauge and "Open" in left and "Open" in right:
                opens = pd.concat([
                    _split_normalized_field(primary, ticker, "Open").rename("primary"),
                    _split_normalized_field(secondary, ticker, "Open").rename("secondary"),
                ], axis=1).dropna()
                open_difference = (opens["primary"] / opens["secondary"] - 1.).abs() * 10000.
                for session, value in open_difference[open_difference > config.source_warning_bps].items():
                    if not np.isfinite(value):
                        continue  # The individual-field validation already blocks this input.
                    issues.append(DataQualityIssue(
                        QualitySeverity.BLOCK if value > config.source_block_bps else QualitySeverity.WARNING,
                        "CROSS_SOURCE_OPEN_MISMATCH", f"{ticker} split-normalized opens differ by {value:.2f} bp.",
                        ticker=ticker, session=str(pd.Timestamp(session).date()), value=float(value),
                        context={"field": "Open"},
                    ))
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
                if not np.isfinite(value):
                    continue  # Invalid individual fields already have blocking evidence.
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

            primary_close = pd.to_numeric(left["Close"], errors="coerce")
            secondary_close = pd.to_numeric(right["Close"], errors="coerce")
            primary_returns = primary_close[~primary_close.index.duplicated(keep="last")].pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan)
            secondary_returns = secondary_close[~secondary_close.index.duplicated(keep="last")].pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan)
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
        quality_model_version=DATA_QUALITY_MODEL_VERSION,
        content_hash=_quality_snapshot_hash(
            status=status,
            expected_session=expected_text,
            latest_session=latest_text,
            stale_sessions=None if freshness is None else freshness.stale_sessions,
            issues=issues,
            raw_data_hash=raw_data_hash,
            quality_model_version=DATA_QUALITY_MODEL_VERSION,
        ),
    )
