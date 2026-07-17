import pandas as pd
import pytest

from config.settings import Config
from risk.controls import PortfolioRiskMonitor, RiskStatus


def test_drawdown_halt_triggers_once_and_requires_manual_monthly_reentry():
    monitor = PortfolioRiskMonitor(Config(), 10_000.0)

    status, drawdown, events = monitor.evaluate(
        nav=8_400.0, daily_return=-0.02, weights={"SPY": 0.3, "BIL": 0.7}
    )
    repeated = monitor.evaluate(
        nav=8_300.0, daily_return=-0.01, weights={"BIL": 1.0}
    )

    assert status == RiskStatus.DRAWDOWN_HALTED
    assert abs(drawdown + 0.16) < 1e-12
    assert [event.code for event in events] == ["PORTFOLIO_DRAWDOWN_STOP"]
    assert repeated[0] == RiskStatus.DRAWDOWN_HALTED
    assert repeated[2] == ()
    with pytest.raises(ValueError, match="next monthly"):
        monitor.authorize_reentry(
            session="2026-07-20",
            next_monthly_rebalance_session="2026-07-31",
            nav=8_300.0,
            reconciliation_ok=True,
            incident_recorded=True,
            authorized_by="operator",
        )
    monitor.authorize_reentry(
        session="2026-07-31",
        next_monthly_rebalance_session="2026-07-31",
        nav=8_300.0,
        reconciliation_ok=True,
        incident_recorded=True,
        authorized_by="operator",
    )
    assert monitor.drawdown_halted is False
    assert monitor.high_water == 8_300.0


def test_daily_loss_and_drift_warning_review_thresholds():
    monitor = PortfolioRiskMonitor(Config(), 10_000.0)
    status, _, events = monitor.evaluate(
        nav=9_490.0, daily_return=-0.051, weights={"SPY": 0.36, "BIL": 0.64}
    )
    assert status == RiskStatus.DAILY_LOSS_HALT
    assert {event.code for event in events} == {"DAILY_LOSS_HALT", "POSITION_DRIFT_WARNING"}

    monitor = PortfolioRiskMonitor(Config(), 10_000.0)
    status, _, events = monitor.evaluate(
        nav=10_000.0, daily_return=0.0, weights={"SPY": 0.41, "BIL": 0.59}
    )
    assert status == RiskStatus.DRIFT_REVIEW
    assert [event.code for event in events] == ["POSITION_DRIFT_REVIEW"]
