"""agent factory phase 2: agent runs

Revision ID: f2a4c6e8b0d1
Revises: e1f3a5c7b9d2
Create Date: 2026-10-01 09:00:00.000000

docs/29 Phase 2 (`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]`, OD-AF-10).
Adds `agent_runs`: one row per on-demand run — the agent version and spec hash
it ran, the ordinary task it ran as, and how it ended. Additive; nothing
writes to it unless `agents.enabled`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f2a4c6e8b0d1"
down_revision: Union[str, Sequence[str], None] = "e1f3a5c7b9d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_runs",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("spec_hash", sa.String(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("failure_code", sa.String(), nullable=True),
        sa.Column("cost_total", sa.Float(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('on_demand')", name="ck_agent_runs_kind"),
        sa.CheckConstraint(
            "status IN ('queued','running','waiting','completed','failed','cancelled')", name="ck_agent_runs_status"
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agent_definitions.agent_id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.task_id"]),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("task_id"),
    )
    op.create_index("ix_agent_runs_agent_started", "agent_runs", ["agent_id", "started_at"])
    op.create_index("ix_agent_runs_owner", "agent_runs", ["owner_user_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_owner", table_name="agent_runs")
    op.drop_index("ix_agent_runs_agent_started", table_name="agent_runs")
    op.drop_table("agent_runs")
