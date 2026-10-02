"""agent factory phase 5: standing delegations and unattended runs

Revision ID: d1f3b5a7c9e2
Revises: b2d4f6a8c0e1
Create Date: 2026-10-02 18:00:00.000000

docs/29 §15, §22.1 (Phase 5). OD-AF-2, ratified 2026-10-02
(docs/DECISION_REGISTER.md §2K; `01` §7.1A). Adds:

* `standing_delegations` — the owner's step-up-confirmed, expiring,
  budget- and run-limited permission for one agent to run unattended, bound
  to its spec version, hash and envelope hash; at most one active per agent;
* `agent_tasks.delegation_id`, with `device_id`/`session_id` now nullable —
  a task is either a present user's (device + session) or a delegation's
  (neither), never both and never neither (`ck_agent_tasks_principal`);
* `agent_runs.delegation_id` + `occurrence_at` and the `unattended` kind —
  one run per schedule occurrence (`uq_agent_runs_occurrence`);
* inbox `notice` items (`kind`, `notice`, `delegation_id`; `run_id` now
  nullable) — the owner's notifications about a delegation.

Downgrade removes every unattended run, delegated task and notice (with the
rows that hang off them) before restoring the old constraints: those rows
cannot exist in the earlier schema.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d1f3b5a7c9e2"
down_revision: Union[str, Sequence[str], None] = "b2d4f6a8c0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TASK_PRINCIPAL = (
    "(device_id IS NOT NULL AND session_id IS NOT NULL AND delegation_id IS NULL)"
    " OR (device_id IS NULL AND session_id IS NULL AND delegation_id IS NOT NULL)"
)
_RUN_KIND_OLD = "kind IN ('on_demand','reminder_tap')"
_RUN_KIND_NEW = "kind IN ('on_demand','reminder_tap','unattended')"
_RUN_DELEGATION = (
    "(kind = 'unattended' AND delegation_id IS NOT NULL AND occurrence_at IS NOT NULL)"
    " OR (kind <> 'unattended' AND delegation_id IS NULL AND occurrence_at IS NULL)"
)
_INBOX_STATUS_OLD = "status IN ('completed','failed','cancelled')"
_INBOX_STATUS_NEW = "status IN ('completed','failed','cancelled','notice')"
_INBOX_KIND = (
    "(kind = 'result' AND run_id IS NOT NULL AND notice IS NULL AND status <> 'notice')"
    " OR (kind = 'notice' AND run_id IS NULL AND notice IS NOT NULL AND status = 'notice')"
)


def upgrade() -> None:
    op.create_table(
        "standing_delegations",
        sa.Column("delegation_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("graph_id", sa.Uuid(), nullable=True),
        sa.Column("spec_version", sa.Integer(), nullable=False),
        sa.Column("spec_hash", sa.String(), nullable=False),
        sa.Column("envelope_hash", sa.String(), nullable=False),
        sa.Column("allowed_trigger_cron", sa.String(), nullable=False),
        sa.Column("timezone", sa.String(), nullable=False),
        sa.Column("max_runs_per_day", sa.Integer(), nullable=False),
        sa.Column("budget_per_run", sa.Float(), nullable=False),
        sa.Column("budget_per_month", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_with_step_up", sa.Boolean(), nullable=False),
        sa.Column("created_by_device_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_session_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("status_reason", sa.String(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_occurrence_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expiry_notice_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('active','revoked','expired','invalidated')",
                           name="ck_standing_delegations_status"),
        sa.CheckConstraint("created_with_step_up", name="ck_standing_delegations_step_up"),
        sa.CheckConstraint("max_runs_per_day >= 1 AND max_runs_per_day <= 24", name="ck_standing_delegations_runs"),
        sa.CheckConstraint("budget_per_run > 0 AND budget_per_month > 0", name="ck_standing_delegations_budget"),
        sa.CheckConstraint("expires_at > created_at", name="ck_standing_delegations_expiry"),
        sa.CheckConstraint("spec_version >= 1", name="ck_standing_delegations_version"),
        sa.ForeignKeyConstraint(["agent_id"], ["agent_definitions.agent_id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["graph_id"], ["graphs.graph_id"]),
        sa.PrimaryKeyConstraint("delegation_id"),
    )
    op.create_index("uq_standing_delegations_active", "standing_delegations", ["agent_id"], unique=True,
                    sqlite_where=sa.text("status = 'active'"), postgresql_where=sa.text("status = 'active'"))
    op.create_index("ix_standing_delegations_status", "standing_delegations", ["status"])
    op.create_index("ix_standing_delegations_owner", "standing_delegations", ["owner_user_id"])

    with op.batch_alter_table("agent_tasks") as batch:
        batch.alter_column("device_id", existing_type=sa.Uuid(), nullable=True)
        batch.alter_column("session_id", existing_type=sa.Uuid(), nullable=True)
        batch.add_column(sa.Column("delegation_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key("fk_agent_tasks_delegation", "standing_delegations",
                                 ["delegation_id"], ["delegation_id"])
        batch.create_check_constraint("ck_agent_tasks_principal", _TASK_PRINCIPAL)

    with op.batch_alter_table("agent_runs") as batch:
        batch.add_column(sa.Column("delegation_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("occurrence_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key("fk_agent_runs_delegation", "standing_delegations",
                                 ["delegation_id"], ["delegation_id"])
        batch.drop_constraint("ck_agent_runs_kind", type_="check")
        batch.create_check_constraint("ck_agent_runs_kind", _RUN_KIND_NEW)
        batch.create_check_constraint("ck_agent_runs_delegation", _RUN_DELEGATION)
        batch.create_index("uq_agent_runs_occurrence", ["delegation_id", "occurrence_at"], unique=True)

    with op.batch_alter_table("agent_inbox_items") as batch:
        batch.alter_column("run_id", existing_type=sa.Uuid(), nullable=True)
        batch.add_column(sa.Column("kind", sa.String(), nullable=False, server_default="result"))
        batch.add_column(sa.Column("notice", sa.String(), nullable=True))
        batch.add_column(sa.Column("delegation_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key("fk_agent_inbox_items_delegation", "standing_delegations",
                                 ["delegation_id"], ["delegation_id"])
        batch.drop_constraint("ck_agent_inbox_items_status", type_="check")
        batch.create_check_constraint("ck_agent_inbox_items_status", _INBOX_STATUS_NEW)
        batch.create_check_constraint("ck_agent_inbox_items_kind", _INBOX_KIND)


def downgrade() -> None:
    unattended_runs = "SELECT run_id FROM agent_runs WHERE kind = 'unattended'"
    delegated_tasks = "SELECT task_id FROM agent_tasks WHERE delegation_id IS NOT NULL"
    op.execute(f"DELETE FROM agent_inbox_items WHERE kind = 'notice' OR run_id IN ({unattended_runs})")
    op.execute("DELETE FROM agent_gateway_nonces WHERE token_id IN "
               f"(SELECT token_id FROM agent_run_tokens WHERE run_id IN ({unattended_runs}))")
    op.execute(f"DELETE FROM agent_run_tokens WHERE run_id IN ({unattended_runs})")
    op.execute(f"DELETE FROM agent_run_usage WHERE run_id IN ({unattended_runs})")
    op.execute("DELETE FROM agent_runs WHERE kind = 'unattended'")
    op.execute(f"UPDATE scheduled_jobs SET origin_task_id = NULL WHERE origin_task_id IN ({delegated_tasks})")
    op.execute(f"DELETE FROM improvement_candidates WHERE task_id IN ({delegated_tasks})")
    op.execute(f"DELETE FROM evaluations WHERE task_id IN ({delegated_tasks})")
    op.execute("DELETE FROM agent_tasks WHERE delegation_id IS NOT NULL")

    with op.batch_alter_table("agent_inbox_items") as batch:
        batch.drop_constraint("ck_agent_inbox_items_kind", type_="check")
        batch.drop_constraint("ck_agent_inbox_items_status", type_="check")
        batch.create_check_constraint("ck_agent_inbox_items_status", _INBOX_STATUS_OLD)
        batch.drop_constraint("fk_agent_inbox_items_delegation", type_="foreignkey")
        batch.drop_column("delegation_id")
        batch.drop_column("notice")
        batch.drop_column("kind")
        batch.alter_column("run_id", existing_type=sa.Uuid(), nullable=False)

    with op.batch_alter_table("agent_runs") as batch:
        batch.drop_index("uq_agent_runs_occurrence")
        batch.drop_constraint("ck_agent_runs_delegation", type_="check")
        batch.drop_constraint("ck_agent_runs_kind", type_="check")
        batch.create_check_constraint("ck_agent_runs_kind", _RUN_KIND_OLD)
        batch.drop_constraint("fk_agent_runs_delegation", type_="foreignkey")
        batch.drop_column("occurrence_at")
        batch.drop_column("delegation_id")

    with op.batch_alter_table("agent_tasks") as batch:
        batch.drop_constraint("ck_agent_tasks_principal", type_="check")
        batch.drop_constraint("fk_agent_tasks_delegation", type_="foreignkey")
        batch.drop_column("delegation_id")
        batch.alter_column("session_id", existing_type=sa.Uuid(), nullable=False)
        batch.alter_column("device_id", existing_type=sa.Uuid(), nullable=False)

    op.drop_index("ix_standing_delegations_owner", table_name="standing_delegations")
    op.drop_index("ix_standing_delegations_status", table_name="standing_delegations")
    op.drop_index("uq_standing_delegations_active", table_name="standing_delegations")
    op.drop_table("standing_delegations")
