"""agent factory phase 2: agent run usage attribution

Revision ID: c7e9b1d3f5a8
Revises: b5d7f9a1c3e6
Create Date: 2026-10-01 18:00:00.000000

docs/29 §17 (`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]`). Adds
`agent_run_usage`, joining a run to each usage event it caused. The locked
`usage_events` entity is not changed. Additive.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c7e9b1d3f5a8"
down_revision: Union[str, Sequence[str], None] = "b5d7f9a1c3e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_run_usage",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("usage_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"]),
        sa.ForeignKeyConstraint(["usage_id"], ["usage_events.usage_id"]),
        sa.PrimaryKeyConstraint("run_id", "usage_id"),
    )
    op.create_index("ix_agent_run_usage_usage", "agent_run_usage", ["usage_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_agent_run_usage_usage", table_name="agent_run_usage")
    op.drop_table("agent_run_usage")
