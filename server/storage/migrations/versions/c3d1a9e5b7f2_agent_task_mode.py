"""agent runtime: task mode

Revision ID: c3d1a9e5b7f2
Revises: 9b4e2f7a1c30
Create Date: 2026-09-24 20:00:00.000000

Adds `agent_tasks.mode` — docs/18_SUPERVISORY_RUNTIME_RECOVERY_CONTROL.md §3
(OD-F1): `execute` (the default, today's behaviour), `draft`, `suggest`,
`observe`. Set once at submission by the caller, never by the worker; the
supervisor's mode ceiling reads it.

Additive: existing rows become `execute`, which is what every existing task was.
Batch mode so the CHECK constraint is created on SQLite too.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3d1a9e5b7f2"
down_revision: Union[str, Sequence[str], None] = "9b4e2f7a1c30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("agent_tasks") as batch:
        batch.add_column(sa.Column("mode", sa.String(), nullable=False, server_default="execute"))
        batch.create_check_constraint("ck_agent_tasks_mode", "mode IN ('execute','draft','suggest','observe')")


def downgrade() -> None:
    with op.batch_alter_table("agent_tasks") as batch:
        batch.drop_constraint("ck_agent_tasks_mode", type_="check")
        batch.drop_column("mode")
