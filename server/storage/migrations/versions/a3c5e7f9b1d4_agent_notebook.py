"""agent factory phase 2: agent notebook

Revision ID: a3c5e7f9b1d4
Revises: f2a4c6e8b0d1
Create Date: 2026-10-01 12:00:00.000000

docs/29 §16.3 (`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]`). Adds
`agent_notebook_entries`: an agent's own operational notes between runs,
owner-private, keyed by (agent, key). Additive; nothing writes to it unless
`agents.enabled` and the agent's approved spec enables its notebook.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a3c5e7f9b1d4"
down_revision: Union[str, Sequence[str], None] = "f2a4c6e8b0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_notebook_entries",
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("updated_by_run_id", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agent_definitions.agent_id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("agent_id", "key"),
    )
    op.create_index("ix_agent_notebook_entries_owner", "agent_notebook_entries", ["owner_user_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_notebook_entries_owner", table_name="agent_notebook_entries")
    op.drop_table("agent_notebook_entries")
