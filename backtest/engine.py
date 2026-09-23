from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from math import isclose, isfinite
from typing import Iterable

import pandas as pd

from backtest.ledger import PortfolioLedger
from config.settings import Config
from data.calendar import NyseCalendar
from data.models import CorporateAction
from execution.accounting import ACCOUNTING_MODEL_VERSION
from risk.controls import PortfolioRiskMonitor, RiskStatus, evaluate_account_risk


@dataclass
class BacktestState:
    """End-of-session state carried between adjacent out-of-sample windows."""
    ledger: PortfolioLedger
    previous_nav: float
    last_session: pd.Timestamp
    risk_monitor_state: dict[str, object]
    pending_target: dict[str, float] | None = None
    pending_signal_date: pd.Timestamp | None = None
    pending_regime: str | None = None
    pending_execution_config: Config | None = None
    drawdown_reentry_session: pd.Timestamp | None = None


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
        drawdown_reentry_mode: str = "next_month_end",
        raw_close_prices: pd.DataFrame | None = None,
        corporate_actions: Iterable[CorporateAction] = (),
        trade_start: object | None = None,
        trade_end: object | None = None,
        initial_state: BacktestState | None = None,
    ):
        self.config = config
        self.prices = prices.copy().sort_index()
        self.raw_close_prices = None if raw_close_prices is None else raw_close_prices.copy().sort_index().reindex(self.prices.index)
        if self.raw_close_prices is not None and self.raw_close_prices.attrs.get("price_basis") == "total_return":
            raise ValueError("Valuation requires raw Close prices, not total-return prices.")
        self.corporate_actions = tuple(corporate_actions)
        if self.corporate_actions and raw_close_prices is None:
            raise ValueError("Corporate actions require an explicit raw Close-price frame.")
        self.trade_start = None if trade_start is None else pd.Timestamp(trade_start).normalize()
        self.trade_end = None if trade_end is None else pd.Timestamp(trade_end).normalize()
        self.initial_state = deepcopy(initial_state)
        self.execution_prices = (
            None
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
        if drawdown_reentry_mode not in {
            "next_month_end",
            "permanent_cash_stress",
        }:
            raise ValueError(
                "drawdown_reentry_mode must be next_month_end or permanent_cash_stress."
            )
        self.drawdown_reentry_mode = drawdown_reentry_mode

    def _get_rebalance_dates(self) -> pd.DatetimeIndex:
        return self.calendar.rebalance_sessions(
            self.prices.index,
            self.config.rebalance_frequency,
        )

    def _pending_execution_risk_reason(
        self,
        *,
        ledger: PortfolioLedger,
        prices: pd.Series,
        target: dict[str, float],
        previous_close_nav: float,
        risk_monitor: PortfolioRiskMonitor,
    ) -> str | None:
        """Recheck the next session using its open and the preceding close only."""
        open_nav = ledger.mark(prices)
        open_weights = ledger.weights(prices)
        assessment = evaluate_account_risk(
            config=self.config,
            nav=open_nav,
            high_water=risk_monitor.high_water,
            previous_close_nav=previous_close_nav,
            weights=open_weights,
            halt_reasons=("DRAWDOWN_HALTED",) if risk_monitor.drawdown_halted else (),
        )
        reasons = list(assessment.halt_reasons)
        if assessment.drift_state == "DRIFT_REVIEW":
            reasons.append(assessment.drift_state)
        cash_assets = {self.config.cash_asset, self.config.synthetic_cash_asset}
        increases_risk = any(
            weight * open_nav > open_weights.get(ticker, 0.) * open_nav + 1e-8
            for ticker, weight in target.items()
            if ticker not in cash_assets
        )
        if reasons and increases_risk:
            return ",".join(reasons)
        return None

    def run(self) -> dict[str, object]:
        self.config.validate_risk_constraints()
        expected_sessions = self.calendar.sessions(
            self.prices.index.min(), self.prices.index.max()
        )
        non_sessions = self.prices.index.difference(expected_sessions)
        if len(non_sessions):
            raise ValueError(
                "Price index contains non-NYSE sessions; VIX-only dates or synthetic business days are forbidden."
            )
        missing_sessions = expected_sessions.difference(self.prices.index)
        if len(missing_sessions):
            raise ValueError(f"Price index contains missing NYSE sessions: {missing_sessions[0].date()}.")
        if self.prices.index.has_duplicates:
            raise ValueError("Price index contains duplicate NYSE sessions.")
        if self.execution_prices is None:
            raise ValueError(
                "T+1 execution requires an explicit Open-price frame; Close fallback is forbidden."
            )
        if self.raw_close_prices is None:
            raise ValueError("Backtest valuation requires an explicit raw Close-price frame; signal-price fallback is forbidden.")
        if self.config.benchmark in self.raw_close_prices:
            benchmark_close = pd.to_numeric(self.raw_close_prices[self.config.benchmark], errors="coerce")
            if benchmark_close.isna().any() or benchmark_close.le(0.).any() or not benchmark_close.map(isfinite).all():
                raise ValueError("Benchmark raw Close data contains a missing or invalid NYSE session.")
        if self.execution_prices.attrs.get("price_basis") == "total_return":
            raise ValueError("Execution requires raw Open prices.")
        warmup = 252
        if len(self.prices) <= warmup:
            raise ValueError("Not enough data after 252-session warmup. Extend start_date earlier.")
        cash_close = self.raw_close_prices.get(self.config.cash_asset)
        cash_open = self.execution_prices.get(self.config.cash_asset)
        explicit_start = self.trade_start
        if explicit_start is None and self.initial_state is not None:
            explicit_start = self.calendar.next_session(self.initial_state.last_session)
        if explicit_start is not None:
            if explicit_start not in self.prices.index:
                raise ValueError("trade_start must be a covered NYSE session.")
            start_position = self.prices.index.get_loc(explicit_start)
            if start_position < warmup:
                raise ValueError("trade_start requires 252 sessions of feature warmup.")
        else:
            if cash_close is None or cash_open is None:
                raise ValueError(f"Backtest cannot start before {self.config.cash_asset} has Close and Open prices.")
            cash_tradable = (pd.to_numeric(cash_close, errors="coerce").gt(0.)
                             & pd.to_numeric(cash_open, errors="coerce").gt(0.))
            eligible_starts = self.prices.index[(self.prices.index >= self.prices.index[warmup]) & cash_tradable]
            if eligible_starts.empty:
                raise ValueError(f"No backtest session satisfies both 252-session warmup and {self.config.cash_asset} tradability.")
            start_position = self.prices.index.get_loc(eligible_starts[0])
        rebalance_dates = set(self._get_rebalance_dates())
        previous_session = self.prices.index[start_position - 1]
        dates = self.prices.index[start_position:]
        if self.trade_end is not None:
            dates = dates[dates <= self.trade_end]
        if dates.empty:
            raise ValueError("The requested trade window is empty.")
        initial_prices = self.raw_close_prices.loc[previous_session].copy()
        previous_cash_open = self.execution_prices.loc[previous_session].get(self.config.cash_asset, float("nan"))
        if pd.isna(previous_cash_open) or float(previous_cash_open) <= 0.0:
            initial_prices.loc[self.config.cash_asset] = float("nan")
        ledger = PortfolioLedger.initialize(self.config, session=previous_session, prices=initial_prices)
        previous_equity = ledger.mark(self.raw_close_prices.loc[previous_session])
        risk_monitor = PortfolioRiskMonitor(self.config, previous_equity)
        pending_target: dict[str, float] | None = None
        pending_signal_date: pd.Timestamp | None = None
        pending_regime: str | None = None
        pending_execution_config: Config | None = None
        drawdown_reentry_session: pd.Timestamp | None = None
        if self.initial_state is not None:
            state = self.initial_state
            if self.calendar.next_session(state.last_session) != dates[0]:
                raise ValueError("Initial state must end on the immediately preceding NYSE session.")
            ledger = deepcopy(state.ledger)
            ledger.config = self.config
            ledger.order_log = []
            previous_equity = state.previous_nav
            expected_previous_nav = ledger.mark(self.raw_close_prices.loc[previous_session])
            if not isfinite(previous_equity) or not isclose(previous_equity, expected_previous_nav, rel_tol=1e-9, abs_tol=1e-6):
                raise ValueError("Initial state NAV does not match its last raw-close valuation.")
            risk_monitor = PortfolioRiskMonitor(self.config, previous_equity)
            for key, value in state.risk_monitor_state.items():
                if key != "config":
                    setattr(risk_monitor, key, deepcopy(value))
            pending_target = deepcopy(state.pending_target)
            pending_signal_date = state.pending_signal_date
            pending_regime = state.pending_regime
            pending_execution_config = deepcopy(state.pending_execution_config)
            drawdown_reentry_session = state.drawdown_reentry_session
        window_initial_nav = previous_equity
        history: list[dict[str, object]] = []
        signal_history: list[dict[str, object]] = []
        actions_by_session: dict[pd.Timestamp, list[CorporateAction]] = {}
        for raw_action in self.corporate_actions:
            action = raw_action.normalized()
            existing = ledger.corporate_action_state.applied_actions.get(action.action_key)
            if existing is not None and existing != action.revision_hash:
                raise ValueError(f"Applied corporate action revision changed: {action.action_key}.")
            inception = ledger.corporate_action_state.started_session
            if (
                self.initial_state is not None
                and existing is None
                and inception is not None
                and pd.Timestamp(inception) <= action.ex_date < dates[0]
            ):
                raise ValueError(
                    f"Unprocessed historical corporate action: {action.action_key}; replay is required."
                )
            actions_by_session.setdefault(action.ex_date, []).append(action)

        for date in dates:
            ledger.settle(date)
            ledger.apply_corporate_actions(actions_by_session.get(date, ()), date)
            execution = {
                "turnover": 0.0,
                "est_trading_cost": 0.0,
                "est_slippage": 0.0,
                "est_impact": 0.0,
                "est_cost": 0.0,
                "cost_dollars": 0.0,
                "maximum_adv_fraction": float("nan"),
            }
            execution_skip_reason = None
            if pending_target is not None and pending_signal_date is not None:
                if date != self.calendar.next_session(pending_signal_date):
                    raise ValueError("Pending order cannot silently skip its intended execution session.")
                ledger.config = pending_execution_config or self.config
                execution_skip_reason = self._pending_execution_risk_reason(
                    ledger=ledger,
                    prices=self.execution_prices.loc[date],
                    target=pending_target,
                    previous_close_nav=previous_equity,
                    risk_monitor=risk_monitor,
                )
                if execution_skip_reason is None:
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
                pending_execution_config = None
                ledger.config = self.config

            equity = ledger.mark(self.raw_close_prices.loc[date])
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
            current_weights = ledger.weights(self.raw_close_prices.loc[date])
            risk_status, drawdown, events = risk_monitor.evaluate(
                nav=equity,
                daily_return=daily_return,
                weights=current_weights,
            )

            stop_triggered = any(
                event.code == "PORTFOLIO_DRAWDOWN_STOP" for event in events
            )
            research_reentry = False
            if stop_triggered:
                pending_target = {self.config.synthetic_cash_asset: 1.0}
                pending_signal_date = pd.Timestamp(date)
                pending_regime = "risk_off"
                pending_execution_config = deepcopy(self.config)
                if self.drawdown_reentry_mode == "next_month_end":
                    drawdown_reentry_session = self.calendar.next_month_end_session(
                        date
                    )
            elif (
                risk_status == RiskStatus.DRAWDOWN_HALTED
                and self.drawdown_reentry_mode == "next_month_end"
                and drawdown_reentry_session is not None
                and date >= drawdown_reentry_session
                and date in rebalance_dates
            ):
                risk_monitor.authorize_research_reentry(
                    session=date,
                    next_monthly_rebalance_session=drawdown_reentry_session,
                    nav=equity,
                )
                risk_status = RiskStatus.NORMAL
                drawdown = 0.0
                research_reentry = True
                drawdown_reentry_session = None

            if stop_triggered:
                pass
            elif date in rebalance_dates and risk_status not in {
                RiskStatus.DRAWDOWN_HALTED,
                RiskStatus.DRIFT_REVIEW,
            }:
                # A daily halt expires at this session's close. Signals prepared
                # here execute on the next session and must not be discarded for
                # an entire month because this session crossed the daily limit.
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
                pending_execution_config = deepcopy(self.config)
                signal_history.append(
                    {
                        "signal_date": pd.Timestamp(date),
                        "intended_execution_date": self.calendar.next_session(date),
                        "regime": regime,
                        "weights": dict(target),
                        "research_reentry": research_reentry,
                    }
                )

            snapshot: dict[str, object] = {
                "date": date,
                "previous_nav": previous_equity,
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
                "dividend_receivable": ledger.dividend_receivable,
                "accounting_model": ACCOUNTING_MODEL_VERSION,
                "unconfirmed_dividend_payments": ledger.corporate_action_state.unconfirmed_payments,
                "drawdown": drawdown,
                "high_water": risk_monitor.high_water,
                "risk_status": risk_status.value,
                "stop_triggered": stop_triggered,
                "research_reentry": research_reentry,
                "drawdown_reentry_mode": self.drawdown_reentry_mode,
                "maximum_adv_fraction": execution["maximum_adv_fraction"],
                "execution_skip_reason": execution_skip_reason,
            }
            for ticker in set(self.config.universe).union(
                {self.config.synthetic_cash_asset}
            ):
                snapshot[f"w_{ticker}"] = current_weights.get(ticker, 0.0)
            history.append(snapshot)
            previous_equity = equity

        portfolio = pd.DataFrame(history).set_index("date")
        portfolio.attrs["initial_nav"] = window_initial_nav
        portfolio.attrs["accounting_model"] = ACCOUNTING_MODEL_VERSION
        portfolio.attrs["execution_model"] = "BACKTEST_OPEN_REBALANCE"
        orders = pd.DataFrame(ledger.order_log)
        signals = pd.DataFrame(signal_history)
        final_state = BacktestState(
            ledger=deepcopy(ledger), previous_nav=previous_equity, last_session=pd.Timestamp(dates[-1]),
            risk_monitor_state={key: deepcopy(value) for key, value in vars(risk_monitor).items() if key != "config"},
            pending_target=deepcopy(pending_target), pending_signal_date=pending_signal_date,
            pending_regime=pending_regime, pending_execution_config=deepcopy(pending_execution_config),
            drawdown_reentry_session=drawdown_reentry_session,
        )
        return {"portfolio": portfolio, "orders": orders, "signals": signals, "final_state": final_state}
