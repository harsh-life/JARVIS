"""What the runtime consumes, as Protocols (05 §0, 16 §3/§5).

`server.agent` may not import `server.graph`, `server.capabilities`, or
`server.secrets` (pyproject contracts: "Agent cannot import the capability/authz
engine", "Agent never imports raw secrets resolution"). So the runtime declares
here what it needs, in its own vocabulary, and the composition root
(`server/composition/`) satisfies each port with the existing Security Core
objects — `AuthorizationEngine`, `ConfirmationService`, `CapabilityGrantService`,
`AuditLogger`, the usage ledger. There is no second authorization engine: every
`authorize_*` call below is `AuthorizationEngine.authorize`.

Note what the ports do **not** offer:

* no way to *grant* a capability except `activate_for_task`, which the runtime
  calls only after a human approved that exact activation with a single-use
  token (PERM-002);
* no way to set a risk tier, waive a confirmation, or mark an action confirmed —
  `Verdict` is produced by the engine and read by the runtime;
* no secret resolution of any kind (SECRET-002, INV-6).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from server.agent.events import AgentEvent
from server.models.provider import ModelProvider
from shared.schemas.agent import ExecutionPlatform, ToolHandle, ToolInvocation, ToolOutput
from shared.schemas.authorization import ActionBinding, Operation, Principal, ResourceType
from shared.schemas.enums import AuditResult, PermissionDecisionValue, RiskCategory, UsageKind


# ── authorization (04 via the composition root) ────────────────────────────


@dataclass(frozen=True)
class ActionRequest:
    """One tool operation, as the **principal** (AGENT-004 — never as "the agent")."""

    principal: Principal
    graph_id: uuid.UUID | None
    task_id: uuid.UUID
    capability: str
    capability_operation: str
    resource_type: ResourceType
    operation: Operation
    resource_ref: str | None
    arguments: Mapping[str, Any]
    resource_scope: Mapping[str, str] | None
    confirmation_token: str | None = None


@dataclass(frozen=True)
class ActivationRequest:
    """Creating a task-scoped capability grant — a `CapabilityGrant` create, which
    the deterministic tier table makes `consequential` (never automatic)."""

    principal: Principal
    graph_id: uuid.UUID | None
    task_id: uuid.UUID
    capability: str
    resource_scope: Mapping[str, str] | None
    confirmation_token: str | None = None


@dataclass(frozen=True)
class Verdict:
    decision: PermissionDecisionValue
    risk_category: RiskCategory
    reason: str
    prohibited: bool = False
    binding: ActionBinding | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is PermissionDecisionValue.ALLOW

    @property
    def needs_confirmation(self) -> bool:
        return self.decision is PermissionDecisionValue.REQUIRE_CONFIRMATION


class CapabilityStatus(str, Enum):
    REGISTERED = "registered"
    PROHIBITED = "prohibited"  # an absolute-floor name (PERM-006)
    UNKNOWN = "unknown"  # not in the closed registry


@dataclass(frozen=True)
class CapabilityInfo:
    status: CapabilityStatus
    scope_keys: frozenset[str] = frozenset()
    # The registry's own tier per enumerated operation (the capability axis).
    operation_tiers: Mapping[str, RiskCategory] = field(default_factory=dict)


@dataclass(frozen=True)
class IssuedConfirmation:
    token: str
    expires_at: datetime


class SecurityPort(Protocol):
    """Request-scoped: bound to the request's transaction and audit writer."""

    async def authorize_action(self, request: ActionRequest) -> Verdict: ...

    async def authorize_activation(self, request: ActivationRequest) -> Verdict: ...

    async def issue_confirmation(
        self, binding: ActionBinding, risk_category: RiskCategory
    ) -> IssuedConfirmation: ...

    def describe_capability(self, capability: str) -> CapabilityInfo: ...

    def operation_tier(
        self,
        *,
        capability: str,
        capability_operation: str,
        resource_type: ResourceType,
        operation: Operation,
    ) -> RiskCategory | None:
        """The engine's deterministic tier for one operation — read-only, for
        the supervisor's mode ceiling (18 §3). `None` if it cannot be
        determined (the ceiling then refuses)."""
        ...

    async def holds_standing_grant(
        self,
        *,
        principal: Principal,
        graph_id: uuid.UUID | None,
        capability: str,
        resource_scope: Mapping[str, str] | None,
    ) -> bool: ...

    async def activate_for_task(
        self,
        *,
        principal: Principal,
        task_id: uuid.UUID,
        capability: str,
        resource_scope: Mapping[str, str] | None,
        expires_at: datetime,
    ) -> None: ...

    async def deactivate_task(self, *, principal: Principal, task_id: uuid.UUID) -> int: ...

    async def invalidate_confirmations(self, *, principal: Principal, task_id: uuid.UUID) -> int:
        """Spend every still-unused confirmation token bound to this task, so no
        token outlives the task it was issued for (18 §5.3 step 3)."""
        ...

    async def principal_active(self, principal: Principal) -> bool: ...

    async def record(
        self,
        event: AgentEvent,
        *,
        principal: Principal,
        graph_id: uuid.UUID | None,
        resource: str,
        result: AuditResult,
        decision: PermissionDecisionValue | None = None,
    ) -> None: ...


# ── usage / rate / budget (13) ─────────────────────────────────────────────


class UsageLimitReached(Exception):
    """A deterministic limit refused a call. `limit` names which (13 §5)."""

    def __init__(self, limit: str, *, retry_after_seconds: int = 60) -> None:
        super().__init__(f"limit reached: {limit}")
        self.limit = limit
        self.retry_after_seconds = retry_after_seconds

    @property
    def is_budget(self) -> bool:
        return "budget" in self.limit


class UsagePort(Protocol):
    async def precheck(self, *, principal: Principal, projected_cost: float) -> None: ...

    async def record(
        self,
        *,
        principal: Principal,
        graph_id: uuid.UUID | None,
        kind: UsageKind,
        units: int,
        estimated_cost: float,
        provider: str | None = None,
        model: str | None = None,
        tool_id: str | None = None,
    ) -> None: ...


# ── tools (07) ─────────────────────────────────────────────────────────────


class ToolCatalog(Protocol):
    def resolve(self, tool_id: str) -> ToolHandle | None: ...

    def enabled_handles(self) -> list[ToolHandle]: ...

    async def run(
        self,
        tool_id: str,
        platform: ExecutionPlatform,
        invocation: ToolInvocation,
        *,
        timeout: float,
    ) -> ToolOutput: ...

    def release_task(self, task_id: uuid.UUID) -> None:
        """Release everything the task's tool calls allocated (09 §7: task
        temp is cleaned up on task end). Called once, at every terminal state."""
        ...


# ── models (06) ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedModels:
    primary: ModelProvider
    fallback: ModelProvider | None = None
    # MP-T4: the tools this principal's resolved AgentConfiguration enables.
    # `None` means "every server-enabled tool".
    allowed_tool_ids: frozenset[str] | None = None


class ModelResolverPort(Protocol):
    async def resolve(
        self, *, principal: Principal, graph_id: uuid.UUID | None
    ) -> ResolvedModels: ...


# ── memory (11) ────────────────────────────────────────────────────────────


@dataclass
class Hydration:
    items: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class HydratorPort(Protocol):
    async def hydrate(
        self, *, principal: Principal, graph_id: uuid.UUID | None, query: str
    ) -> Hydration: ...


# ── the per-request bundle ─────────────────────────────────────────────────


# ── supervisor (18 §5.4) ───────────────────────────────────────────────────


class SupervisorGatePort(Protocol):
    """The runtime's **read-only** view of the global emergency latch.

    There is deliberately no method here that sets or clears the latch: that is
    the superuser control path's alone (`server/composition/supervisor.py`),
    which the runtime can neither import nor reach. The runtime can only ask.
    """

    async def submissions_open(self) -> bool:
        """False while latched — and False whenever the latch cannot be read
        (fail closed)."""
        ...

    def latched_now(self) -> bool:
        """The in-process latch, synchronously — for the check that must not
        yield to the event loop (see `AgentRuntime.submit`)."""
        ...


@dataclass
class TaskEnvironment:
    """Everything request-scoped the runtime uses for one API call."""

    session: AsyncSession
    security: SecurityPort
    usage: UsagePort
    models: ModelResolverPort
    hydrator: HydratorPort
    supervisor: SupervisorGatePort


__all__: Sequence[str] = [
    "ActionRequest",
    "ActivationRequest",
    "CapabilityInfo",
    "CapabilityStatus",
    "Hydration",
    "HydratorPort",
    "IssuedConfirmation",
    "ModelResolverPort",
    "ResolvedModels",
    "SecurityPort",
    "SupervisorGatePort",
    "TaskEnvironment",
    "ToolCatalog",
    "UsageLimitReached",
    "UsagePort",
    "Verdict",
]
