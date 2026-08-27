"""reset legacy automatic universe approval

Revision ID: 5f74c1a9d2b0
Revises: 0984b8c06f2e
Create Date: 2026-08-13

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "5f74c1a9d2b0"
down_revision: Union[str, Sequence[str], None] = "0984b8c06f2e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_LEGACY_AUTOMATIC_APPROVER = "implementation_plan_2026-07-17"


def upgrade() -> None:
    # The old trusted-data loader approved this universe in the same operation
    # that created it. Preserve the provenance fields for audit, but require the
    # new independent approval command to move it out of draft again.
    op.execute(
        sa.text(
            """
            UPDATE universe_versions
            SET status = 'draft'
            WHERE status = 'approved'
              AND approved_by = :legacy_approver
            """
        ).bindparams(legacy_approver=_LEGACY_AUTOMATIC_APPROVER)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE universe_versions
            SET status = 'approved'
            WHERE status = 'draft'
              AND approved_by = :legacy_approver
            """
        ).bindparams(legacy_approver=_LEGACY_AUTOMATIC_APPROVER)
    )
