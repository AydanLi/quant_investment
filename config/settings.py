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

    rebalance_frequency: str = "M"  # D / W / M
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
    slippage_bps: float = 2.0

    # Admitted risk model. ``sample`` preserves the pre-admission baseline.
    risk_model: str = "dynamic_factor"
    ewma_half_life_days: int = 20
    pca_stress_multiplier: float = 1.50

    initial_capital: float = 100000.0

    # Persistence layer. SQLite by default; swap to Postgres/MySQL by changing
    # this URL and installing the matching driver, e.g.
    #   postgresql+psycopg2://user:pass@host/quant
    #   mysql+pymysql://user:pass@host/quant
    db_url: str = "sqlite:///quant_research.db"

    def validate_risk_constraints(self) -> None:
        """Validate settings required to build a fully invested portfolio.

        ``BIL`` is the system's cash-equivalent asset.  When it is available,
        any capital that cannot be assigned without breaking the risky-asset
        cap can remain in BIL.  Without BIL, the configured number of selected
        assets must have enough aggregate capacity to reach 100% invested.
        """
        if not 0.0 < self.max_asset_weight <= 1.0:
            raise ValueError("max_asset_weight must be in the interval (0, 1].")
        if not 0.0 <= self.min_asset_weight <= self.max_asset_weight:
            raise ValueError(
                "min_asset_weight must be between 0 and max_asset_weight."
            )
        if not 0.0 <= self.risk_off_cash_weight <= 1.0:
            raise ValueError("risk_off_cash_weight must be between 0 and 1.")
        if self.top_n < 1:
            raise ValueError("top_n must be at least 1.")
        if self.trading_cost_bps < 0.0:
            raise ValueError("trading_cost_bps cannot be negative.")
        if self.slippage_bps < 0.0:
            raise ValueError("slippage_bps cannot be negative.")
        if self.risk_model not in {"sample", "dynamic_factor"}:
            raise ValueError("risk_model must be 'sample' or 'dynamic_factor'.")
        if self.ewma_half_life_days < 1:
            raise ValueError("ewma_half_life_days must be at least 1.")
        if self.pca_stress_multiplier < 1.0:
            raise ValueError("pca_stress_multiplier must be at least 1.0.")

        selectable_assets = min(self.top_n, len(self.universe))
        has_cash_equivalent = "BIL" in self.universe
        if (
            not has_cash_equivalent
            and selectable_assets * self.max_asset_weight < 1.0 - 1e-9
        ):
            raise ValueError(
                "Risk constraints are infeasible without BIL: top_n multiplied "
                "by max_asset_weight must be at least 1.0."
            )
