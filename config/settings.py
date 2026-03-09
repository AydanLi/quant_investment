from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Config:
    start_date: str = "2018-01-01"
    end_date: Optional[str] = None

    universe: List[str] = field(
        default_factory=lambda: [
            "SPY",
            "QQQ",
            "IWM",
            "TLT",
            "GLD",
            "XLE",
            "XLV",
            "BIL",
        ]
    )

    benchmark: str = "SPY"
    fear_gauge: str = "^VIX"

    rebalance_frequency: str = "M"
    top_n: int = 3
    min_momentum_threshold: float = 0.0

    weight_mom_20: float = 0.35
    weight_mom_60: float = 0.35
    weight_mom_120: float = 0.20
    weight_low_vol: float = 0.10

    vix_risk_off_threshold: float = 28.0
    vix_high_threshold: float = 22.0
    max_allowed_drawdown_from_200d: float = -0.08

    target_annual_vol: float = 0.12
    max_asset_weight: float = 0.40
    min_asset_weight: float = 0.00
    risk_off_cash_weight: float = 0.50
    trading_cost_bps: float = 5.0

    initial_capital: float = 100000.0