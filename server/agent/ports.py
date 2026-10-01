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
* no secret resolution of any kind (SECRET-002, INV-6);
* no way to create, widen or extend a break-glass record (20 §2.2) — only to
  settle and end the task's own.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Protocol, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from server.agent.events import AgentEvent
from server.models.provider import ChatMessage, ModelProvider
from shared.schemas.agent import (
    AgentFailureCode,
    AgentTaskStatus,
    ExecutionPlatform,
    ToolHandle,
    ToolInvocation,
    ToolOutput,
)
from shared.schemas.authorization import ActionBinding, Operation, Principal, ResourceType
from shared.schemas.enums import AuditResult, PermissionDecisionValue, RiskCategory, UsageKind

if TYPE_CHECKING:
    from server.agent.agent_run import AgentRunPort


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

    async def settle_break_glass(
        self,
        *,
        principal: Principal,
        graph_id: uuid.UUID | None,
        task_id: uuid.UUID,
        ended: AgentTaskStatus | None = None,
        failure: AgentFailureCode | None = None,
    ) -> None:
        """Write the audit rows owed for this task's break-glass record (20
        §2.4) — its invocations, and its end once exhausted or expired. With
        `ended`, the task has reached that terminal state: any live record
        ends with it first. The runtime can neither see nor create a record;
        it can only settle and end one."""
        ...

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


# 18 §8 — the worker slot. A worker is whatever *proposes the next step*:
# `invoke` (compacted transcript in, raw proposal text out), `health`, and a
# `spec` for bounds and metering. Today every worker is an LLM behind the
# `ModelProvider` interface (06), so the slot *is* that interface rather than a
# second, parallel one. A future reasoning engine plugs in as another
# implementation, injected by the composition root, and gets no other port: no
# authorization handle, no tool handle, no SecretStore, no registry.
Worker = ModelProvider


@dataclass(frozen=True)
class ResolvedModels:
    """The task's ordered worker chain (18 §4.2): its resolved primary (user →
    graph → server default, OD-RT-3) first, then operator-configured fallbacks.
    Selection is configuration alone — no worker, and no evaluator, picks the
    next one."""

    chain: tuple[Worker, ...]
    # MP-T4: the tools this principal's resolved AgentConfiguration enables.
    # `None` means "every server-enabled tool".
    allowed_tool_ids: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if not self.chain:
            raise ValueError("a worker chain has at least one worker")

    @property
    def primary(self) -> Worker:
        return self.chain[0]


class ModelResolverPort(Protocol):
    async def resolve(
        self, *, principal: Principal, graph_id: uuid.UUID | None, agent_model_ref: str | None = None
    ) -> ResolvedModels:
        """`agent_model_ref` (docs/29 §7.4, only for an agent run): the
        configured model entry its selected profile references. The resolver
        builds that entry's adapter through the same key path as any worker;
        the run never names a provider, an endpoint or a key itself."""
        ...


# ── memory (11) ────────────────────────────────────────────────────────────


@dataclass
class Hydration:
    items: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Relevant Knowledge Vault chunks (11 §3, docs/21 §5) — shared, curated,
    # and still untrusted data when they reach the model (PRD §24).
    knowledge: list[str] = field(default_factory=list)


class HydratorPort(Protocol):
    async def hydrate(
        self, *, principal: Principal, graph_id: uuid.UUID | None, query: str
    ) -> Hydration: ...


class MemoryFormationPort(Protocol):
    """docs/21 §2.2 option (a) / §3 — runtime-owned memory formation.

    The runtime makes the one model call (bounded, budget-checked and metered
    like every other, MP-T4); the port only builds the prompt from the two
    permitted inputs and gates and stores what comes back. The runtime cannot
    reach the memory store, the write gate, or any other memory operation through
    this port — only these two steps for its own finished task.
    """

    def plan(
        self, *, principal: Principal, graph_id: uuid.UUID | None, user_request: str, final_answer: str
    ) -> Sequence[ChatMessage] | None:
        """The extraction prompt, or `None` when formation is off for this task
        (disabled, no memory store, no graph context)."""
        ...

    async def commit(
        self, *, principal: Principal, graph_id: uuid.UUID | None, task_id: uuid.UUID, model_output: str
    ) -> list[str]:
        """Gate and store the proposed facts; returns user-facing notes. Never
        raises for a refused fact."""
        ...


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


# ── observation (19 §4, §6) ────────────────────────────────────────────────


@dataclass(frozen=True)
class TraceEvent:
    """One fact the runtime produced for a task, mirrored in the task's volatile
    state: the same events it audits (with the tier and decision of each
    authorization, and the units and cost of each metered call). `position` is
    how many transcript messages preceded it."""

    position: int
    at: float
    name: str
    result: str | None = None
    resource: str | None = None
    decision: str | None = None
    risk: str | None = None
    units: int | None = None
    cost: float | None = None


@dataclass(frozen=True)
class TaskSnapshot:
    """A read-only copy of one task, for an observer. Only what the worker
    produced (its proposals) and what the runtime fed back (observations),
    plus the task's own counters and events — never the system prompt, the
    hydrated memory or vault context, a confirmation token, a grant, or any
    handle on the task itself. Nothing in it can be used to act."""

    task_id: uuid.UUID
    user_id: uuid.UUID
    graph_id: uuid.UUID | None
    mode: str
    user_input: str
    status: str
    failure_code: str | None
    final_response: str | None
    unresolved: bool
    iterations: int
    model_calls: int
    tool_calls: int
    worker_switches: int
    denials: int
    violations: int
    rejections: int
    tripped_source: str | None
    elapsed_seconds: float
    transcript: tuple[tuple[str, str], ...]
    events: tuple[TraceEvent, ...]


class TaskObserver(Protocol):
    """Something outside the runtime that watches tasks — the Judge (19),
    wired by the composition root. The runtime never imports it (pyproject:
    "The runtime never depends on the Judge").

    Both methods are **synchronous and must only enqueue**: the runtime calls
    them inline, ignores anything they raise, and never waits for what they
    start (19 §6 — "the task does not wait for it"). There is no return value
    the runtime reads, so an observer cannot steer a task. Its one lawful way
    to affect a running task is the breaker's `trip()`, which it receives
    separately from the composition root, never from here."""

    def step_completed(self, task_id: uuid.UUID, tool_calls: int, snapshot: Callable[[], TaskSnapshot]) -> None:
        ...

    def task_ended(self, snapshot: TaskSnapshot) -> None:
        ...


# ── tuning (19 §9: human-approved, versioned configuration) ────────────────


@dataclass(frozen=True)
class WorkerTuning:
    """Worker-facing values a **superuser** approved (19 §9), read at the start
    of each task segment. Guidance for the worker and bounds for recovery —
    never authority: the text is shown to the worker, whose every proposal is
    still parsed and authorized from scratch, and a recovery value can only be
    one the registry's ranges allow. `None` everywhere is the base config."""

    system_prompt: str | None = None
    tool_descriptions: Mapping[str, str] = field(default_factory=dict)
    stall_window: int | None = None
    loop_repeat_limit: int | None = None
    max_worker_switches: int | None = None


class TuningPort(Protocol):
    async def current(self) -> WorkerTuning: ...


@dataclass
class TaskEnvironment:
    """Everything request-scoped the runtime uses for one API call."""

    session: AsyncSession
    security: SecurityPort
    usage: UsagePort
    models: ModelResolverPort
    hydrator: HydratorPort
    supervisor: SupervisorGatePort
    memory: MemoryFormationPort | None = None
    tuning: TuningPort | None = None
    # H-1: ends the request's store transaction before the runtime starts a long
    # wait — a model call, a tool run, a memory search or write — so no
    # transaction (and no lock) is held across one. What the task has written
    # so far becomes durable; the next write opens a new transaction. `None`
    # (a test environment) keeps the one-transaction behaviour.
    release_store: Callable[[], Awaitable[None]] | None = None
    # docs/29 Phase 2: what an agent run needs from the Agent Factory (per-step
    # re-validation, attribution, inbox, notebook, model routing). `None`
    # unless `agents.enabled`; an agent run without it fails closed.
    agent_runs: "AgentRunPort | None" = None


__all__: Sequence[str] = [
    "ActionRequest",
    "ActivationRequest",
    "CapabilityInfo",
    "CapabilityStatus",
    "Hydration",
    "HydratorPort",
    "IssuedConfirmation",
    "MemoryFormationPort",
    "ModelResolverPort",
    "ResolvedModels",
    "SecurityPort",
    "SupervisorGatePort",
    "TaskEnvironment",
    "TaskObserver",
    "TaskSnapshot",
    "TraceEvent",
    "TuningPort",
    "WorkerTuning",
    "ToolCatalog",
    "UsageLimitReached",
    "UsagePort",
    "Verdict",
    "Worker",
]
