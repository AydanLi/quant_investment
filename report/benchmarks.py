from __future__ import annotations

import pandas as pd

from data.calendar import NyseCalendar


def build_benchmark_returns(prices: pd.DataFrame) -> dict[str, pd.Series]:
    result: dict[str, pd.Series] = {}
    returns = prices.pct_change(fill_method=None)
    if "BIL" in returns:
        result["BIL"] = returns["BIL"].dropna()
    if "SPY" in returns:
        result["SPY"] = returns["SPY"].dropna()
    if {"SPY", "IEF"}.issubset(returns.columns):
        pair = returns[["SPY", "IEF"]].dropna()
        calendar = NyseCalendar()
        weights = {"SPY": 0.60, "IEF": 0.40}
        values: dict[pd.Timestamp, float] = {}
        for session, row in pair.iterrows():
            daily = weights["SPY"] * float(row["SPY"]) + weights["IEF"] * float(row["IEF"])
            values[pd.Timestamp(session)] = daily
            gross_spy = weights["SPY"] * (1.0 + float(row["SPY"]))
            gross_ief = weights["IEF"] * (1.0 + float(row["IEF"]))
            total = gross_spy + gross_ief
            weights = {"SPY": gross_spy / total, "IEF": gross_ief / total}
            if calendar.is_month_end_session(session):
                weights = {"SPY": 0.60, "IEF": 0.40}
        result["60/40 SPY/IEF"] = pd.Series(values, name="60/40 SPY/IEF")
    return result
