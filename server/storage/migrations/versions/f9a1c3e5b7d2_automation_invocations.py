"""automation: the durable idempotency record of n8n workflow calls

Revision ID: f9a1c3e5b7d2
Revises: c7e9a1b3d5f8
Create Date: 2026-11-27 12:00:00.000000

docs/32 ([PROPOSAL] OD-AUTO-1), 2026-11-27 hardening. The executor's
duplicate prevention was an in-process LRU that did not survive a restart,
eviction or another process. `automation_invocations` holds one row per
idempotency key, written before the call is sent. Additive: no existing table
changes; downgrade drops only this table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f9a1c3e5b7d2"
down_revision: Union[str, Sequence[str], None] = "c7e9a1b3d5f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "automation_invocations",
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("workflow_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("result_content", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('reserved', 'in_progress', 'completed', 'failed', 'failed_before_send', "
            "'outcome_uncertain')",
            name="ck_automation_invocations_state",
        ),
        sa.PrimaryKeyConstraint("idempotency_key"),
    )
    op.create_index("ix_automation_invocations_user_id", "automation_invocations", ["user_id"])
    op.create_index("ix_automation_invocations_updated_at", "automation_invocations", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_automation_invocations_updated_at", table_name="automation_invocations")
    op.drop_index("ix_automation_invocations_user_id", table_name="automation_invocations")
    op.drop_table("automation_invocations")
