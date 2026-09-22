"""agent runtime: agent_tasks lifecycle table

Revision ID: 7e2b9d4c1f05
Revises: 1a8103630d99
Create Date: 2026-09-22 12:00:00.000000

Adds the one table the runtime branch needs: `agent_tasks`, the lifecycle record
behind `02` §5's `task_id` / `AgentResult`. It holds status, counters, and the
final response or failure code — never the working transcript or a paused
action's arguments, which stay session-volatile (MEM-001). See the ORM model in
`server/storage/models.py` and docs/DECISION_REGISTER.md §2.

Additive: no existing table is altered.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7e2b9d4c1f05"
down_revision: Union[str, Sequence[str], None] = "1a8103630d99"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_tasks",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("graph_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("failure_code", sa.String(), nullable=True),
        sa.Column("response", sa.String(), nullable=True),
        sa.Column("iterations", sa.Integer(), nullable=False),
        sa.Column("model_calls", sa.Integer(), nullable=False),
        sa.Column("tool_calls", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running','awaiting_confirmation','completed','failed','cancelled')",
            name="ck_agent_tasks_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["device_id"], ["devices.device_id"]),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.session_id"]),
        sa.ForeignKeyConstraint(["graph_id"], ["graphs.graph_id"]),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index(
        "ix_agent_tasks_user_created", "agent_tasks", ["user_id", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_agent_tasks_user_created", table_name="agent_tasks")
    op.drop_table("agent_tasks")
