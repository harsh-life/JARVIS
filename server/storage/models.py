"""SQLAlchemy ORM models for the relational/identity store.

Source: 01_DATA_MODEL_SCHEMA.md §14 STORE-001(a) — "the relational/identity
store (users/devices/sessions/graphs/memberships/configs/grants/secret-refs/
audit/usage)". Mem0Fact and VaultDocument are deliberately absent from this
module: their storage is Mem0/ChromaDB (STORE-001b/c), a later branch's
concern (see shared/schemas/memory.py). ToolContract/ToolConfiguration are
also absent: 01 §10 recommends a git-backed registry for those, not a table.

These are storage-layer types, distinct from (and mapped to/from) the
Pydantic contracts in `shared/schemas/` — kept deliberately decoupled so the
contracts stay ORM-agnostic (§11 of this branch's instructions: "schema/model
definitions must not be tightly coupled to one ORM-specific assumption").

No column here ever holds a secret *value* (SECRET-004) — see
`SecretReference` below, and `tests/foundation/test_storage.py::test_secret_reference_table_has_no_value_column`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Uuid, text
from sqlalchemy import JSON as SAJSON
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from server.storage.base import Base
from shared.schemas.enums import (
    AgentConfigScopeType,
    AuditActor,
    AuditResult,
    CapabilityScopeType,
    DevicePlatform,
    FactType,  # noqa: F401  (referenced in docstrings; kept for discoverability)
    GraphType,
    JobStatus,
    MembershipRole,
    PermissionDecisionValue,
    RiskCategory,
    SecretClass,
    SecretOwnerScopeType,
    UsageKind,
    UserStatus,
    Visibility,
)


def _sa_enum(enum_cls, name: str) -> SAEnum:
    """Store enum *values* (e.g. "private"), not Python member names (e.g.
    "PRIVATE") — the canonical registry (01 §1.2) is defined in lowercase
    string values, and that is what must round-trip through the DB."""

    return SAEnum(
        enum_cls,
        name=name,
        values_callable=lambda obj: [e.value for e in obj],
        native_enum=False,
    )


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    oidc_subject: Mapped[str] = mapped_column(String, nullable=False)
    oidc_issuer: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[UserStatus] = mapped_column(
        _sa_enum(UserStatus, "user_status"), nullable=False, default=UserStatus.ACTIVE
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_users_issuer_subject", "oidc_issuer", "oidc_subject", unique=True),
    )


class Device(Base):
    __tablename__ = "devices"

    device_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.user_id"), nullable=False)
    platform: Mapped[DevicePlatform] = mapped_column(
        _sa_enum(DevicePlatform, "device_platform"), nullable=False, default=DevicePlatform.ANDROID
    )
    # A handle into the SecretStore (12_SECRETSTORE.md) — never a credential
    # value. Foundation does not implement resolution of this handle.
    credential_ref: Mapped[str] = mapped_column(String, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked: Mapped[bool] = mapped_column(nullable=False, default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_devices_user_id", "user_id"),)


class Session(Base):
    __tablename__ = "sessions"

    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("devices.device_id"), nullable=False
    )
    # Denormalized for fast authZ (01 §2.3) — MUST equal Device.user_id.
    # Enforced at construction time by shared.schemas.identity.new_session_for_device;
    # DM-T6 is tested at that boundary (tests/foundation/test_schemas.py),
    # since SQLite cannot express a cross-table equality CHECK constraint.
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.user_id"), nullable=False)
    active_graph_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("graphs.graph_id"), nullable=True
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scope: Mapped[list | None] = mapped_column(SAJSON, nullable=True)

    __table_args__ = (
        Index("ix_sessions_device_id", "device_id"),
        Index("ix_sessions_user_id", "user_id"),
    )


class Graph(Base):
    __tablename__ = "graphs"

    graph_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.user_id"), nullable=False
    )
    type: Mapped[GraphType] = mapped_column(_sa_enum(GraphType, "graph_type"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_graphs_owner_user_id", "owner_user_id"),)


class GraphMembership(Base):
    __tablename__ = "graph_memberships"

    membership_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    graph_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("graphs.graph_id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.user_id"), nullable=False)
    role: Mapped[MembershipRole] = mapped_column(
        _sa_enum(MembershipRole, "membership_role"), nullable=False
    )
    granted_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.user_id"), nullable=False)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # DM-T7: a user has at most one *active* membership per graph.
        # SQLite/Postgres both support partial unique indexes.
        Index(
            "uq_graph_memberships_active",
            "graph_id",
            "user_id",
            unique=True,
            sqlite_where=text("revoked_at IS NULL"),
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_graph_memberships_user_id", "user_id"),
    )


class AgentConfiguration(Base):
    __tablename__ = "agent_configurations"

    config_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    scope_type: Mapped[AgentConfigScopeType] = mapped_column(
        _sa_enum(AgentConfigScopeType, "agentconfig_scope_type"), nullable=False
    )
    scope_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # Embedded ModelConfiguration (shared.schemas.agent_config) as JSON —
    # secret_ref only inside, never a literal key (01 §4.1/§9.1).
    primary_model: Mapped[dict] = mapped_column(SAJSON, nullable=False)
    model_tools: Mapped[list | None] = mapped_column(SAJSON, nullable=True)
    enabled_tools: Mapped[list | None] = mapped_column(SAJSON, nullable=True)
    granted_capabilities: Mapped[list | None] = mapped_column(SAJSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("uq_agent_configurations_scope", "scope_type", "scope_id", unique=True),
    )


class CapabilityGrant(Base):
    __tablename__ = "capability_grants"

    grant_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    scope_type: Mapped[CapabilityScopeType] = mapped_column(
        _sa_enum(CapabilityScopeType, "capability_scope_type"), nullable=False
    )
    capability: Mapped[str] = mapped_column(String, nullable=False)
    resource_scope: Mapped[dict | None] = mapped_column(SAJSON, nullable=True)
    granted_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.user_id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "ix_capability_grants_principal",
            "principal_id",
            "scope_type",
            "capability",
        ),
    )


class SecretReference(Base):
    """Metadata only — see module docstring. There is deliberately no
    column here, or anywhere in this file, capable of holding a secret
    value; that is asserted by test, not just by convention."""

    __tablename__ = "secret_references"

    secret_ref: Mapped[str] = mapped_column(String, primary_key=True)
    owner_scope_type: Mapped[SecretOwnerScopeType] = mapped_column(
        _sa_enum(SecretOwnerScopeType, "secret_owner_scope_type"), nullable=False
    )
    owner_scope_id: Mapped[str | None] = mapped_column(String, nullable=True)
    class_: Mapped[SecretClass] = mapped_column(
        _sa_enum(SecretClass, "secret_class"), name="class", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_secret_references_owner", "owner_scope_type", "owner_scope_id"),
    )


class AuditEvent(Base):
    """Append-only (PERM-006: the agent cannot disable or write false audit
    entries; foundation enforces "append-only" by never exposing an
    update/delete path for this table — there is no repository code for
    this table at all yet)."""

    __tablename__ = "audit_events"

    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.user_id"), nullable=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("devices.device_id"), nullable=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("sessions.session_id"), nullable=True
    )
    graph_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("graphs.graph_id"), nullable=True)
    actor: Mapped[AuditActor] = mapped_column(_sa_enum(AuditActor, "audit_actor"), nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    resource: Mapped[str] = mapped_column(String, nullable=False)
    decision: Mapped[PermissionDecisionValue | None] = mapped_column(
        _sa_enum(PermissionDecisionValue, "audit_decision"), nullable=True
    )
    result: Mapped[AuditResult] = mapped_column(_sa_enum(AuditResult, "audit_result"), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_audit_events_request_id", "request_id"),
        Index("ix_audit_events_user_timestamp", "user_id", "timestamp"),
        Index("ix_audit_events_graph_timestamp", "graph_id", "timestamp"),
    )


class UsageEvent(Base):
    __tablename__ = "usage_events"

    usage_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.user_id"), nullable=False)
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("devices.device_id"), nullable=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("sessions.session_id"), nullable=True
    )
    graph_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("graphs.graph_id"), nullable=True)
    kind: Mapped[UsageKind] = mapped_column(_sa_enum(UsageKind, "usage_kind"), nullable=False)
    provider: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    tool_id: Mapped[str | None] = mapped_column(String, nullable=True)
    tokens_or_units: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_cost: Mapped[float] = mapped_column(Float, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("tokens_or_units >= 0", name="ck_usage_events_tokens_nonneg"),
        CheckConstraint("estimated_cost >= 0", name="ck_usage_events_cost_nonneg"),
        Index("ix_usage_events_user_timestamp", "user_id", "timestamp"),
        Index("ix_usage_events_graph_timestamp", "graph_id", "timestamp"),
    )


class PermissionDecision(Base):
    """Append-only audited output of an authZ check. No repository code
    writes here yet — 04_AUTHORIZATION_GRAPH_RESOURCE.md's job."""

    __tablename__ = "permission_decisions"

    decision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    capability: Mapped[str] = mapped_column(String, nullable=False)
    resource_ref: Mapped[str] = mapped_column(String, nullable=False)
    decision: Mapped[PermissionDecisionValue] = mapped_column(
        _sa_enum(PermissionDecisionValue, "permission_decision_value"), nullable=False
    )
    risk_category: Mapped[RiskCategory] = mapped_column(
        _sa_enum(RiskCategory, "risk_category"), nullable=False
    )
    reason: Mapped[str] = mapped_column(String, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_permission_decisions_request_id", "request_id"),
        Index("ix_permission_decisions_principal_id", "principal_id"),
    )


class FileResource(Base):
    __tablename__ = "file_resources"

    file_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.user_id"), nullable=False)
    source_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.user_id"), nullable=False)
    graph_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("graphs.graph_id"), nullable=True)
    visibility: Mapped[Visibility] = mapped_column(
        _sa_enum(Visibility, "file_visibility"), nullable=False, default=Visibility.PRIVATE
    )
    sandbox_root: Mapped[str] = mapped_column(String, nullable=False)
    relative_path: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="ck_file_resources_size_nonneg"),
        Index("uq_file_resources_root_path", "sandbox_root", "relative_path", unique=True),
        Index("ix_file_resources_graph_owner", "graph_id", "owner_user_id"),
    )


class ScheduledJob(Base):
    __tablename__ = "scheduled_jobs"

    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.user_id"), nullable=False)
    source_user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.user_id"), nullable=False)
    graph_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("graphs.graph_id"), nullable=True)
    visibility: Mapped[Visibility] = mapped_column(
        _sa_enum(Visibility, "job_visibility"), nullable=False, default=Visibility.PRIVATE
    )
    # SCHED-001 / DM-T4: never empty. Enforced at the Pydantic layer
    # (shared.schemas.resources.ScheduledJob) AND here at the DB layer
    # (defense in depth) — this is a data-integrity rule, not an
    # authorization decision.
    task_reason: Mapped[str] = mapped_column(String, nullable=False)
    schedule: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        _sa_enum(JobStatus, "job_status"), nullable=False, default=JobStatus.ACTIVE
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("length(trim(task_reason)) > 0", name="ck_scheduled_jobs_reason_nonempty"),
        Index("ix_scheduled_jobs_owner_status", "owner_user_id", "status"),
    )


class VoiceEvent(Base):
    __tablename__ = "voice_events"

    voice_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("sessions.session_id"), nullable=False)
    transcript: Mapped[str] = mapped_column(String, nullable=False)
    audio_retained: Mapped[bool] = mapped_column(nullable=False, default=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SpeakerContext(Base):
    __tablename__ = "speaker_contexts"

    speaker_context_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    speaker_id: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    utterance: Mapped[str] = mapped_column(String, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # DM-T8 / INV-14, enforced at the DB layer too (belt-and-suspenders with
    # the Pydantic Literal[False] in shared.schemas.voice.SpeakerContext):
    # this column can never be anything but false.
    is_authorization_signal: Mapped[bool] = mapped_column(nullable=False, default=False)

    __table_args__ = (
        CheckConstraint(
            "is_authorization_signal = 0",
            name="ck_speaker_contexts_never_auth_signal",
        ),
    )


class IdempotencyKey(Base):
    """Foundation's own addition (not in 01_DATA_MODEL_SCHEMA.md — that doc
    doesn't enumerate protocol-infrastructure tables). Backs the reusable
    idempotency abstraction required by 02_API_PROTOCOL.md §1.4; see
    server/storage/idempotency.py. No endpoint in this branch writes to it
    yet (no state-changing endpoints exist)."""

    __tablename__ = "idempotency_keys"

    idempotency_key: Mapped[str] = mapped_column(String, primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict] = mapped_column(SAJSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
