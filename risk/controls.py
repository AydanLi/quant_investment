from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

import pandas as pd

from config.settings import Config


class RiskStatus(StrEnum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    DRIFT_REVIEW = "DRIFT_REVIEW"
    DAILY_LOSS_HALT = "DAILY_LOSS_HALT"
    DRAWDOWN_HALTED = "DRAWDOWN_HALTED"


@dataclass(frozen=True)
class RiskEvent:
    status: RiskStatus
    code: str
    message: str
    trigger_value: float


class PortfolioRiskMonitor:
    def __init__(self, config: Config, initial_nav: float) -> None:
        self.config = config
        self.high_water = float(initial_nav)
        self.drawdown_halted = False

    def authorize_reentry(
        self,
        *,
        session: object,
        next_monthly_rebalance_session: object,
        nav: float,
        reconciliation_ok: bool,
        incident_recorded: bool,
        authorized_by: str,
    ) -> None:
        if not self.drawdown_halted:
            raise ValueError("Portfolio is not drawdown-halted.")
        if pd.Timestamp(session).normalize() < pd.Timestamp(
            next_monthly_rebalance_session
        ).normalize():
            raise ValueError("Re-entry cannot occur before the next monthly rebalance.")
        if not reconciliation_ok or not incident_recorded or not authorized_by.strip():
            raise ValueError("Re-entry requires reconciliation, incident record, and human authorization.")
        if nav <= 0.0:
            raise ValueError("Re-entry NAV must be positive.")
        self.drawdown_halted = False
        self.high_water = float(nav)

    def evaluate(
        self,
        *,
        nav: float,
        daily_return: float,
        weights: Mapping[str, float],
    ) -> tuple[RiskStatus, float, tuple[RiskEvent, ...]]:
        self.high_water = max(self.high_water, float(nav))
        drawdown = float(nav) / self.high_water - 1.0
        events: list[RiskEvent] = []

        if (
            drawdown <= -self.config.portfolio_drawdown_stop
            and not self.drawdown_halted
        ):
            self.drawdown_halted = True
            events.append(
                RiskEvent(
                    RiskStatus.DRAWDOWN_HALTED,
                    "PORTFOLIO_DRAWDOWN_STOP",
                    "Portfolio drawdown reached the manual liquidation trigger.",
                    drawdown,
                )
            )
        if daily_return <= -self.config.daily_loss_halt:
            events.append(
                RiskEvent(
                    RiskStatus.DAILY_LOSS_HALT,
                    "DAILY_LOSS_HALT",
                    "Daily portfolio loss reached the temporary halt threshold.",
                    daily_return,
                )
            )
        risky_weights = {
            ticker: weight
            for ticker, weight in weights.items()
            if ticker not in {self.config.cash_asset, self.config.synthetic_cash_asset}
        }
        max_risky_weight = max(risky_weights.values()) if risky_weights else 0.0
        # Drift thresholds are relative to the configured target cap. This
        # preserves the 35%/40% production rule while allowing deliberately
        # wider, explicitly exploratory test configurations to remain valid.
        warning_threshold = max(
            self.config.drift_warning_weight, self.config.max_asset_weight
        )
        review_threshold = min(
            1.0,
            max(
                self.config.drift_review_weight,
                warning_threshold
                + (
                    self.config.drift_review_weight
                    - self.config.drift_warning_weight
                ),
            ),
        )
        if max_risky_weight > review_threshold:
            events.append(
                RiskEvent(
                    RiskStatus.DRIFT_REVIEW,
                    "POSITION_DRIFT_REVIEW",
                    "A live-equivalent position drifted above the review threshold.",
                    max_risky_weight,
                )
            )
        elif max_risky_weight > warning_threshold:
            events.append(
                RiskEvent(
                    RiskStatus.WARNING,
                    "POSITION_DRIFT_WARNING",
                    "A live-equivalent position drifted above the target cap.",
                    max_risky_weight,
                )
            )

        if self.drawdown_halted:
            status = RiskStatus.DRAWDOWN_HALTED
        elif any(event.status == RiskStatus.DAILY_LOSS_HALT for event in events):
            status = RiskStatus.DAILY_LOSS_HALT
        elif any(event.status == RiskStatus.DRIFT_REVIEW for event in events):
            status = RiskStatus.DRIFT_REVIEW
        elif events:
            status = RiskStatus.WARNING
        else:
            status = RiskStatus.NORMAL
        return status, drawdown, tuple(events)
