from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class CostCalibration:
    fill_count: int
    median_shortfall_bps: float
    p95_shortfall_bps: float
    median_commission_bps: float
    suggested_all_in_bps: float
    coefficient_only_update: bool = True


def calibrate_cost_model(fills: pd.DataFrame) -> CostCalibration:
    required = {"implementation_shortfall_bps", "commission", "notional"}
    missing = required - set(fills.columns)
    if missing:
        raise ValueError(f"Cost calibration is missing fields: {sorted(missing)}.")
    sample = fills.dropna(subset=list(required)).copy()
    sample = sample[sample["notional"] > 0.0]
    if len(sample) < 30:
        raise ValueError("At least 30 paper fills are required to calibrate costs.")
    shortfall = sample["implementation_shortfall_bps"].astype(float).abs()
    commission_bps = (
        sample["commission"].astype(float) / sample["notional"].astype(float)
    ) * 10_000.0
    all_in = shortfall + commission_bps
    return CostCalibration(
        fill_count=len(sample),
        median_shortfall_bps=float(np.median(shortfall)),
        p95_shortfall_bps=float(np.percentile(shortfall, 95)),
        median_commission_bps=float(np.median(commission_bps)),
        suggested_all_in_bps=float(np.median(all_in)),
    )
