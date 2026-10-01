"""agent factory phase 3: owner-scoped improvement candidates

Revision ID: e6b8d0f2a4c7
Revises: d4f6a8c0e2b5
Create Date: 2026-10-02 09:00:00.000000

docs/29 §18 (`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]`). Adds a nullable
`improvement_candidates.agent_id`: for an `agent.purpose` candidate, the agent
whose run the Judge evaluated (from the trace). Such a candidate goes to that
agent's owner only. Additive.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e6b8d0f2a4c7"
down_revision: Union[str, Sequence[str], None] = "d4f6a8c0e2b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("improvement_candidates", sa.Column("agent_id", sa.Uuid(), nullable=True))
    op.create_index("ix_improvement_candidates_agent_id", "improvement_candidates", ["agent_id"])


def downgrade() -> None:
    op.drop_index("ix_improvement_candidates_agent_id", table_name="improvement_candidates")
    with op.batch_alter_table("improvement_candidates") as batch:
        batch.drop_column("agent_id")
