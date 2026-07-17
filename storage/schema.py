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
    Column("dataset_snapshot_id", Integer),
    Column("universe_version", String(40)),
    Column("strategy_version", String(40)),
    Column("admissible", Integer, nullable=False, server_default="0"),
    Column("invalidated_reason", String),
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
    Column("gross_return", Float),
    Column("daily_return", Float),
    Column("regime", String(40)),
    Column("turnover", Float),
    Column("est_trading_cost", Float),
    Column("est_slippage", Float),
    Column("est_impact", Float),
    Column("est_cost", Float),
    Column("cost_dollars", Float),
    Column("cash", Float),
    Column("settled_cash", Float),
    Column("unsettled_cash", Float),
    Column("drawdown", Float),
    Column("high_water", Float),
    Column("risk_status", String(30)),
    Column("stop_triggered", Integer),
    Column("maximum_adv_fraction", Float),
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
    Column("signal_date", String(20)),
    Column("ticker", String(20)),
    Column("side", String(8)),
    Column("weight_change", Float),
    Column("quantity", Float),
    Column("notional", Float),
    Column("price", Float),
    Column("est_cost", Float),
    Column("trading_cost_dollars", Float),
    Column("slippage_dollars", Float),
    Column("impact_cost_dollars", Float),
    Column("adv_fraction", Float),
    Column("average_entry_cost", Float),
    Column("gross_realized_pnl", Float),
    Column("realized_pnl", Float),
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

# Trusted-data v3 tables. The legacy ``market_data`` table remains read-only.
raw_market_data = Table(
    "raw_market_data",
    metadata,
    Column("ticker", String(20), primary_key=True),
    Column("date", String(20), primary_key=True),
    Column("source", String(40), primary_key=True),
    Column("open", Float),
    Column("high", Float),
    Column("low", Float),
    Column("close", Float),
    Column("volume", Float),
    Column("revision", Integer, nullable=False, server_default="1"),
    Column("fetched_at", DateTime, nullable=False, server_default=func.now()),
    Index("ix_raw_market_data_ticker_date", "ticker", "date"),
)

security_master = Table(
    "security_master",
    metadata,
    Column("ticker", String(20), primary_key=True),
    Column("source", String(40), primary_key=True),
    Column("name", String(200)),
    Column("asset_type", String(60)),
    Column("exchange", String(60)),
    Column("listing_date", String(20)),
    Column("delisting_date", String(20)),
    Column("leveraged_or_inverse", Integer, nullable=False, server_default="0"),
    Column("metadata_json", JSON, nullable=False),
    Column("fetched_at", DateTime, nullable=False, server_default=func.now()),
    Index("ix_security_master_ticker", "ticker"),
)

corporate_actions = Table(
    "corporate_actions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("ticker", String(20), nullable=False),
    Column("ex_date", String(20), nullable=False),
    Column("action_type", String(20), nullable=False),
    Column("cash_amount", Float, nullable=False, server_default="0"),
    Column("split_factor", Float, nullable=False, server_default="1"),
    Column("status", String(20), nullable=False, server_default="active"),
    Column("source", String(40), nullable=False),
    Column("fetched_at", DateTime, nullable=False, server_default=func.now()),
    UniqueConstraint(
        "ticker", "ex_date", "action_type", "source",
        name="uq_corporate_actions_ticker_ex_date_action_type_source",
    ),
    Index("ix_corporate_actions_ticker_ex_date", "ticker", "ex_date"),
)

data_revisions = Table(
    "data_revisions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("dataset_table", String(40), nullable=False),
    Column("ticker", String(20), nullable=False),
    Column("date", String(20), nullable=False),
    Column("field", String(40), nullable=False),
    Column("old_value", Float),
    Column("new_value", Float),
    Column("source", String(40), nullable=False),
    Column("detected_at", DateTime, nullable=False, server_default=func.now()),
    Index("ix_data_revisions_ticker_date", "ticker", "date"),
)

dataset_snapshots = Table(
    "dataset_snapshots",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column("as_of", String(40), nullable=False),
    Column("start_date", String(20)),
    Column("end_date", String(20)),
    Column("primary_source", String(40), nullable=False),
    Column("secondary_source", String(40)),
    Column("content_hash", String(64), nullable=False, unique=True),
    Column("status", String(20), nullable=False),
    Column("quality_json", JSON, nullable=False),
    Index("ix_dataset_snapshots_content_hash", "content_hash"),
)

dataset_snapshot_bars = Table(
    "dataset_snapshot_bars",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("snapshot_id", Integer, ForeignKey("dataset_snapshots.id", ondelete="CASCADE"), nullable=False),
    Column("ticker", String(20), nullable=False),
    Column("date", String(20), nullable=False),
    Column("role", String(20), nullable=False),
    Column("source", String(40), nullable=False),
    Column("open", Float),
    Column("high", Float),
    Column("low", Float),
    Column("close", Float),
    Column("volume", Float),
    UniqueConstraint("snapshot_id", "ticker", "date", "role", name="uq_dataset_snapshot_bars_snapshot_ticker_date_role"),
    Index("ix_dataset_snapshot_bars_snapshot_ticker", "snapshot_id", "ticker"),
)

dataset_snapshot_actions = Table(
    "dataset_snapshot_actions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("snapshot_id", Integer, ForeignKey("dataset_snapshots.id", ondelete="CASCADE"), nullable=False),
    Column("ticker", String(20), nullable=False),
    Column("ex_date", String(20), nullable=False),
    Column("action_type", String(20), nullable=False),
    Column("role", String(20), nullable=False),
    Column("cash_amount", Float, nullable=False),
    Column("split_factor", Float, nullable=False),
    Column("status", String(20), nullable=False),
    Column("source", String(40), nullable=False),
    UniqueConstraint("snapshot_id", "ticker", "ex_date", "action_type", "role", name="uq_dataset_snapshot_actions_snapshot_ticker_date_type_role"),
    Index("ix_dataset_snapshot_actions_snapshot_ticker", "snapshot_id", "ticker"),
)

universe_versions = Table(
    "universe_versions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("version", String(40), nullable=False, unique=True),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column("effective_date", String(20), nullable=False),
    Column("status", String(20), nullable=False, server_default="draft"),
    Column("seed_tickers_json", JSON, nullable=False),
    Column("rules_json", JSON, nullable=False),
    Column("eligibility_json", JSON),
    Column("approved_at", DateTime),
    Column("approved_by", String(100)),
    Column("historical_universe_integrity", Integer, nullable=False, server_default="0"),
)

strategy_versions = Table(
    "strategy_versions",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("version", String(40), nullable=False, unique=True),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column("frozen_at", DateTime),
    Column("status", String(20), nullable=False, server_default="draft"),
    Column("universe_version", String(40), nullable=False),
    Column("dataset_snapshot_id", Integer),
    Column("code_commit", String(64)),
    Column("protocol_json", JSON, nullable=False),
    Column("paper_start", DateTime),
    Column("paper_clock_restart_reason", String),
)

admission_runs = Table(
    "admission_runs",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("strategy_version", String(40), nullable=False),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column("methodology", String(100), nullable=False),
    Column("status", String(20), nullable=False),
    Column("selection_uses_future_holdout", Integer, nullable=False, server_default="0"),
    Column("results_json", JSON, nullable=False),
    Index("ix_admission_runs_strategy_version", "strategy_version"),
)

parameter_trials = Table(
    "parameter_trials",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("admission_run_id", Integer, ForeignKey("admission_runs.id", ondelete="CASCADE"), nullable=False),
    Column("label", String(100), nullable=False),
    Column("parameters_json", JSON, nullable=False),
    Column("folds_json", JSON, nullable=False),
    Column("status", String(20), nullable=False),
    Column("score", Float),
    UniqueConstraint("admission_run_id", "label", name="uq_parameter_trials_admission_label"),
)

order_intents = Table(
    "order_intents",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("client_order_id", String(100), nullable=False),
    Column("environment", String(20), nullable=False),
    Column("strategy_version", String(40), nullable=False),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column("approved_at", DateTime),
    Column("approved_by", String(100)),
    Column("status", String(20), nullable=False),
    Column("ticker", String(20), nullable=False),
    Column("side", String(8), nullable=False),
    Column("quantity", Float, nullable=False),
    Column("limit_price", Float, nullable=False),
    Column("arrival_bid", Float),
    Column("arrival_ask", Float),
    Column("arrival_mid", Float),
    Column("adv_fraction", Float),
    Column("broker_order_id", String(100)),
    Column("submitted_at", DateTime),
    Column("metadata_json", JSON),
    UniqueConstraint("environment", "client_order_id"),
)

execution_fills = Table(
    "execution_fills",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("order_intent_id", Integer, ForeignKey("order_intents.id", ondelete="CASCADE"), nullable=False),
    Column("environment", String(20), nullable=False),
    Column("filled_at", DateTime, nullable=False),
    Column("quantity", Float, nullable=False),
    Column("price", Float, nullable=False),
    Column("commission", Float, nullable=False, server_default="0"),
    Column("implementation_shortfall_bps", Float),
    Column("broker_execution_id", String(100), nullable=False),
    UniqueConstraint("environment", "broker_execution_id"),
)

reconciliations = Table(
    "reconciliations",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column("environment", String(20), nullable=False),
    Column("account_ref", String(80), nullable=False),
    Column("status", String(20), nullable=False),
    Column("nav", Float),
    Column("difference_value", Float),
    Column("details_json", JSON, nullable=False),
)

risk_incidents = Table(
    "risk_incidents",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column("resolved_at", DateTime),
    Column("strategy_version", String(40), nullable=False),
    Column("code", String(60), nullable=False),
    Column("severity", String(20), nullable=False),
    Column("status", String(20), nullable=False, server_default="open"),
    Column("trigger_value", Float),
    Column("details_json", JSON, nullable=False),
    Column("resolution_note", String),
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
    "raw_market_data",
    "security_master",
    "corporate_actions",
    "data_revisions",
    "dataset_snapshots",
    "dataset_snapshot_bars",
    "dataset_snapshot_actions",
    "universe_versions",
    "strategy_versions",
    "admission_runs",
    "parameter_trials",
    "order_intents",
    "execution_fills",
    "reconciliations",
    "risk_incidents",
    "brokerage_mirror_snapshots",
    "brokerage_mirror_positions",
]
