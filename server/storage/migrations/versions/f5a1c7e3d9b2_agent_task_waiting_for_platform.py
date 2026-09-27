"""agent runtime: waiting_for_platform task status

Revision ID: f5a1c7e3d9b2
Revises: e4f7a2c9b1d3
Create Date: 2026-09-27 05:00:00.000000

docs/23 §5.3: a task whose device operation was refused because an on-device
dependency (e.g. Shizuku after a reboot) is unavailable waits, bounded, in
`waiting_for_platform`. The operation itself is never queued; only the task
waits. Widens `ck_agent_tasks_status` by that one value.

Downgrade: a task still waiting cannot be represented by the old constraint,
so it is failed closed first (`platform_unavailable`) — never resumed.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f5a1c7e3d9b2"
down_revision: Union[str, Sequence[str], None] = "e4f7a2c9b1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = "status IN ('running','awaiting_confirmation','completed','failed','cancelled')"
_NEW = "status IN ('running','awaiting_confirmation','waiting_for_platform','completed','failed','cancelled')"


def upgrade() -> None:
    with op.batch_alter_table("agent_tasks") as batch:
        batch.drop_constraint("ck_agent_tasks_status", type_="check")
        batch.create_check_constraint("ck_agent_tasks_status", _NEW)


def downgrade() -> None:
    op.execute(
        "UPDATE agent_tasks SET status = 'failed', failure_code = 'platform_unavailable' "
        "WHERE status = 'waiting_for_platform'"
    )
    with op.batch_alter_table("agent_tasks") as batch:
        batch.drop_constraint("ck_agent_tasks_status", type_="check")
        batch.create_check_constraint("ck_agent_tasks_status", _OLD)
