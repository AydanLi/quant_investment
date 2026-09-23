from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

import pandas as pd

from config.settings import Config
from math import isfinite


def risk_state_projection(halt_reasons, drift_state: str = "NORMAL") -> str:
    for reason in ("RECONCILIATION_HALTED", "DRAWDOWN_HALTED", "DAILY_LOSS_HALTED", "VALUATION_HALTED", "EXECUTION_HALTED"):
        if reason in halt_reasons:
            return reason
    return drift_state


@dataclass(frozen=True)
class AccountRiskAssessment:
    high_water: float
    drawdown: float
    daily_return: float | None
    halt_reasons: tuple[str, ...]
    drift_state: str
    new_halts: tuple[str, ...]

    @property
    def state(self) -> str:
        return risk_state_projection(self.halt_reasons, self.drift_state)


def evaluate_account_risk(*, config: Config, nav: float, high_water: float,
                          previous_close_nav: float | None, weights: Mapping[str, float],
                          halt_reasons=()) -> AccountRiskAssessment:
    """Financial halts latch independently; valuation never clears another lock."""
    if not all(isfinite(x) and x > 0 for x in (nav, high_water)):
        raise ValueError("Risk NAV and high water must be finite and positive.")
    high_water = max(nav, high_water)
    drawdown = nav / high_water - 1
    daily_return = None
    before = set(halt_reasons)
    reasons = set(before)
    if previous_close_nav is not None:
        if not isfinite(previous_close_nav) or previous_close_nav <= 0:
            raise ValueError("Previous close NAV must be finite and positive.")
        daily_return = nav / previous_close_nav - 1
        reasons.discard("VALUATION_HALTED")
        if daily_return <= -config.daily_loss_halt:
            reasons.add("DAILY_LOSS_HALTED")
    else:
        reasons.add("VALUATION_HALTED")
    if drawdown <= -config.portfolio_drawdown_stop:
        reasons.add("DRAWDOWN_HALTED")
    risky = [weight for ticker, weight in weights.items()
             if ticker not in {config.cash_asset, config.synthetic_cash_asset}]
    if any(not isfinite(weight) or weight < 0 for weight in risky):
        raise ValueError("Risk weights must be finite and nonnegative.")
    largest = max(risky, default=0.0)
    warning = max(config.drift_warning_weight, config.max_asset_weight)
    review = min(1.0, max(config.drift_review_weight,
                          warning + config.drift_review_weight - config.drift_warning_weight))
    drift = "DRIFT_REVIEW" if largest > review else "WARNING" if largest > warning else "NORMAL"
    return AccountRiskAssessment(high_water, drawdown, daily_return, tuple(sorted(reasons)),
                                 drift, tuple(sorted(reasons - before)))


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

    def authorize_research_reentry(
        self,
        *,
        session: object,
        next_monthly_rebalance_session: object,
        nav: float,
    ) -> None:
        """Apply the preregistered historical proxy at the next month-end."""
        self.authorize_reentry(
            session=session,
            next_monthly_rebalance_session=next_monthly_rebalance_session,
            nav=nav,
            reconciliation_ok=True,
            incident_recorded=True,
            authorized_by="historical_research_proxy",
        )

    def evaluate(
        self,
        *,
        nav: float,
        daily_return: float,
        weights: Mapping[str, float],
    ) -> tuple[RiskStatus, float, tuple[RiskEvent, ...]]:
        if not isfinite(daily_return) or daily_return <= -1:
            raise ValueError("Daily return must be finite and greater than -100%.")
        assessment = evaluate_account_risk(
            config=self.config, nav=float(nav), high_water=self.high_water,
            previous_close_nav=float(nav) / (1 + daily_return), weights=weights,
            halt_reasons=("DRAWDOWN_HALTED",) if self.drawdown_halted else (),
        )
        self.high_water = assessment.high_water
        events = []
        if "DRAWDOWN_HALTED" in assessment.new_halts:
            self.drawdown_halted = True
            events.append(RiskEvent(RiskStatus.DRAWDOWN_HALTED, "PORTFOLIO_DRAWDOWN_STOP",
                                    "Portfolio drawdown reached the manual liquidation trigger.", assessment.drawdown))
        if "DAILY_LOSS_HALTED" in assessment.halt_reasons:
            events.append(RiskEvent(RiskStatus.DAILY_LOSS_HALT, "DAILY_LOSS_HALT",
                                    "Daily portfolio loss reached the temporary halt threshold.", daily_return))
        largest = max((weight for ticker, weight in weights.items()
                       if ticker not in {self.config.cash_asset, self.config.synthetic_cash_asset}), default=0.)
        if assessment.drift_state != "NORMAL":
            events.append(RiskEvent(RiskStatus(assessment.drift_state),
                                    "POSITION_DRIFT_REVIEW" if assessment.drift_state == "DRIFT_REVIEW" else "POSITION_DRIFT_WARNING",
                                    "A position exceeded its configured drift threshold.", largest))
        state = "DAILY_LOSS_HALT" if assessment.state == "DAILY_LOSS_HALTED" else assessment.state
        return RiskStatus(state), assessment.drawdown, tuple(events)
