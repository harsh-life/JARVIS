"""agent runtime: worker switches counter

Revision ID: d8e2b6c4a0f1
Revises: c3d1a9e5b7f2
Create Date: 2026-09-24 21:00:00.000000

Adds `agent_tasks.worker_switches` — how many times the supervisor replaced
the task's worker (docs/18_SUPERVISORY_RUNTIME_RECOVERY_CONTROL.md §4, §7). A
counter, like `iterations`; never content.

Additive: existing rows get 0.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8e2b6c4a0f1"
down_revision: Union[str, Sequence[str], None] = "c3d1a9e5b7f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("agent_tasks") as batch:
        batch.add_column(sa.Column("worker_switches", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("agent_tasks") as batch:
        batch.drop_column("worker_switches")
