from __future__ import annotations

import math
from typing import Mapping

import pandas as pd


GATE_LABELS = {
    "same_interval_same_costs": "Same interval and costs",
    "holdout_sharpe": "Holdout Sharpe improvement",
    "maximum_drawdown": "Maximum drawdown reduction",
    "rolling_windows": "Rolling-window effectiveness",
    "after_costs": "After-cost incremental return",
    "parameter_robustness": "Parameter robustness",
    "start_date_robustness": "Start-date robustness",
    "independent_information": "Independent information",
    "historical_universe_integrity": "Historical-universe integrity",
}


def format_timestamp_utc(value: object) -> str:
    timestamp = _utc_timestamp(value)
    return timestamp.strftime("%Y-%m-%d %H:%M UTC")


def format_result_age(value: object, *, now: object | None = None) -> str:
    generated_at = _utc_timestamp(value)
    current_time = _utc_timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    total_minutes = int((current_time - generated_at).total_seconds() // 60)
    if total_minutes < 0:
        return "Future-dated"
    if total_minutes < 60:
        return f"{total_minutes}m"
    hours, minutes = divmod(total_minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, remaining_hours = divmod(hours, 24)
    return f"{days}d {remaining_hours}h"


def build_allocation_comparison(
    positions: pd.DataFrame,
    target_weights: Mapping[str, object],
) -> pd.DataFrame:
    required_columns = {"symbol", "cost_basis_weight"}
    missing = sorted(required_columns - set(positions.columns))
    if missing:
        raise ValueError("positions are missing: " + ", ".join(missing))

    current = positions[["symbol", "cost_basis_weight"]].copy()
    current["symbol"] = current["symbol"].astype(str)
    current["cost_basis_weight"] = pd.to_numeric(
        current["cost_basis_weight"], errors="coerce"
    )
    if current["cost_basis_weight"].isna().any() or not current[
        "cost_basis_weight"
    ].map(math.isfinite).all():
        raise ValueError("current cost-basis weights must be finite")
    current_weights = current.groupby("symbol")["cost_basis_weight"].sum()

    target_values = {}
    for symbol, value in target_weights.items():
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("target weights must be finite")
        target_values[str(symbol)] = numeric

    symbols = sorted(set(current_weights.index) | set(target_values))
    rows = []
    for symbol in symbols:
        current_weight = float(current_weights.get(symbol, 0.0))
        target_weight = target_values.get(symbol, 0.0)
        delta = target_weight - current_weight
        rows.append(
            {
                "symbol": symbol,
                "current_weight": current_weight,
                "diagnostic_target": target_weight,
                "diagnostic_delta": delta,
                "absolute_delta": abs(delta),
            }
        )
    return pd.DataFrame(rows).sort_values(
        ["absolute_delta", "symbol"],
        ascending=[False, True],
        ignore_index=True,
    )


def build_admission_gate_table(admission: Mapping[str, object]) -> pd.DataFrame:
    gates = admission.get("gates")
    if not isinstance(gates, Mapping) or not gates:
        raise ValueError("admission gates are missing")
    if any(not isinstance(value, bool) for value in gates.values()):
        raise ValueError("admission gates must be boolean")

    ordered_keys = [key for key in GATE_LABELS if key in gates]
    ordered_keys.extend(sorted(set(gates) - set(ordered_keys)))
    return pd.DataFrame(
        [
            {
                "gate_key": key,
                "gate": GATE_LABELS.get(key, key.replace("_", " ").title()),
                "passed": gates[key],
                "status": "PASS" if gates[key] else "FAIL",
            }
            for key in ordered_keys
        ]
    )


def _utc_timestamp(value: object) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError("timestamp is missing")
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")
