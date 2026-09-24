"""supervisor: global emergency latch

Revision ID: 9b4e2f7a1c30
Revises: 7e2b9d4c1f05
Create Date: 2026-09-24 12:00:00.000000

Adds `supervisor_latch`, the single-row, persisted global emergency latch of
docs/18_SUPERVISORY_RUNTIME_RECOVERY_CONTROL.md §5.4. Persisted rather than
held only in memory so that a restart cannot clear a latch the operator set
(18 §5.2: cleared only by the operator). See `SupervisorLatch` in
`server/storage/models.py`.

Additive: no existing table is altered.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9b4e2f7a1c30"
down_revision: Union[str, Sequence[str], None] = "7e2b9d4c1f05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "supervisor_latch",
        sa.Column("latch_id", sa.Integer(), nullable=False),
        sa.Column("latched", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("changed_by", sa.String(), nullable=True),
        sa.CheckConstraint("latch_id = 1", name="ck_supervisor_latch_single_row"),
        sa.PrimaryKeyConstraint("latch_id"),
    )


def downgrade() -> None:
    op.drop_table("supervisor_latch")
