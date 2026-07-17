from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from config.universe import CASH_ETF, INITIAL_ETF_UNIVERSE, SYNTHETIC_CASH


@dataclass
class Config:
    start_date: str = "2006-01-01"
    end_date: Optional[str] = None

    universe: List[str] = field(
        default_factory=lambda: list(INITIAL_ETF_UNIVERSE)
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
    max_asset_weight: float = 0.35
    min_asset_weight: float = 0.10
    risk_off_cash_weight: float = 0.50
    trading_cost_bps: float = 5.0
    slippage_bps: float = 2.0

    # Dynamic-factor parameters must pass the nested admission protocol.
    risk_model: str = "sample"
    ewma_half_life_days: int = 20
    pca_stress_multiplier: float = 1.50

    initial_capital: float = 10000.0

    cash_asset: str = CASH_ETF
    synthetic_cash_asset: str = SYNTHETIC_CASH
    execution_lag_sessions: int = 1
    signal_cutoff_time_et: str = "20:30"
    execution_time_et: str = "09:35"
    actionable_staleness_sessions: int = 0
    diagnostic_staleness_sessions: int = 1
    source_warning_bps: float = 5.0
    source_block_bps: float = 20.0
    unconfirmed_return_threshold: float = 0.10
    portfolio_drawdown_stop: float = 0.15
    daily_loss_halt: float = 0.05
    drift_warning_weight: float = 0.35
    drift_review_weight: float = 0.40
    operational_cash_buffer_pct: float = 0.005
    operational_cash_buffer_min: float = 25.0
    impact_model_adv_threshold: float = 0.001
    maximum_order_adv: float = 0.01
    impact_coefficient_bps: float = 10.0
    spread_block_bps: float = 20.0
    cost_scenarios_bps: Tuple[float, ...] = (2.0, 7.0, 20.0)
    strategy_version: str = "UNFROZEN"
    universe_version: str = "UV-001"
    universe_effective_date: str = "2026-07-17"
    historical_universe_integrity: bool = False
    daily_regime_overlay_enabled: bool = False
    daily_regime_overlay_admitted: bool = False

    # Persistence layer. SQLite by default; swap to Postgres/MySQL by changing
    # this URL and installing the matching driver, e.g.
    #   postgresql+psycopg2://user:pass@host/quant
    #   mysql+pymysql://user:pass@host/quant
    db_url: str = "sqlite:///quant_research.db"

    def validate_risk_constraints(self) -> None:
        """Validate settings required to build a fully invested portfolio.

        ``BIL`` is the system's cash-equivalent asset. Any capital that cannot
        be assigned without breaking risky-asset bounds remains in BIL. If BIL
        is unavailable, it remains in internal ``CASH_USD``; risk positions are
        never enlarged merely to force 100% ETF investment.
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

        if self.execution_lag_sessions != 1:
            raise ValueError("execution_lag_sessions must remain 1 for admitted runs.")
        if self.rebalance_frequency not in {"D", "W", "M"}:
            raise ValueError("rebalance_frequency must be D, W, or M.")
        if not 0.0 < self.portfolio_drawdown_stop < 1.0:
            raise ValueError("portfolio_drawdown_stop must be in (0, 1).")
        if not 0.0 < self.daily_loss_halt < 1.0:
            raise ValueError("daily_loss_halt must be in (0, 1).")
        if not 0.0 < self.drift_warning_weight <= 1.0:
            raise ValueError("drift_warning_weight must be in (0, 1].")
        if not self.drift_warning_weight <= self.drift_review_weight <= 1.0:
            raise ValueError(
                "drift_review_weight must be between drift_warning_weight and 1."
            )
        if self.source_warning_bps > self.source_block_bps:
            raise ValueError("source_warning_bps cannot exceed source_block_bps.")
        if self.impact_model_adv_threshold > self.maximum_order_adv:
            raise ValueError("impact_model_adv_threshold cannot exceed maximum_order_adv.")
        if self.impact_coefficient_bps < 0.0:
            raise ValueError("impact_coefficient_bps cannot be negative.")
        if len(set(self.universe)) != len(self.universe):
            raise ValueError("universe cannot contain duplicate tickers.")
        if self.daily_regime_overlay_enabled and not self.daily_regime_overlay_admitted:
            raise ValueError(
                "Daily regime overlay cannot be enabled before independent admission."
            )
