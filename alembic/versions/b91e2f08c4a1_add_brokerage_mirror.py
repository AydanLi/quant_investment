"""add read-only brokerage mirror

Revision ID: b91e2f08c4a1
Revises: e3d6c6759be2
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b91e2f08c4a1"
down_revision: Union[str, None] = "e3d6c6759be2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "brokerage_mirror_snapshots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("account_ref", sa.String(length=40), nullable=False),
        sa.Column("account_type", sa.String(length=40), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("position_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_brokerage_mirror_snapshots")),
    )
    op.create_index(
        "ix_brokerage_mirror_snapshots_provider_account_ref_captured_at",
        "brokerage_mirror_snapshots",
        ["provider", "account_ref", "captured_at"],
    )
    op.create_table(
        "brokerage_mirror_positions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("average_buy_price", sa.Float(), nullable=True),
        sa.Column("shares_available_for_sells", sa.Float(), nullable=False),
        sa.Column("position_type", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["brokerage_mirror_snapshots.id"],
            name=op.f("fk_brokerage_mirror_positions_snapshot_id_brokerage_mirror_snapshots"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_brokerage_mirror_positions")),
        sa.UniqueConstraint(
            "snapshot_id",
            "symbol",
            name="uq_brokerage_mirror_positions_snapshot_symbol",
        ),
    )
    op.create_index(
        "ix_brokerage_mirror_positions_snapshot_id",
        "brokerage_mirror_positions",
        ["snapshot_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_brokerage_mirror_positions_snapshot_id",
        table_name="brokerage_mirror_positions",
    )
    op.drop_table("brokerage_mirror_positions")
    op.drop_index(
        "ix_brokerage_mirror_snapshots_provider_account_ref_captured_at",
        table_name="brokerage_mirror_snapshots",
    )
    op.drop_table("brokerage_mirror_snapshots")
