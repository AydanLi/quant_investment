from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd

from config.universe import SYNTHETIC_CASH


ASSET_CLASS = {
    **{ticker: "US_EQUITY" for ticker in ("SPY", "QQQ", "IWM", "MDY")},
    **{ticker: "INTERNATIONAL_EQUITY" for ticker in ("EFA", "EEM")},
    **{ticker: "FIXED_INCOME" for ticker in ("SHY", "IEF", "TLT", "TIP", "LQD", "HYG")},
    **{ticker: "REAL_ASSET" for ticker in ("GLD", "DBC", "VNQ")},
    **{ticker: "US_SECTOR_EQUITY" for ticker in ("XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY")},
    "BIL": "CASH_EQUIVALENT",
    SYNTHETIC_CASH: "CASH",
}


@dataclass(frozen=True)
class ExposureReport:
    risky_exposure: float
    cash_exposure: float
    largest_risky_position: float
    largest_asset_class_exposure: float
    asset_class_exposures: Mapping[str, float]
    correlation_concentration: float
    effective_independent_bets: float


def analyze_exposure(
    weights: Mapping[str, float],
    asset_returns: pd.DataFrame | None = None,
) -> ExposureReport:
    classes: dict[str, float] = {}
    risky: dict[str, float] = {}
    for ticker, raw_weight in weights.items():
        weight = max(float(raw_weight), 0.0)
        label = ASSET_CLASS.get(ticker, "OTHER")
        classes[label] = classes.get(label, 0.0) + weight
        if label not in {"CASH", "CASH_EQUIVALENT"}:
            risky[ticker] = weight
    cash = classes.get("CASH", 0.0) + classes.get("CASH_EQUIVALENT", 0.0)
    risky_total = sum(risky.values())
    risky_classes = {
        key: value
        for key, value in classes.items()
        if key not in {"CASH", "CASH_EQUIVALENT"}
    }
    concentration = float("nan")
    effective_bets = float("nan")
    if asset_returns is not None and risky_total > 0.0:
        tickers = [ticker for ticker in risky if ticker in asset_returns.columns]
        history = asset_returns[tickers].tail(252).dropna()
        if tickers and len(history) >= 20:
            correlation = history.corr().fillna(0.0).to_numpy(dtype=float)
            np.fill_diagonal(correlation, 1.0)
            vector = np.asarray([risky[ticker] for ticker in tickers], dtype=float)
            vector /= vector.sum()
            concentration = float(vector @ correlation @ vector)
            if concentration > 0.0:
                effective_bets = float(1.0 / concentration)
    return ExposureReport(
        risky_exposure=risky_total,
        cash_exposure=cash,
        largest_risky_position=max(risky.values(), default=0.0),
        largest_asset_class_exposure=max(risky_classes.values(), default=0.0),
        asset_class_exposures=classes,
        correlation_concentration=concentration,
        effective_independent_bets=effective_bets,
    )
