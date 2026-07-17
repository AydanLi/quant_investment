from __future__ import annotations

from collections import defaultdict
from typing import Iterable

import numpy as np
import pandas as pd

from data.models import CorporateAction


_PRICE_COLUMNS = ("Open", "High", "Low", "Close")


def locally_adjust_ohlcv(
    frame: pd.DataFrame,
    actions: Iterable[CorporateAction],
) -> pd.DataFrame:
    """Backward-adjust raw OHLC with explicit dividends and split events.

    The newest bar remains on the raw-price scale. Historical factors are
    recomputed from the complete action history, so adding a dividend cannot
    create a discontinuity at a cache-batch boundary.
    """
    if frame.empty:
        return frame.copy()
    if "Close" not in frame:
        raise ValueError("Raw OHLCV data must contain Close.")

    result = frame.copy().sort_index()
    result.index = pd.DatetimeIndex(result.index).tz_localize(None).normalize()
    result = result[~result.index.duplicated(keep="last")]

    dividends: dict[pd.Timestamp, float] = defaultdict(float)
    splits: dict[pd.Timestamp, float] = defaultdict(lambda: 1.0)
    for raw_action in actions:
        action = raw_action.normalized()
        session = action.ex_date
        if action.status != "active":
            continue
        if action.action_type == "dividend":
            dividends[session] += action.cash_amount
        elif action.action_type == "split":
            splits[session] *= action.split_factor

    factors = pd.Series(1.0, index=result.index, dtype=float)
    for position in range(len(result.index) - 1, 0, -1):
        session = result.index[position]
        previous = result.index[position - 1]
        close = float(result.at[session, "Close"])
        dividend = float(dividends.get(session, 0.0))
        split = float(splits.get(session, 1.0))
        denominator = (close + dividend) * split
        if not np.isfinite(denominator) or denominator <= 0.0:
            raise ValueError(f"Invalid corporate action adjustment on {session.date()}.")
        factors.at[previous] = factors.at[session] * close / denominator

    for column in _PRICE_COLUMNS:
        if column in result:
            result[f"Adjusted {column}"] = result[column].astype(float) * factors
    result["Adjustment Factor"] = factors
    result["Dividend"] = pd.Series(dividends, dtype=float).reindex(result.index).fillna(0.0)
    result["Split Factor"] = pd.Series(splits, dtype=float).reindex(result.index).fillna(1.0)
    return result
