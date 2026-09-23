"""Versioned economic accounting, runtime identity and complete metrics.

Revision ID: 6b2e1d9a4f30
Revises: 5f74c1a9d2b0
"""
from alembic import op
import sqlalchemy as sa

revision = "6b2e1d9a4f30"
down_revision = "5f74c1a9d2b0"
branch_labels = None
depends_on = None


def additions():
    return {
        "data_revisions": [sa.Column("old_text", sa.String(500)), sa.Column("new_text", sa.String(500))],
        "corporate_actions": [sa.Column("payment_date", sa.String(20)), sa.Column("payment_source", sa.String(200))],
        "dataset_snapshot_actions": [sa.Column("payment_date", sa.String(20)), sa.Column("payment_source", sa.String(200))],
        "portfolio_daily": [sa.Column("dividend_receivable", sa.Float())],
        "experiment_runs": [sa.Column("summary_json", sa.JSON()), sa.Column("runtime_hash", sa.String(64))],
        "strategy_versions": [sa.Column("runtime_manifest_json", sa.JSON()), sa.Column("runtime_hash", sa.String(64)),
                              sa.Column("approved_by", sa.String(100)), sa.Column("approved_at", sa.DateTime())],
        "admission_runs": [sa.Column("runtime_hash", sa.String(64)), sa.Column("parent_runtime_hash", sa.String(64)),
                           sa.Column("evidence_role", sa.String(40), nullable=False, server_default="research_selector")],
        "signal_decisions": [sa.Column("runtime_hash", sa.String(64))],
        "paper_cycles": [sa.Column("execution_payload_json", sa.JSON())],
        "paper_accounts": [sa.Column("halt_reasons_json", sa.JSON(), nullable=False, server_default="[]"),
                           sa.Column("drift_state", sa.String(30), nullable=False, server_default="NORMAL"),
                           sa.Column("accounting_state_json", sa.JSON(), nullable=False, server_default="{}")],
    }


def upgrade():
    # Legacy events remain undated. Only new inserts get a database timestamp.
    for table in ("signal_decisions", "execution_fills"):
        op.add_column(table, sa.Column("recorded_at", sa.DateTime(), nullable=True))
        with op.batch_alter_table(table) as batch:
            batch.alter_column("recorded_at", existing_type=sa.DateTime(),
                               server_default=sa.func.current_timestamp())
    for table, columns in additions().items():
        for column in columns:
            op.add_column(table, column)
    op.create_table(
        "paper_account_closes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session", sa.String(20), nullable=False),
        sa.Column("nav", sa.Float(), nullable=False),
        sa.Column("account_version", sa.Integer(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.Column("source_snapshot_id", sa.Integer(), sa.ForeignKey("dataset_snapshots.id")),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("baseline_kind", sa.String(30), nullable=False),
        sa.UniqueConstraint("account_id", "session", "baseline_kind", name="uq_paper_account_closes_account_session"),
    )
    op.create_table(
        "paper_account_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("paper_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action_key", sa.String(200), nullable=False),
        sa.Column("phase", sa.String(20), nullable=False),
        sa.Column("revision_hash", sa.String(64), nullable=False),
        sa.Column("session", sa.String(20), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("account_id", "action_key", "phase", name="uq_paper_account_action_phase"),
    )
    op.create_table(
        "validation_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_version", sa.String(40), sa.ForeignKey("strategy_versions.version"), nullable=False),
        sa.Column("runtime_hash", sa.String(64), nullable=False),
        sa.Column("environment", sa.String(20), nullable=False),
        sa.Column("execution_model", sa.String(50), nullable=False),
        sa.Column("account_ref", sa.String(80), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime()),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("restart_reason", sa.String()),
    )
    op.create_index("uq_validation_runs_active", "validation_runs",
        ["strategy_version", "environment", "execution_model", "account_ref"], unique=True,
        sqlite_where=sa.text("status = 'active'"), postgresql_where=sa.text("status = 'active'"))
    # Keep historical financial locks; never manufacture a previous close or
    # silently convert a legacy account into an approved new runtime.
    accounts = sa.table("paper_accounts", sa.column("id", sa.Integer()),
        sa.column("risk_state", sa.String()), sa.column("halt_reasons_json", sa.JSON()),
        sa.column("drift_state", sa.String()))
    conn = op.get_bind()
    for row in conn.execute(sa.select(accounts.c.id, accounts.c.risk_state)).mappings():
        state = row["risk_state"]
        drift = state if state in {"WARNING", "DRIFT_REVIEW"} else "NORMAL"
        reasons = [] if state in {"NORMAL", "WARNING", "DRIFT_REVIEW"} else [state]
        conn.execute(accounts.update().where(accounts.c.id == row["id"]).values(
            halt_reasons_json=reasons, drift_state=drift))


def downgrade():
    conn = op.get_bind()
    for table in ("signal_decisions", "execution_fills"):
        if conn.scalar(sa.text(f"SELECT COUNT(*) FROM {table} WHERE recorded_at IS NOT NULL")):
            raise RuntimeError("Cannot discard new accounting/runtime evidence; restore a consistent backup or roll forward.")
    for table in ("paper_account_actions", "paper_account_closes", "validation_runs"):
        if conn.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one():
            raise RuntimeError("Cannot discard new accounting/runtime evidence; restore a consistent backup or roll forward.")
    # Production histories using the new model must not be run by old code.
    count = conn.execute(sa.text(
        "SELECT COUNT(*) FROM strategy_versions WHERE runtime_hash IS NOT NULL"
    )).scalar_one()
    if count:
        raise RuntimeError("Cannot downgrade after a runtime manifest has been frozen.")
    for table in ("validation_runs", "paper_account_actions", "paper_account_closes"):
        op.drop_table(table)
    for table, columns in reversed(list(additions().items())):
        with op.batch_alter_table(table) as batch:
            for column in reversed(columns):
                batch.drop_column(column.name)
    for table in ("execution_fills", "signal_decisions"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("recorded_at")
