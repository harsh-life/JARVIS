"""Observability & accounting entities — AuditEvent, UsageEvent.

Source: 01_DATA_MODEL_SCHEMA.md §11. Both are append-only records in the
relational identity store (STORE-001a). Foundation defines the shapes and
the append-only storage; it does not implement the authorization/metering
*logic* that decides what gets written (that arrives with the authorization
engine, agent runtime, and tool execution branches).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import Field

from shared.schemas.common import ORMBase, utcnow
from shared.schemas.enums import AuditActor, AuditResult, PermissionDecisionValue, UsageKind


class AuditEvent(ORMBase):
    """PRD §30 threat model, §44 (01 §11.1) — the canonical security/audit
    record. Append-only; never edited/deleted except by a future lifecycle
    purge (LIFE-001), which foundation does not implement.
    """

    event_id: UUID = Field(default_factory=uuid4)
    request_id: UUID
    user_id: UUID | None = None
    device_id: UUID | None = None
    session_id: UUID | None = None
    graph_id: UUID | None = None
    actor: AuditActor
    action: str
    resource: str
    decision: PermissionDecisionValue | None = None  # None represents "n/a"
    result: AuditResult
    timestamp: datetime = Field(default_factory=utcnow)


class UsageEvent(ORMBase):
    """PRD USAGE-001..003 (01 §11.2) — the metering substrate. Every
    model_call/tool_call is required (by later branches) to emit exactly
    one of these before returning. Foundation stores the shape; no code
    here emits one, because no model/tool calls exist yet to meter.
    """

    usage_id: UUID = Field(default_factory=uuid4)
    request_id: UUID
    user_id: UUID
    device_id: UUID | None = None
    session_id: UUID | None = None
    graph_id: UUID | None = None
    kind: UsageKind
    provider: str | None = None
    model: str | None = None
    tool_id: str | None = None
    tokens_or_units: int = Field(ge=0)
    estimated_cost: float = Field(ge=0.0)
    timestamp: datetime = Field(default_factory=utcnow)
