"""add daily return and cost components

Revision ID: d4c91f7a2e6b
Revises: b91e2f08c4a1
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4c91f7a2e6b"
down_revision: Union[str, None] = "b91e2f08c4a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("portfolio_daily", schema=None) as batch_op:
        batch_op.add_column(sa.Column("gross_return", sa.Float(), nullable=True))
        batch_op.add_column(
            sa.Column("est_trading_cost", sa.Float(), nullable=True)
        )
        batch_op.add_column(sa.Column("est_slippage", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("portfolio_daily", schema=None) as batch_op:
        batch_op.drop_column("est_slippage")
        batch_op.drop_column("est_trading_cost")
        batch_op.drop_column("gross_return")
