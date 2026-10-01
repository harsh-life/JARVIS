"""agent factory phase 4: scheduled_jobs.agent_id

Revision ID: f8c0e2a4b6d9
Revises: e6b8d0f2a4c7
Create Date: 2026-10-02 12:00:00.000000

docs/29 §17.1 (`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]`, the `01` field
addition flagged in §28 M5). A nullable `scheduled_jobs.agent_id`: the agent a
reminder offers to run, as data only — the scheduler never interprets it.
Additive; nothing sets it unless `agents.enabled`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f8c0e2a4b6d9"
down_revision: Union[str, Sequence[str], None] = "e6b8d0f2a4c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("scheduled_jobs", sa.Column("agent_id", sa.Uuid(), nullable=True))
    op.create_index("ix_scheduled_jobs_agent", "scheduled_jobs", ["agent_id"])


def downgrade() -> None:
    op.drop_index("ix_scheduled_jobs_agent", table_name="scheduled_jobs")
    with op.batch_alter_table("scheduled_jobs") as batch:
        batch.drop_column("agent_id")
