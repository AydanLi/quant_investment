from __future__ import annotations

from typing import Dict

import pandas as pd

from backtest.ledger import PortfolioLedger
from config.settings import Config
from data.calendar import NyseCalendar
from risk.controls import PortfolioRiskMonitor, RiskStatus


class Backtester:
    """Close-signal/T+1-open portfolio simulation with drifting quantities."""

    def __init__(
        self,
        config: Config,
        prices: pd.DataFrame,
        returns: pd.DataFrame,
        features: dict,
        regime_detector,
        strategy,
        risk_engine,
        execution_prices: pd.DataFrame | None = None,
        median_dollar_volume: pd.DataFrame | None = None,
    ):
        self.config = config
        self.prices = prices.copy().sort_index()
        self.execution_prices = (
            prices.copy().sort_index()
            if execution_prices is None
            else execution_prices.copy().sort_index().reindex(prices.index)
        )
        self.returns = returns.copy().reindex(prices.index)
        self.median_dollar_volume = (
            None
            if median_dollar_volume is None
            else median_dollar_volume.copy().sort_index().reindex(prices.index)
        )
        self.features = features
        self.regime_detector = regime_detector
        self.strategy = strategy
        self.risk_engine = risk_engine
        self.calendar = NyseCalendar()

    def _get_rebalance_dates(self) -> pd.DatetimeIndex:
        idx = self.prices.index
        if self.config.rebalance_frequency == "D":
            return idx
        if self.config.rebalance_frequency == "W":
            # Weekly is exploratory only. Determine week-end sessions from the
            # exchange calendar, not from whether future price rows exist.
            return pd.DatetimeIndex(
                [
                    session
                    for session in idx
                    if self.calendar.next_session(session).isocalendar().week
                    != pd.Timestamp(session).isocalendar().week
                ]
            )
        return pd.DatetimeIndex(
            [session for session in idx if self.calendar.is_month_end_session(session)]
        )

    def run(self) -> Dict[str, pd.DataFrame]:
        self.config.validate_risk_constraints()
        expected_sessions = self.calendar.sessions(
            self.prices.index.min(), self.prices.index.max()
        )
        non_sessions = self.prices.index.difference(expected_sessions)
        if len(non_sessions):
            raise ValueError(
                "Price index contains non-NYSE sessions; VIX-only dates or synthetic business days are forbidden."
            )
        warmup = 252
        if len(self.prices) <= warmup:
            raise ValueError("Not enough data after 252-session warmup. Extend start_date earlier.")
        rebalance_dates = set(self._get_rebalance_dates())
        previous_session = self.prices.index[warmup - 1]
        dates = self.prices.index[warmup:]
        ledger = PortfolioLedger.initialize(
            self.config,
            session=previous_session,
            prices=self.prices.loc[previous_session],
        )
        previous_equity = ledger.mark(self.prices.loc[previous_session])
        risk_monitor = PortfolioRiskMonitor(self.config, previous_equity)
        pending_target: dict[str, float] | None = None
        pending_signal_date: pd.Timestamp | None = None
        pending_regime: str | None = None
        history: list[dict[str, object]] = []
        signal_history: list[dict[str, object]] = []

        for date in dates:
            ledger.settle(date)
            execution = {
                "turnover": 0.0,
                "est_trading_cost": 0.0,
                "est_slippage": 0.0,
                "est_impact": 0.0,
                "est_cost": 0.0,
                "cost_dollars": 0.0,
                "maximum_adv_fraction": float("nan"),
            }
            if pending_target is not None and pending_signal_date is not None:
                execution = ledger.rebalance(
                    signal_date=pending_signal_date,
                    execution_date=date,
                    target_weights=pending_target,
                    prices=self.execution_prices.loc[date],
                    median_dollar_volume=(
                        None
                        if self.median_dollar_volume is None
                        else self.median_dollar_volume.loc[pending_signal_date]
                    ),
                    risk_off=pending_regime == "risk_off",
                )
                pending_target = None
                pending_signal_date = None
                pending_regime = None

            equity = ledger.mark(self.prices.loc[date])
            gross_return = (
                (equity + float(execution["cost_dollars"])) / previous_equity - 1.0
            )
            daily_return = equity / previous_equity - 1.0
            price_history = self.prices.loc[:date]
            return_history = self.returns.loc[:date]
            feature_history = {
                name: frame.loc[:date] for name, frame in self.features.items()
            }
            regime = self.regime_detector.classify(
                date, price_history, feature_history
            )
            current_weights = ledger.weights(self.prices.loc[date])
            risk_status, drawdown, events = risk_monitor.evaluate(
                nav=equity,
                daily_return=daily_return,
                weights=current_weights,
            )

            stop_triggered = any(
                event.code == "PORTFOLIO_DRAWDOWN_STOP" for event in events
            )
            if stop_triggered:
                pending_target = {self.config.synthetic_cash_asset: 1.0}
                pending_signal_date = pd.Timestamp(date)
                pending_regime = "risk_off"
            elif date in rebalance_dates and risk_status not in {
                RiskStatus.DRAWDOWN_HALTED,
                RiskStatus.DAILY_LOSS_HALT,
                RiskStatus.DRIFT_REVIEW,
            }:
                target = self.strategy.target_weights(
                    date, regime, price_history, feature_history
                )
                target = self.risk_engine.scale_to_target_vol(
                    date, target, return_history
                )
                target = self.risk_engine.enforce_weight_limits(target)
                ok, reason = self.risk_engine.pre_trade_check(target)
                if not ok:
                    raise ValueError(
                        f"Pre-trade risk check failed on {date.date()}: {reason}"
                    )
                pending_target = target
                pending_signal_date = pd.Timestamp(date)
                pending_regime = regime
                signal_history.append(
                    {
                        "signal_date": pd.Timestamp(date),
                        "intended_execution_date": (
                            self.prices.index[self.prices.index.get_loc(date) + 1]
                            if self.prices.index.get_loc(date) + 1 < len(self.prices)
                            else pd.NaT
                        ),
                        "regime": regime,
                        "weights": dict(target),
                    }
                )

            snapshot: dict[str, object] = {
                "date": date,
                "equity": equity,
                "gross_return": gross_return,
                "daily_return": daily_return,
                "regime": regime,
                "turnover": execution["turnover"],
                "est_trading_cost": execution["est_trading_cost"],
                "est_slippage": execution["est_slippage"],
                "est_impact": execution["est_impact"],
                "est_cost": execution["est_cost"],
                "cost_dollars": execution["cost_dollars"],
                "cash": ledger.cash,
                "settled_cash": ledger.settled_cash_balance,
                "unsettled_cash": ledger.unsettled_cash,
                "drawdown": drawdown,
                "high_water": risk_monitor.high_water,
                "risk_status": risk_status.value,
                "stop_triggered": stop_triggered,
                "maximum_adv_fraction": execution["maximum_adv_fraction"],
            }
            for ticker in set(self.config.universe).union(
                {self.config.synthetic_cash_asset}
            ):
                snapshot[f"w_{ticker}"] = current_weights.get(ticker, 0.0)
            history.append(snapshot)
            previous_equity = equity

        portfolio = pd.DataFrame(history).set_index("date")
        orders = pd.DataFrame(ledger.order_log)
        signals = pd.DataFrame(signal_history)
        return {"portfolio": portfolio, "orders": orders, "signals": signals}
