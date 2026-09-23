from __future__ import annotations

import pandas as pd
import numpy as np

from data.calendar import NyseCalendar


def runtime_is_verified(value: object) -> bool:
    return isinstance(value, (bool, np.bool_)) and bool(value)


def equity_chart(portfolio: pd.DataFrame, summary: dict) -> pd.DataFrame:
    """Use the saved opening NAV only when its versioned meaning is known."""
    curve = portfolio[["date", "equity"]].set_index("date").copy()
    opening_nav = summary.get("Start Equity")
    if (summary.get("Metric Schema Version") == 2
            and isinstance(opening_nav, (int, float))
            and np.isfinite(opening_nav) and opening_nav > 0 and not curve.empty):
        prior_session = NyseCalendar().previous_session(curve.index[0])
        curve = pd.concat([pd.DataFrame({"equity": [opening_nav]}, index=[prior_session]), curve])
        curve.index.name = "date"
    return curve


def eligible_comparison_runs(runs: pd.DataFrame) -> pd.DataFrame:
    """Only complete, currently verified, admitted metrics enter comparison."""
    if runs.empty:
        return runs.copy()
    def eligible(row: pd.Series) -> bool:
        summary = row.get("summary_json")
        return (row.get("admissible") == 1 and row.get("status") == "complete"
                and runtime_is_verified(row.get("runtime_verified"))
                and isinstance(summary, dict)
                and summary.get("Metric Schema Version") == 2
                and summary.get("Metric Status") == "FINAL")
    return runs.loc[runs.apply(eligible, axis=1)].copy()


def format_parameter_display_value(value: object) -> str:
    """Return an Arrow-safe string for a Dashboard parameter table cell."""
    if value is None:
        return ""

    if pd.api.types.is_scalar(value):
        try:
            if pd.isna(value):
                return ""
        except (TypeError, ValueError):
            pass

    return str(value)
