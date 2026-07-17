from __future__ import annotations

from dataclasses import asdict
from typing import Mapping

import pandas as pd

from config.settings import Config
from data.calendar import NEW_YORK, NyseCalendar
from data.features import FeatureEngineer
from data.trusted_loader import TrustedMarketDataLoader
from risk.engine import RiskEngine
from services.models import SignalDecision, SignalStatus
from strategy.momentum_rotation import MomentumRotationStrategy
from strategy.regime import RegimeDetector
from storage.repositories import GovernanceRepository


class SignalService:
    def __init__(
        self,
        config: Config,
        *,
        loader: TrustedMarketDataLoader | None = None,
        calendar: NyseCalendar | None = None,
    ) -> None:
        self.config = config
        self.calendar = calendar or NyseCalendar()
        self.loader = loader

    def generate_decision(
        self,
        *,
        as_of: pd.Timestamp | None = None,
        current_weights: Mapping[str, float] | None = None,
        nav: float | None = None,
        risk_state: str = "NORMAL",
    ) -> SignalDecision:
        now = pd.Timestamp.now(tz=NEW_YORK) if as_of is None else pd.Timestamp(as_of)
        if now.tzinfo is None:
            now = now.tz_localize(NEW_YORK)
        else:
            now = now.tz_convert(NEW_YORK)
        loader = self.loader or TrustedMarketDataLoader(
            self.config, calendar=self.calendar, as_of=now
        )
        data = loader.load()
        fe = FeatureEngineer(data, self.config)
        prices = fe.make_price_frame()
        returns = fe.make_returns_frame(prices)
        features = fe.compute_features(prices, returns)

        date = pd.Timestamp(prices.index[-1]).normalize()
        quality = loader.quality_report
        issues = tuple(
            {**asdict(issue), "severity": issue.severity.value}
            for issue in (() if quality is None else quality.issues)
        )
        block_reasons: list[str] = []
        diagnostic_data_only = bool(
            quality is not None
            and quality.status.value != "BLOCKED"
            and quality.stale_sessions == self.config.diagnostic_staleness_sessions
        )
        data_blocked = (
            quality is None
            or quality.status.value == "BLOCKED"
            or quality.stale_sessions is None
            or quality.stale_sessions > self.config.diagnostic_staleness_sessions
        )
        if data_blocked:
            block_reasons.append("Data quality or freshness gate did not pass.")

        regime = "UNAVAILABLE"
        target: dict[str, float] = {}
        target_available = False
        if not data_blocked:
            try:
                regime = RegimeDetector(self.config).classify(date, prices, features)
                strategy = MomentumRotationStrategy(self.config)
                risk_engine = RiskEngine(self.config)
                target = strategy.target_weights(date, regime, prices, features)
                target = risk_engine.scale_to_target_vol(date, target, returns)
                target = risk_engine.enforce_weight_limits(target)
                ok, reason = risk_engine.pre_trade_check(target)
                if not ok:
                    block_reasons.append(reason)
                else:
                    target_available = True
            except (ArithmeticError, KeyError, TypeError, ValueError) as exc:
                block_reasons.append(f"Signal inputs failed validation: {exc}")

        if loader.dataset_snapshot_id is None:
            block_reasons.append("No reproducible dataset snapshot was persisted.")
        if self.config.strategy_version == "UNFROZEN":
            block_reasons.append("Strategy version is not frozen.")
        repository = getattr(loader, "repository", None)
        if repository is not None:
            governance = GovernanceRepository(engine=repository.engine)
            if not governance.is_universe_approved(self.config.universe_version):
                block_reasons.append("Universe version is not approved.")
            if not governance.is_strategy_frozen(self.config.strategy_version):
                block_reasons.append("Strategy version is not frozen in governance storage.")

        month_end = self.calendar.is_month_end_session(date)
        after_cutoff = self.calendar.after_cutoff(now, self.config.signal_cutoff_time_et)
        admitted_frequency = self.config.rebalance_frequency == "M"
        halted = risk_state.upper() not in {"NORMAL", "WARNING", "DRIFT_REVIEW"}
        if halted:
            status = SignalStatus.HALTED
        elif block_reasons:
            status = SignalStatus.BLOCKED
        elif diagnostic_data_only:
            status = SignalStatus.DIAGNOSTIC
        elif month_end and after_cutoff and admitted_frequency:
            status = SignalStatus.ACTIONABLE
        else:
            status = SignalStatus.DIAGNOSTIC

        current = {ticker: float(weight) for ticker, weight in (current_weights or {}).items()}
        all_tickers = set(current).union(target) if target_available else set()
        deltas = {
            ticker: float(target.get(ticker, 0.0) - current.get(ticker, 0.0))
            for ticker in sorted(all_tickers)
        }
        account_nav = float(self.config.initial_capital if nav is None else nav)
        dollar_deltas = {ticker: delta * account_nav for ticker, delta in deltas.items()}
        estimated_cost_bps = self.config.trading_cost_bps + self.config.slippage_bps
        if regime == "risk_off":
            estimated_cost_bps = max(
                estimated_cost_bps,
                max(self.config.cost_scenarios_bps),
            )
        estimated_cost = (
            sum(abs(value) for value in dollar_deltas.values())
            * estimated_cost_bps
            / 10000.0
        )
        next_session = (
            self.calendar.next_session(date)
            if month_end
            else self.calendar.next_month_end_session(date)
        )
        return SignalDecision(
            strategy_version=self.config.strategy_version,
            universe_version=self.config.universe_version,
            dataset_snapshot_id=loader.dataset_snapshot_id,
            signal_session=str(date.date()),
            data_as_of=(
                quality.latest_session
                if quality is not None and quality.latest_session is not None
                else str(date.date())
            ),
            generated_at=now.isoformat(),
            next_rebalance_session=str(next_session.date()),
            status=status,
            regime=regime,
            target_weights=target,
            current_weights=current,
            weight_deltas=deltas,
            dollar_deltas=dollar_deltas,
            estimated_cost_dollars=float(estimated_cost),
            data_issues=issues,
            risk_state=risk_state.upper(),
            block_reasons=tuple(dict.fromkeys(block_reasons)),
        )

    def generate_latest_allocation(self, **kwargs: object) -> dict[str, object]:
        """Compatibility wrapper returning the unified decision as a mapping."""
        return self.generate_decision(**kwargs).to_dict()
