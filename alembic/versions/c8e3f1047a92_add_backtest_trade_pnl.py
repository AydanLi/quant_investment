"""add backtest trade cost basis and realized pnl

Revision ID: c8e3f1047a92
Revises: f7a2c9e4b301
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8e3f1047a92"
down_revision: Union[str, None] = "f7a2c9e4b301"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch_op:
        batch_op.add_column(sa.Column("average_entry_cost", sa.Float()))
        batch_op.add_column(sa.Column("gross_realized_pnl", sa.Float()))
        batch_op.add_column(sa.Column("realized_pnl", sa.Float()))


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch_op:
        batch_op.drop_column("realized_pnl")
        batch_op.drop_column("gross_realized_pnl")
        batch_op.drop_column("average_entry_cost")
