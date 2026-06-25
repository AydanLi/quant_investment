"""ResearchStore: a single facade over the repositories.

Gives application code one object with the same method names the old
``SQLiteStore`` exposed (so consumers barely change), while delegating to the
backend-agnostic repositories underneath. Also adds capabilities the old store
lacked: per-asset daily weights, run deletion, and direct repository access.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

import pandas as pd
from sqlalchemy import Engine

from config.settings import Config
from storage.db import create_all, get_engine
from storage.repositories import (
    ExperimentRepository,
    MarketDataRepository,
    OrderRepository,
    PortfolioRepository,
    SignalRepository,
)


class ResearchStore:
    def __init__(
        self,
        db_url: Optional[str] = None,
        engine: Optional[Engine] = None,
    ):
        if engine is None:
            engine = get_engine(db_url or Config().db_url)
        self.engine = engine
        self.experiments = ExperimentRepository(engine=engine)
        self.portfolio = PortfolioRepository(engine=engine)
        self.orders = OrderRepository(engine=engine)
        self.signals = SignalRepository(engine=engine)
        self.market_data = MarketDataRepository(engine=engine)

    # -- schema / lifecycle -------------------------------------------------- #
    def init_db(self) -> None:
        """Create any missing tables (idempotent convenience).

        Alembic remains the authoritative schema manager (``alembic upgrade
        head``); this is a safety net so the app still works against a fresh DB.
        """
        create_all(self.engine)

    def close(self) -> None:
        """No-op: the engine/pool is shared and managed elsewhere."""

    # -- writes -------------------------------------------------------------- #
    def save_experiment_run(
        self,
        scenario_name: str,
        config: Any,
        summary: pd.Series,
        latest_signal: Mapping[str, Any],
        **kwargs: Any,
    ) -> int:
        return self.experiments.save_run(
            scenario_name=scenario_name,
            config=config,
            summary=summary,
            latest_signal=latest_signal,
            **kwargs,
        )

    def save_portfolio_daily(self, run_id: int, portfolio: pd.DataFrame) -> None:
        """Persist daily rows AND per-asset weights (from ``w_*`` columns)."""
        self.portfolio.save(run_id, portfolio)

    def save_orders(self, run_id: int, order_df: pd.DataFrame) -> None:
        self.orders.save(run_id, order_df)

    def save_signals(self, run_id: int, latest_signal: Mapping[str, Any]) -> None:
        self.signals.save(run_id, latest_signal)

    def save_full_run(
        self,
        *,
        scenario_name: str,
        config: Any,
        summary: pd.Series,
        portfolio: pd.DataFrame,
        order_df: pd.DataFrame,
        latest_signal: Mapping[str, Any],
        **kwargs: Any,
    ) -> int:
        """Save an experiment and all its child data in one call."""
        run_id = self.save_experiment_run(
            scenario_name, config, summary, latest_signal, **kwargs
        )
        self.save_portfolio_daily(run_id, portfolio)
        self.save_orders(run_id, order_df)
        self.save_signals(run_id, latest_signal)
        return run_id

    # -- reads --------------------------------------------------------------- #
    def get_experiment_runs(self, limit: int = 20) -> pd.DataFrame:
        return self.experiments.get_runs(limit)

    def get_run_portfolio(self, run_id: int) -> pd.DataFrame:
        return self.portfolio.get_daily(run_id)

    def get_run_weights(self, run_id: int, wide: bool = False) -> pd.DataFrame:
        """Per-asset daily weights — new capability vs the legacy store."""
        return self.portfolio.get_weights(run_id, wide=wide)

    def get_run_orders(self, run_id: int) -> pd.DataFrame:
        return self.orders.get(run_id)

    def get_run_signals(self, run_id: int) -> pd.DataFrame:
        return self.signals.get(run_id)

    def delete_run(self, run_id: int) -> bool:
        """Delete a run and all its child rows (cascade)."""
        return self.experiments.delete_run(run_id)
