"""trusted data, research governance, and execution controls

Revision ID: f7a2c9e4b301
Revises: d4c91f7a2e6b
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f7a2c9e4b301"
down_revision: Union[str, None] = "d4c91f7a2e6b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("experiment_runs") as batch_op:
        batch_op.add_column(sa.Column("dataset_snapshot_id", sa.Integer()))
        batch_op.add_column(sa.Column("universe_version", sa.String(length=40)))
        batch_op.add_column(sa.Column("strategy_version", sa.String(length=40)))
        batch_op.add_column(
            sa.Column("admissible", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("invalidated_reason", sa.String()))

    with op.batch_alter_table("portfolio_daily") as batch_op:
        batch_op.add_column(sa.Column("cost_dollars", sa.Float()))
        batch_op.add_column(sa.Column("est_impact", sa.Float()))
        batch_op.add_column(sa.Column("cash", sa.Float()))
        batch_op.add_column(sa.Column("settled_cash", sa.Float()))
        batch_op.add_column(sa.Column("unsettled_cash", sa.Float()))
        batch_op.add_column(sa.Column("drawdown", sa.Float()))
        batch_op.add_column(sa.Column("high_water", sa.Float()))
        batch_op.add_column(sa.Column("risk_status", sa.String(length=30)))
        batch_op.add_column(sa.Column("stop_triggered", sa.Integer()))
        batch_op.add_column(sa.Column("maximum_adv_fraction", sa.Float()))

    with op.batch_alter_table("orders") as batch_op:
        batch_op.add_column(sa.Column("signal_date", sa.String(length=20)))
        batch_op.add_column(sa.Column("quantity", sa.Float()))
        batch_op.add_column(sa.Column("notional", sa.Float()))
        batch_op.add_column(sa.Column("trading_cost_dollars", sa.Float()))
        batch_op.add_column(sa.Column("slippage_dollars", sa.Float()))
        batch_op.add_column(sa.Column("impact_cost_dollars", sa.Float()))
        batch_op.add_column(sa.Column("adv_fraction", sa.Float()))

    op.execute(
        sa.text(
            "UPDATE experiment_runs SET status='invalid_data_v1', admissible=0, "
            "invalidated_reason='Legacy adjusted-price cache may contain batch-boundary dividend loss.' "
            "WHERE dataset_snapshot_id IS NULL"
        )
    )

    op.create_table(
        "raw_market_data",
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("date", sa.String(length=20), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("open", sa.Float()),
        sa.Column("high", sa.Float()),
        sa.Column("low", sa.Float()),
        sa.Column("close", sa.Float()),
        sa.Column("volume", sa.Float()),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("fetched_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("ticker", "date", "source", name=op.f("pk_raw_market_data")),
    )
    op.create_index("ix_raw_market_data_ticker_date", "raw_market_data", ["ticker", "date"])

    op.create_table(
        "security_master",
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=200)),
        sa.Column("asset_type", sa.String(length=60)),
        sa.Column("exchange", sa.String(length=60)),
        sa.Column("listing_date", sa.String(length=20)),
        sa.Column("delisting_date", sa.String(length=20)),
        sa.Column("leveraged_or_inverse", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("ticker", "source", name=op.f("pk_security_master")),
    )
    op.create_index("ix_security_master_ticker", "security_master", ["ticker"])

    op.create_table(
        "corporate_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("ex_date", sa.String(length=20), nullable=False),
        sa.Column("action_type", sa.String(length=20), nullable=False),
        sa.Column("cash_amount", sa.Float(), nullable=False, server_default="0"),
        sa.Column("split_factor", sa.Float(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("ticker", "ex_date", "action_type", "source", name="uq_corporate_actions_ticker_ex_date_action_type_source"),
    )
    op.create_index("ix_corporate_actions_ticker_ex_date", "corporate_actions", ["ticker", "ex_date"])

    op.create_table(
        "data_revisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dataset_table", sa.String(length=40), nullable=False),
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("date", sa.String(length=20), nullable=False),
        sa.Column("field", sa.String(length=40), nullable=False),
        sa.Column("old_value", sa.Float()),
        sa.Column("new_value", sa.Float()),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("detected_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_data_revisions_ticker_date", "data_revisions", ["ticker", "date"])

    op.create_table(
        "dataset_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("as_of", sa.String(length=40), nullable=False),
        sa.Column("start_date", sa.String(length=20)),
        sa.Column("end_date", sa.String(length=20)),
        sa.Column("primary_source", sa.String(length=40), nullable=False),
        sa.Column("secondary_source", sa.String(length=40)),
        sa.Column("content_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("quality_json", sa.JSON(), nullable=False),
    )
    op.create_index("ix_dataset_snapshots_content_hash", "dataset_snapshots", ["content_hash"])

    op.create_table(
        "dataset_snapshot_bars",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("date", sa.String(length=20), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("open", sa.Float()),
        sa.Column("high", sa.Float()),
        sa.Column("low", sa.Float()),
        sa.Column("close", sa.Float()),
        sa.Column("volume", sa.Float()),
        sa.ForeignKeyConstraint(["snapshot_id"], ["dataset_snapshots.id"], ondelete="CASCADE", name=op.f("fk_dataset_snapshot_bars_snapshot_id_dataset_snapshots")),
        sa.UniqueConstraint("snapshot_id", "ticker", "date", "role", name="uq_dataset_snapshot_bars_snapshot_ticker_date_role"),
    )
    op.create_index("ix_dataset_snapshot_bars_snapshot_ticker", "dataset_snapshot_bars", ["snapshot_id", "ticker"])

    op.create_table(
        "dataset_snapshot_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("ex_date", sa.String(length=20), nullable=False),
        sa.Column("action_type", sa.String(length=20), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("cash_amount", sa.Float(), nullable=False),
        sa.Column("split_factor", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["snapshot_id"], ["dataset_snapshots.id"], ondelete="CASCADE", name=op.f("fk_dataset_snapshot_actions_snapshot_id_dataset_snapshots")),
        sa.UniqueConstraint("snapshot_id", "ticker", "ex_date", "action_type", "role", name="uq_dataset_snapshot_actions_snapshot_ticker_date_type_role"),
    )
    op.create_index("ix_dataset_snapshot_actions_snapshot_ticker", "dataset_snapshot_actions", ["snapshot_id", "ticker"])

    op.create_table(
        "universe_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.String(length=40), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("effective_date", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("seed_tickers_json", sa.JSON(), nullable=False),
        sa.Column("rules_json", sa.JSON(), nullable=False),
        sa.Column("eligibility_json", sa.JSON()),
        sa.Column("approved_at", sa.DateTime()),
        sa.Column("approved_by", sa.String(length=100)),
        sa.Column("historical_universe_integrity", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "strategy_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.String(length=40), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("frozen_at", sa.DateTime()),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("universe_version", sa.String(length=40), nullable=False),
        sa.Column("dataset_snapshot_id", sa.Integer()),
        sa.Column("code_commit", sa.String(length=64)),
        sa.Column("protocol_json", sa.JSON(), nullable=False),
        sa.Column("paper_start", sa.DateTime()),
        sa.Column("paper_clock_restart_reason", sa.String()),
    )

    op.create_table(
        "admission_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_version", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("methodology", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("selection_uses_future_holdout", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("results_json", sa.JSON(), nullable=False),
    )
    op.create_index("ix_admission_runs_strategy_version", "admission_runs", ["strategy_version"])

    op.create_table(
        "parameter_trials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("admission_run_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("folds_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("score", sa.Float()),
        sa.ForeignKeyConstraint(["admission_run_id"], ["admission_runs.id"], ondelete="CASCADE", name=op.f("fk_parameter_trials_admission_run_id_admission_runs")),
        sa.UniqueConstraint("admission_run_id", "label", name="uq_parameter_trials_admission_label"),
    )

    op.create_table(
        "order_intents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("client_order_id", sa.String(length=100), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("strategy_version", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("approved_at", sa.DateTime()),
        sa.Column("approved_by", sa.String(length=100)),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("ticker", sa.String(length=20), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("limit_price", sa.Float(), nullable=False),
        sa.Column("arrival_bid", sa.Float()),
        sa.Column("arrival_ask", sa.Float()),
        sa.Column("arrival_mid", sa.Float()),
        sa.Column("adv_fraction", sa.Float()),
        sa.Column("broker_order_id", sa.String(length=100)),
        sa.Column("submitted_at", sa.DateTime()),
        sa.Column("metadata_json", sa.JSON()),
        sa.UniqueConstraint(
            "environment",
            "client_order_id",
            name=op.f("uq_order_intents_environment_client_order_id"),
        ),
    )

    op.create_table(
        "execution_fills",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_intent_id", sa.Integer(), nullable=False),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("filled_at", sa.DateTime(), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("commission", sa.Float(), nullable=False, server_default="0"),
        sa.Column("implementation_shortfall_bps", sa.Float()),
        sa.Column("broker_execution_id", sa.String(length=100), nullable=False),
        sa.UniqueConstraint(
            "environment",
            "broker_execution_id",
            name=op.f("uq_execution_fills_environment_broker_execution_id"),
        ),
        sa.ForeignKeyConstraint(["order_intent_id"], ["order_intents.id"], ondelete="CASCADE", name=op.f("fk_execution_fills_order_intent_id_order_intents")),
    )

    op.create_table(
        "reconciliations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("environment", sa.String(length=20), nullable=False),
        sa.Column("account_ref", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("nav", sa.Float()),
        sa.Column("difference_value", sa.Float()),
        sa.Column("details_json", sa.JSON(), nullable=False),
    )

    op.create_table(
        "risk_incidents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime()),
        sa.Column("strategy_version", sa.String(length=40), nullable=False),
        sa.Column("code", sa.String(length=60), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("trigger_value", sa.Float()),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("resolution_note", sa.String()),
    )


def downgrade() -> None:
    for table in (
        "risk_incidents", "reconciliations", "execution_fills", "order_intents",
        "parameter_trials", "admission_runs", "strategy_versions", "universe_versions",
        "dataset_snapshot_actions", "dataset_snapshot_bars", "dataset_snapshots",
        "data_revisions", "corporate_actions", "security_master",
        "raw_market_data",
    ):
        op.drop_table(table)
    with op.batch_alter_table("experiment_runs") as batch_op:
        batch_op.drop_column("invalidated_reason")
        batch_op.drop_column("admissible")
        batch_op.drop_column("strategy_version")
        batch_op.drop_column("universe_version")
        batch_op.drop_column("dataset_snapshot_id")
    with op.batch_alter_table("orders") as batch_op:
        batch_op.drop_column("adv_fraction")
        batch_op.drop_column("impact_cost_dollars")
        batch_op.drop_column("slippage_dollars")
        batch_op.drop_column("trading_cost_dollars")
        batch_op.drop_column("notional")
        batch_op.drop_column("quantity")
        batch_op.drop_column("signal_date")
    with op.batch_alter_table("portfolio_daily") as batch_op:
        batch_op.drop_column("maximum_adv_fraction")
        batch_op.drop_column("stop_triggered")
        batch_op.drop_column("risk_status")
        batch_op.drop_column("high_water")
        batch_op.drop_column("drawdown")
        batch_op.drop_column("unsettled_cash")
        batch_op.drop_column("settled_cash")
        batch_op.drop_column("cash")
        batch_op.drop_column("cost_dollars")
        batch_op.drop_column("est_impact")
