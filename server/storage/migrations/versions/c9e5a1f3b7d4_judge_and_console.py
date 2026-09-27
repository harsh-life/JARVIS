"""judge: evaluations, improvement review queue, versioned config, switches

Revision ID: c9e5a1f3b7d4
Revises: b6d4f8a2c1e7
Create Date: 2026-09-27 21:00:00.000000

Stage 5 (docs/19, docs/28). Adds:
* `evaluations` — one row per evaluation attempt, whatever its outcome
  (19 §5, §7); a record, never a control. Owner-private (11's triplet).
* `improvement_candidates` — the review queue (19 §9). Nothing in it applies
  until a superuser approves it.
* `config_versions` — append-only history of approved changes and rollbacks;
  a target's effective value is its latest row.
* `evaluation_control` — the operator's runtime switches for the Judge, one
  row, each capped by configuration.

Additive: no existing table is altered.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c9e5a1f3b7d4"
down_revision: Union[str, Sequence[str], None] = "b6d4f8a2c1e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evaluations",
        sa.Column("evaluation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("graph_id", sa.Uuid(), nullable=True),
        sa.Column("visibility", sa.Enum("private", "graph", name="visibility", native_enum=False), nullable=False),
        sa.Column("evaluator_id", sa.String(), nullable=False),
        sa.Column("evaluator_version", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("reason_code", sa.String(), nullable=True),
        sa.Column("quality", sa.Float(), nullable=True),
        sa.Column("efficiency", sa.Float(), nullable=True),
        sa.Column("anomaly", sa.String(), nullable=False),
        sa.Column("anomaly_reason", sa.String(), nullable=True),
        sa.Column("findings", sa.JSON(), nullable=True),
        sa.Column("stop_requested", sa.Boolean(), nullable=False),
        sa.Column("stop_honoured", sa.Boolean(), nullable=False),
        sa.Column("stop_not_honoured_because", sa.String(), nullable=True),
        sa.Column("redactions", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.task_id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["graph_id"], ["graphs.graph_id"]),
        sa.PrimaryKeyConstraint("evaluation_id"),
    )
    op.create_index("ix_evaluations_task_id", "evaluations", ["task_id"])
    op.create_index("ix_evaluations_owner_user_id", "evaluations", ["owner_user_id"])
    op.create_index("ix_evaluations_created_at", "evaluations", ["created_at"])

    op.create_table(
        "improvement_candidates",
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("evaluation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("source_user_id", sa.Uuid(), nullable=False),
        sa.Column("evaluator_id", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=True),
        sa.Column("proposed_value", sa.String(), nullable=False),
        sa.Column("expected_effect", sa.String(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(), nullable=True),
        sa.Column("decision_reason", sa.String(), nullable=True),
        sa.Column("config_version_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["evaluation_id"], ["evaluations.evaluation_id"]),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.task_id"]),
        sa.ForeignKeyConstraint(["source_user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("candidate_id"),
    )
    op.create_index("ix_improvement_candidates_evaluation_id", "improvement_candidates", ["evaluation_id"])
    op.create_index("ix_improvement_candidates_status", "improvement_candidates", ["status"])

    op.create_table(
        "config_versions",
        sa.Column("version_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("target_key", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("rolled_back_version_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["candidate_id"], ["improvement_candidates.candidate_id"]),
        sa.PrimaryKeyConstraint("version_id"),
    )
    op.create_index("ix_config_versions_target_key", "config_versions", ["target_key"])

    op.create_table(
        "evaluation_control",
        sa.Column("control_id", sa.Integer(), nullable=False),
        sa.Column("judge_enabled", sa.Boolean(), nullable=False),
        sa.Column("stop_requests_enabled", sa.Boolean(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("changed_by", sa.String(), nullable=True),
        sa.CheckConstraint("control_id = 1", name="ck_evaluation_control_single_row"),
        sa.PrimaryKeyConstraint("control_id"),
    )


def downgrade() -> None:
    op.drop_table("evaluation_control")
    op.drop_index("ix_config_versions_target_key", table_name="config_versions")
    op.drop_table("config_versions")
    op.drop_index("ix_improvement_candidates_status", table_name="improvement_candidates")
    op.drop_index("ix_improvement_candidates_evaluation_id", table_name="improvement_candidates")
    op.drop_table("improvement_candidates")
    op.drop_index("ix_evaluations_created_at", table_name="evaluations")
    op.drop_index("ix_evaluations_owner_user_id", table_name="evaluations")
    op.drop_index("ix_evaluations_task_id", table_name="evaluations")
    op.drop_table("evaluations")
