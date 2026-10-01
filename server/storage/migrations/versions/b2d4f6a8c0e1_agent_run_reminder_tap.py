"""agent factory phase 4: reminder-tap runs

Revision ID: b2d4f6a8c0e1
Revises: f8c0e2a4b6d9
Create Date: 2026-10-02 15:00:00.000000

docs/29 §17.1, §22.1 (`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]`). A run
tapped from one of the agent's reminders is the present owner's ordinary run,
labelled `reminder_tap` and bound (uniquely) to the delivery it was tapped
from, so a double tap is one run. Widens `ck_agent_runs_kind` by that value.

Downgrade: a `reminder_tap` run becomes `on_demand` — it was the present
owner's own run either way; only the label goes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b2d4f6a8c0e1"
down_revision: Union[str, Sequence[str], None] = "f8c0e2a4b6d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = "kind IN ('on_demand')"
_NEW = "kind IN ('on_demand','reminder_tap')"


def upgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch:
        batch.add_column(sa.Column("reminder_delivery_id", sa.Uuid(), nullable=True))
        batch.drop_constraint("ck_agent_runs_kind", type_="check")
        batch.create_check_constraint("ck_agent_runs_kind", _NEW)
        batch.create_index("uq_agent_runs_reminder_delivery", ["reminder_delivery_id"], unique=True)


def downgrade() -> None:
    op.execute("UPDATE agent_runs SET kind = 'on_demand' WHERE kind = 'reminder_tap'")
    with op.batch_alter_table("agent_runs") as batch:
        batch.drop_index("uq_agent_runs_reminder_delivery")
        batch.drop_constraint("ck_agent_runs_kind", type_="check")
        batch.create_check_constraint("ck_agent_runs_kind", _OLD)
        batch.drop_column("reminder_delivery_id")
