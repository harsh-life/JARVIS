"""Resource entities (visibility-aware) — FileResource, ScheduledJob.

Source: 01_DATA_MODEL_SCHEMA.md §6.

Foundation represents the *metadata row* shape for each (both are
"Storage scope: ... metadata row server-global" per 01 §6.1/§6.2 — i.e.
part of the relational identity store). It does not implement:
  - the filesystem sandbox / path-normalization / traversal defenses that
    give `FileResource.relative_path` its safety guarantee (09_FILESYSTEM_SANDBOX.md), or
  - the scheduler that actually fires a ScheduledJob (APScheduler-class
    mechanism, PRD §22).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import Field, field_validator

from shared.schemas.common import ORMBase, utcnow
from shared.schemas.enums import JobStatus, Visibility


class FileResource(ORMBase):
    """PRD FS-001, RAUTH-003 (01 §6.1).

    `relative_path` MUST pass path-normalization + traversal + symlink
    checks — that algorithm belongs to 09_FILESYSTEM_SANDBOX.md and is not
    implemented here. Foundation only stores the metadata row; it does not
    open, read, or write the underlying file.
    """

    file_id: UUID = Field(default_factory=uuid4)
    owner_user_id: UUID
    source_user_id: UUID
    graph_id: UUID | None = None
    visibility: Visibility = Visibility.PRIVATE
    sandbox_root: str
    relative_path: str
    size_bytes: int = Field(ge=0)
    created_at: datetime = Field(default_factory=utcnow)


class ScheduledJob(ORMBase):
    """PRD SCHED-001, RAUTH-003 (01 §6.2).

    DM-T4 [LOCKED]: an empty/absent user-given `task_reason` is rejected —
    "a job with no user reason must not exist" (no unprompted proactivity).
    Enforced structurally here (Pydantic + DB CHECK constraint in
    server/storage/models.py), independent of any scheduler execution logic.
    """

    job_id: UUID = Field(default_factory=uuid4)
    owner_user_id: UUID
    source_user_id: UUID
    graph_id: UUID | None = None
    visibility: Visibility = Visibility.PRIVATE
    task_reason: str
    schedule: str
    status: JobStatus = JobStatus.ACTIVE
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("task_reason")
    @classmethod
    def _task_reason_required(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError(
                "ScheduledJob.task_reason must be a non-empty, user-given "
                "reason (SCHED-001) — no unprompted proactivity"
            )
        return v
