"""Scheduler API shapes (02 §9, docs/22).

`JobCreateRequest` carries no owner, source, visibility or device field: a new
reminder is the caller's, `private`, created from the caller's own session
(RAUTH-005, PHONE-003). `graph_id` is a claim the engine checks (D1), never a
grant. Being strict (`extra="forbid"`), a body that tries to assert any of
those is refused rather than half-read.

`ScheduledJobView` is the `01` §6.2 entity plus what the scheduler adds:
provenance (`reason_source`, `origin_task_id`), the next due instant, and the
last firing's outcome — which is where "missed" is reported (docs/22 §3),
since `job.status` stays the locked `active|cancelled|fired`.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import Field

from shared.schemas.common import ORMBase
from shared.schemas.resources import ScheduledJob


class ReasonSource(str, Enum):
    """Whose words `task_reason` is (docs/22 §1)."""

    # Typed by the user into `POST /api/v1/jobs`.
    USER = "user"
    # Copied by the runtime from the user's own task instruction — never the
    # worker's paraphrase.
    TASK_INPUT = "task_input"


class FiringOutcome(str, Enum):
    DELIVERED = "delivered"
    # Some owner device was offline; the reminder waits for its reconnect.
    QUEUED = "queued"
    # The owner has no active device to deliver to.
    UNDELIVERABLE = "undeliverable"
    # Due while the server was down, beyond `misfire_grace_minutes`.
    MISSED = "missed"
    # The fire-time re-check refused; nothing was delivered.
    CANCELLED_RECHECK = "cancelled_recheck"


class JobCreateRequest(ORMBase):
    task_reason: str = Field(min_length=1, max_length=8000)
    schedule: str = Field(min_length=1, max_length=128)
    graph_id: UUID | None = None


class LastFiring(ORMBase):
    scheduled_for: datetime
    fired_at: datetime
    outcome: FiringOutcome
    late: bool = False
    coalesced: int = 1


class ScheduledJobView(ScheduledJob):
    next_fire_at: datetime | None = None
    reason_source: ReasonSource = ReasonSource.USER
    origin_task_id: UUID | None = None
    last_firing: LastFiring | None = None


class JobListResponse(ORMBase):
    items: list[ScheduledJobView]
    next_cursor: str | None = None


__all__ = [
    "FiringOutcome",
    "JobCreateRequest",
    "JobListResponse",
    "LastFiring",
    "ReasonSource",
    "ScheduledJobView",
]
