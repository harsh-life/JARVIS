"""scheduler: task-linked reminders (docs/22)

Revision ID: b6d4f8a2c1e7
Revises: a7c3e9f1b5d2
Create Date: 2026-09-27 12:00:00.000000

Adds to `scheduled_jobs` the scheduler backend's own state and provenance:
* `next_fire_at` — the next due instant; the application database is the one
  job store (docs/22 §3), so nothing about a job lives outside this row.
* `reason_source` / `origin_task_id` / `created_by_device_id` — whose words
  `task_reason` is, and where the job came from (docs/22 §1).

Adds `scheduled_job_firings` (one row per occurrence and its outcome — `01`
§1.2 keeps `job.status` to `active|cancelled|fired`) and `reminder_deliveries`
(reminders owed to the owner's devices; safe to queue — no authority).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b6d4f8a2c1e7"
down_revision: Union[str, Sequence[str], None] = "a7c3e9f1b5d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("scheduled_jobs") as batch:
        batch.add_column(sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("reason_source", sa.String(), nullable=False, server_default="user"))
        batch.add_column(sa.Column("origin_task_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("created_by_device_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_scheduled_jobs_origin_task", "agent_tasks", ["origin_task_id"], ["task_id"]
        )
        batch.create_foreign_key(
            "fk_scheduled_jobs_created_by_device", "devices", ["created_by_device_id"], ["device_id"]
        )
        batch.create_check_constraint(
            "ck_scheduled_jobs_reason_source", "reason_source IN ('user','task_input')"
        )
        batch.create_index("ix_scheduled_jobs_due", ["status", "next_fire_at"])
        batch.create_index("ix_scheduled_jobs_owner_created", ["owner_user_id", "created_at"])

    op.create_table(
        "scheduled_job_firings",
        sa.Column("firing_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("late", sa.Boolean(), nullable=False),
        sa.Column("coalesced", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('delivered','queued','undeliverable','missed','cancelled_recheck')",
            name="ck_scheduled_job_firings_outcome",
        ),
        sa.CheckConstraint("coalesced >= 1", name="ck_scheduled_job_firings_coalesced"),
        sa.ForeignKeyConstraint(["job_id"], ["scheduled_jobs.job_id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("firing_id"),
    )
    op.create_index(
        "uq_scheduled_job_firings_occurrence", "scheduled_job_firings", ["job_id", "scheduled_for"], unique=True
    )
    op.create_index("ix_scheduled_job_firings_owner", "scheduled_job_firings", ["owner_user_id", "fired_at"])

    op.create_table(
        "reminder_deliveries",
        sa.Column("delivery_id", sa.Uuid(), nullable=False),
        sa.Column("firing_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','sent','acked','dropped','expired')", name="ck_reminder_deliveries_status"
        ),
        sa.ForeignKeyConstraint(["firing_id"], ["scheduled_job_firings.firing_id"]),
        sa.ForeignKeyConstraint(["job_id"], ["scheduled_jobs.job_id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["device_id"], ["devices.device_id"]),
        sa.PrimaryKeyConstraint("delivery_id"),
    )
    op.create_index(
        "uq_reminder_deliveries_firing_device", "reminder_deliveries", ["firing_id", "device_id"], unique=True
    )
    op.create_index("ix_reminder_deliveries_device_status", "reminder_deliveries", ["device_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_reminder_deliveries_device_status", table_name="reminder_deliveries")
    op.drop_index("uq_reminder_deliveries_firing_device", table_name="reminder_deliveries")
    op.drop_table("reminder_deliveries")
    op.drop_index("ix_scheduled_job_firings_owner", table_name="scheduled_job_firings")
    op.drop_index("uq_scheduled_job_firings_occurrence", table_name="scheduled_job_firings")
    op.drop_table("scheduled_job_firings")
    with op.batch_alter_table("scheduled_jobs") as batch:
        batch.drop_index("ix_scheduled_jobs_owner_created")
        batch.drop_index("ix_scheduled_jobs_due")
        batch.drop_constraint("ck_scheduled_jobs_reason_source", type_="check")
        batch.drop_constraint("fk_scheduled_jobs_created_by_device", type_="foreignkey")
        batch.drop_constraint("fk_scheduled_jobs_origin_task", type_="foreignkey")
        batch.drop_column("created_by_device_id")
        batch.drop_column("origin_task_id")
        batch.drop_column("reason_source")
        batch.drop_column("next_fire_at")
