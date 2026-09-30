"""agent factory: definitions, immutable spec versions, compile previews

Revision ID: e1f3a5c7b9d2
Revises: a2d6e8f4c0b9
Create Date: 2026-09-30 20:24:32.957576

docs/29 Phase 1 (`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]`, OD-AF-10).
Adds:
* `agent_definitions` — the mutable head of an owner-private agent (status,
  current version); a tombstone survives deletion with its name cleared.
* `agent_spec_versions` — immutable CompiledAgentSpecs as canonical JSON with
  their SHA-256; purged when the agent is deleted.
* `agent_compile_previews` — owner- and task-bound, single-use, expiring
  compile results awaiting the owner's approval.

Additive: no existing table is altered. Nothing writes to these tables unless
`agents.enabled`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e1f3a5c7b9d2"
down_revision: Union[str, Sequence[str], None] = "a2d6e8f4c0b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_compile_previews",
        sa.Column("compile_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("base_version", sa.Integer(), nullable=True),
        sa.Column("spec_hash", sa.String(), nullable=False),
        sa.Column("spec_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("compile_id"),
    )
    op.create_index("ix_agent_compile_previews_expires_at", "agent_compile_previews", ["expires_at"])
    op.create_index("ix_agent_compile_previews_owner", "agent_compile_previews", ["owner_user_id"])

    op.create_table(
        "agent_definitions",
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("graph_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("status_reason", sa.String(), nullable=True),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("visibility", sa.Enum("private", "graph", name="visibility", native_enum=False), nullable=False),
        sa.Column("created_from_task_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_device_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('awaiting_confirmation','active','paused','needs_reapproval','revoked','deleted')",
            name="ck_agent_definitions_status",
        ),
        sa.CheckConstraint("visibility = 'private'", name="ck_agent_definitions_private"),
        sa.CheckConstraint("current_version >= 1", name="ck_agent_definitions_version"),
        sa.ForeignKeyConstraint(["graph_id"], ["graphs.graph_id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("agent_id"),
    )
    op.create_index("ix_agent_definitions_owner", "agent_definitions", ["owner_user_id", "status"])

    op.create_table(
        "agent_spec_versions",
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("spec_hash", sa.String(), nullable=False),
        sa.Column("spec_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agent_definitions.agent_id"]),
        sa.PrimaryKeyConstraint("agent_id", "version"),
    )


def downgrade() -> None:
    op.drop_table("agent_spec_versions")
    op.drop_index("ix_agent_definitions_owner", table_name="agent_definitions")
    op.drop_table("agent_definitions")
    op.drop_index("ix_agent_compile_previews_owner", table_name="agent_compile_previews")
    op.drop_index("ix_agent_compile_previews_expires_at", table_name="agent_compile_previews")
    op.drop_table("agent_compile_previews")
