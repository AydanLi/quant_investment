"""Database schema for the quant research platform.

Single source of truth for all tables, defined with SQLAlchemy Core so the same
schema runs unchanged on SQLite (today) and Postgres / MySQL (later). Only
portable column types are used (Integer / Float / String / DateTime / JSON);
no dialect-specific SQL appears here.

Alembic imports ``metadata`` from this module to autogenerate migrations, so
this file — not the live database — is the authoritative definition.
"""
from __future__ import annotations

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    func,
)

# Explicit naming convention so every constraint/index has a deterministic name.
# Alembic needs this to emit clean ALTER statements, and named constraints
# behave consistently across SQLite / Postgres / MySQL.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


# --------------------------------------------------------------------------- #
# experiment_runs: one row per backtest run.
# Filterable params are promoted to columns; the complete config is also kept
# as JSON so any run is fully reproducible regardless of future param changes.
# --------------------------------------------------------------------------- #
experiment_runs = Table(
    "experiment_runs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column("scenario_name", String(200)),
    # Reproducibility: full config snapshot + a hash for dedup / lookup.
    Column("config_json", JSON, nullable=False),
    Column("config_hash", String(64)),
    # Promoted, queryable config fields (mirrors of values inside config_json).
    Column("start_date", String(20)),
    Column("end_date", String(20)),
    Column("benchmark", String(20)),
    Column("rebalance_frequency", String(4)),
    Column("top_n", Integer),
    Column("min_momentum_threshold", Float),
    Column("target_annual_vol", Float),
    Column("max_asset_weight", Float),
    Column("risk_off_cash_weight", Float),
    Column("vix_risk_off_threshold", Float),
    Column("vix_high_threshold", Float),
    Column("trading_cost_bps", Float),
    # Summary metrics.
    Column("start_equity", Float),
    Column("end_equity", Float),
    Column("total_return", Float),
    Column("cagr", Float),
    Column("annual_vol", Float),
    Column("sharpe", Float),
    Column("sortino", Float),
    Column("max_drawdown", Float),
    Column("avg_turnover", Float),
    # Latest-signal pointer + research metadata.
    Column("latest_signal_date", String(20)),
    Column("latest_regime", String(40)),
    Column("status", String(20), nullable=False, server_default="complete"),
    Column("notes", String),
    Column("tags", String(200)),
    Index("ix_experiment_runs_config_hash", "config_hash"),
    Index("ix_experiment_runs_scenario_name", "scenario_name"),
)


# --------------------------------------------------------------------------- #
# portfolio_daily: one row per day, per run.
# --------------------------------------------------------------------------- #
portfolio_daily = Table(
    "portfolio_daily",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "run_id",
        Integer,
        ForeignKey("experiment_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("date", String(20), nullable=False),
    Column("equity", Float),
    Column("daily_return", Float),
    Column("regime", String(40)),
    Column("turnover", Float),
    Column("est_cost", Float),
    UniqueConstraint("run_id", "date", name="uq_portfolio_daily_run_id_date"),
    Index("ix_portfolio_daily_run_id_date", "run_id", "date"),
)


# --------------------------------------------------------------------------- #
# portfolio_weights: daily per-asset weight, long format.
# NEW vs the old store — captures what the portfolio actually held each day.
# --------------------------------------------------------------------------- #
portfolio_weights = Table(
    "portfolio_weights",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "run_id",
        Integer,
        ForeignKey("experiment_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("date", String(20), nullable=False),
    Column("ticker", String(20), nullable=False),
    Column("weight", Float, nullable=False),
    UniqueConstraint(
        "run_id", "date", "ticker", name="uq_portfolio_weights_run_id_date_ticker"
    ),
    Index("ix_portfolio_weights_run_id_date", "run_id", "date"),
)


# --------------------------------------------------------------------------- #
# orders: rebalance trades emitted by the (mock) broker.
# --------------------------------------------------------------------------- #
orders = Table(
    "orders",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "run_id",
        Integer,
        ForeignKey("experiment_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("order_date", String(20), nullable=False),
    Column("ticker", String(20)),
    Column("side", String(8)),
    Column("weight_change", Float),
    Column("price", Float),
    Column("est_cost", Float),
    Index("ix_orders_run_id", "run_id"),
)


# --------------------------------------------------------------------------- #
# signals: latest target allocation snapshot for a run.
# --------------------------------------------------------------------------- #
signals = Table(
    "signals",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "run_id",
        Integer,
        ForeignKey("experiment_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("signal_date", String(20), nullable=False),
    Column("regime", String(40)),
    Column("ticker", String(20)),
    Column("weight", Float),
    Index("ix_signals_run_id", "run_id"),
)


# --------------------------------------------------------------------------- #
# market_data: cached OHLCV bars, shared across runs.
# Download once, reuse everywhere -> reproducible backtests, no API hammering.
# --------------------------------------------------------------------------- #
market_data = Table(
    "market_data",
    metadata,
    Column("ticker", String(20), primary_key=True),
    Column("date", String(20), primary_key=True),
    Column("open", Float),
    Column("high", Float),
    Column("low", Float),
    Column("close", Float),
    Column("volume", Float),
    Column("auto_adjusted", Integer, nullable=False, server_default="1"),
    Column("source", String(40), nullable=False, server_default="yfinance"),
    Column("fetched_at", DateTime, nullable=False, server_default=func.now()),
    Index("ix_market_data_ticker_date", "ticker", "date"),
)

# Read-only snapshots imported from an external brokerage.  This is deliberately
# separate from backtest portfolio state and the mock-broker order tables.
brokerage_mirror_snapshots = Table(
    "brokerage_mirror_snapshots",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("provider", String(40), nullable=False),
    Column("account_ref", String(40), nullable=False),
    Column("account_type", String(40), nullable=False),
    Column("captured_at", DateTime, nullable=False),
    Column("position_count", Integer, nullable=False),
    Index(
        "ix_brokerage_mirror_snapshots_provider_account_ref_captured_at",
        "provider",
        "account_ref",
        "captured_at",
    ),
)

brokerage_mirror_positions = Table(
    "brokerage_mirror_positions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column(
        "snapshot_id",
        Integer,
        ForeignKey("brokerage_mirror_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("symbol", String(20), nullable=False),
    Column("quantity", Float, nullable=False),
    Column("average_buy_price", Float),
    Column("shares_available_for_sells", Float, nullable=False),
    Column("position_type", String(20), nullable=False),
    UniqueConstraint(
        "snapshot_id", "symbol", name="uq_brokerage_mirror_positions_snapshot_symbol"
    ),
    Index("ix_brokerage_mirror_positions_snapshot_id", "snapshot_id"),
)


__all__ = [
    "metadata",
    "experiment_runs",
    "portfolio_daily",
    "portfolio_weights",
    "orders",
    "signals",
    "market_data",
    "brokerage_mirror_snapshots",
    "brokerage_mirror_positions",
]
