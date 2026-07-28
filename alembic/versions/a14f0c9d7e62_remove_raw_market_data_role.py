"""remove accidental role column from mutable raw market data

Revision ID: a14f0c9d7e62
Revises: c8e3f1047a92
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a14f0c9d7e62"
down_revision: Union[str, None] = "c8e3f1047a92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ``role`` belongs only to immutable snapshot rows. Mutable raw rows are
    # uniquely identified by ticker/date/source, as defined in storage.schema.
    with op.batch_alter_table("raw_market_data") as batch_op:
        batch_op.drop_column("role")


def downgrade() -> None:
    with op.batch_alter_table("raw_market_data") as batch_op:
        batch_op.add_column(
            sa.Column(
                "role",
                sa.String(length=20),
                nullable=False,
                server_default="raw",
            )
        )
