"""agent factory phase 2: agent inbox

Revision ID: b5d7f9a1c3e6
Revises: a3c5e7f9b1d4
Create Date: 2026-10-01 15:00:00.000000

docs/29 §19 (`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]`). Adds
`agent_inbox_items`: one item per finished agent run, delivered to the run's
owner only — plain, bounded text, never parsed for authority. Additive;
nothing writes to it unless `agents.enabled`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b5d7f9a1c3e6"
down_revision: Union[str, Sequence[str], None] = "a3c5e7f9b1d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_inbox_items",
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("failure_code", sa.String(), nullable=True),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("withheld", sa.Boolean(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('completed','failed','cancelled')", name="ck_agent_inbox_items_status"),
        sa.ForeignKeyConstraint(["agent_id"], ["agent_definitions.agent_id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"]),
        sa.PrimaryKeyConstraint("item_id"),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index("ix_agent_inbox_items_owner_created", "agent_inbox_items", ["owner_user_id", "created_at"])
    op.create_index("ix_agent_inbox_items_agent", "agent_inbox_items", ["agent_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_inbox_items_agent", table_name="agent_inbox_items")
    op.drop_index("ix_agent_inbox_items_owner_created", table_name="agent_inbox_items")
    op.drop_table("agent_inbox_items")
