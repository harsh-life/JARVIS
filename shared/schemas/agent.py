"""Agent-runtime vocabulary — task results, tool invocation, execution platforms.

Source: `02` §5 (the agent endpoints and their `AgentResult`), `05` (the loop),
`07` §1–§3 (tool → operation → primitive), and the owner's platform-neutral
capability model (`docs/CAPABILITY_MATRIX.md` §1).

Lives in `shared/schemas/` because three modules that may not import each other
all speak it: `server/agent` (the runtime), `server/tools` and
`server/modeltools` (the executable side), and `server/gateway` (the HTTP
surface). None of it is a `01` §1.2 registry value except where noted.

What is deliberately absent from every type here: a risk tier the model could
set, a "confirmed" flag, a principal or graph id the model could choose. Those
are decided by deterministic code from server-derived identity (P1, PHONE-003).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from shared.schemas.enums import RiskCategory, UsageKind


class ExecutionPlatform(str, Enum):
    """Where an adapter executes. `[PROPOSED]` engine vocabulary (not a `01`
    §1.2 registry value): a semantic capability can map to a different adapter
    on each platform, and a platform with no adapter rejects the operation
    rather than pretending to support it."""

    SERVER = "server"
    LINUX = "linux"
    ANDROID = "android"


class TaskMode(str, Enum):
    """What kind of task it is (18 §3, OD-F1). Set once, at submission, by the
    caller — never by the worker: no proposal can carry it (proposals are
    `extra="forbid"`), and nothing after creation changes it.

    * `execute` — the user's explicit instruction; the tier table governs as
      is (low-risk operations chain automatically; consequential and
      high-impact ones still need confirmation, and step-up).
    * `draft` / `suggest` / `observe` — nothing is executed: only `low_read`
      operations run, everything else is refused outright (never offered for
      confirmation). Turning a draft or suggestion into action takes a new
      `execute` task created by the user.
    """

    EXECUTE = "execute"
    DRAFT = "draft"
    SUGGEST = "suggest"
    OBSERVE = "observe"


class AgentTaskStatus(str, Enum):
    """Lifecycle of one agent task (`02` §5). `[PROPOSED]` — `01` has no task
    entity; this is the runtime branch's addition, recorded in
    `docs/DECISION_REGISTER.md` §2."""

    RUNNING = "running"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset(
    {AgentTaskStatus.COMPLETED, AgentTaskStatus.FAILED, AgentTaskStatus.CANCELLED}
)


class AgentFailureCode(str, Enum):
    """Why a task stopped without finishing (FAIL-CORE-001: always explicit).

    Every bound in `05` §3 has its own code, so a runaway stop is never
    reported as something vaguer than the ceiling it hit.
    """

    MAX_ITERATIONS = "max_iterations_exceeded"
    MAX_MODEL_CALLS = "max_model_calls_exceeded"
    MAX_TOOL_CALLS = "max_tool_calls_exceeded"
    TIMEOUT = "wall_clock_timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    RATE_LIMITED = "rate_limited"
    MODEL_UNAVAILABLE = "model_unavailable"
    UNPARSEABLE_PROPOSAL = "unparseable_proposal"
    CONFIRMATION_EXPIRED = "confirmation_expired"
    CONFIRMATION_STATE_LOST = "confirmation_state_lost"
    PRINCIPAL_REVOKED = "principal_revoked"
    # 18 §5: the circuit breaker stopped the task. Terminal; never resumed.
    EMERGENCY_STOP = "emergency_stop"
    # 18 §4.1: no progress (or the same operation over and over) and no other
    # worker to switch to.
    STALLED = "stalled"
    # 18 §4.2/§4.4: recovery needed a switch, and the worker chain (or
    # `max_worker_switches`) was exhausted.
    WORKER_CHAIN_EXHAUSTED = "worker_chain_exhausted"
    INTERNAL_ERROR = "internal_error"


# ── tool invocation (07 §3/§5) ─────────────────────────────────────────────


@dataclass(frozen=True)
class ToolInvocation:
    """What an adapter receives. Never a secret, never a raw principal token.

    `resource_scope` is the narrowing the user's grant bound this task to (e.g.
    one app, one sandbox root) — the adapter must stay inside it, and the engine
    has already checked the grant against it. `user_id`/`task_id` let an adapter
    scope its own work (a per-user sandbox root) without being handed any
    credential.
    """

    tool_id: str
    operation: str
    arguments: Mapping[str, Any]
    user_id: UUID
    task_id: UUID
    platform: ExecutionPlatform
    resource_ref: str | None = None
    resource_scope: Mapping[str, str] | None = None
    # The device of the authenticated principal the task runs as — server-
    # derived (03 §8), never from a proposal. A device-platform adapter must
    # target exactly this device: capability grants can be device-scoped (the
    # per-app grid is per phone, PRD §13), so dispatching to "some device of
    # this user" would execute where the user never granted anything.
    device_id: UUID | None = None


@dataclass(frozen=True)
class ToolOutput:
    """An adapter's result — returned to the model as **data**, never as
    instructions (`00` §24).

    `usage_kind`/`units`/`estimated_cost` feed the one `UsageEvent` every tool
    execution emits (USAGE-001). A model-tool reports `model_call` with its
    provider and model, per `02` §6.
    """

    ok: bool
    content: str = ""
    error: str | None = None
    usage_kind: UsageKind = UsageKind.TOOL_CALL
    units: int = 1
    estimated_cost: float = 0.0
    provider: str | None = None
    model: str | None = None


# ── API shapes (02 §5/§6) ──────────────────────────────────────────────────


class TaskCounters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    iterations: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    worker_switches: int = 0


class PendingAction(BaseModel):
    """What the human is being asked to approve — the explicit operation
    details OD-F1 requires, so an approval is informed rather than blind."""

    model_config = ConfigDict(extra="forbid")

    kind: str  # "tool_operation" | "capability_activation"
    capability: str
    risk_category: RiskCategory
    tool_id: str | None = None
    operation: str | None = None
    resource_ref: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    resource_scope: dict[str, str] | None = None
    requires_step_up: bool = False
    confirmation_token: str
    expires_at: datetime


class AgentFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: AgentFailureCode
    message: str


class AgentResult(BaseModel):
    """`02` §5's `AgentResult` — a proposal-executed-under-authorization result,
    never raw model execution authority (P1)."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    status: AgentTaskStatus
    mode: TaskMode = TaskMode.EXECUTE
    response: str | None = None
    # 18 §4.1: the worker said it could not resolve the request. An honest
    # answer, reported as such — never presented as completed work.
    unresolved: bool = False
    failure: AgentFailure | None = None
    pending: PendingAction | None = None
    active_capabilities: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    counters: TaskCounters = Field(default_factory=TaskCounters)
    # 20 §2.4 "observable": a live break-glass record exists for this task —
    # its next matching `system.restricted` run would be unconfined.
    break_glass_active: bool = False


class ToolOperationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str
    risk_category: RiskCategory


class ToolSummary(BaseModel):
    """`02` §6 `GET /config/tools` — a ToolContract summary."""

    model_config = ConfigDict(extra="forbid")

    tool_id: str
    description: str
    required_capability: str
    platforms: list[ExecutionPlatform]
    operations: list[ToolOperationSummary]


@dataclass(frozen=True)
class OperationSpec:
    """How one enumerated tool operation is presented to the authorization
    engine (`04` §1): which resource type it touches and which of the six
    resource operations it is.

    `requires_resource_ref` is true for anything but `CREATE`: an operation on
    an existing resource must name it, so the engine can load it and apply
    ownership and visibility (D3/D4). There is no way to express "any resource".
    """

    resource_type: str  # a shared.schemas.authorization.ResourceType value
    resource_operation: str  # a shared.schemas.authorization.Operation value
    requires_resource_ref: bool = False


@dataclass(frozen=True)
class ToolHandle:
    """The runtime's view of a registered, enabled tool."""

    tool_id: str
    description: str
    required_capability: str
    operations: Mapping[str, OperationSpec]
    platforms: frozenset[ExecutionPlatform]
    timeout_seconds: float
    is_model_tool: bool = False
    operation_tiers: Mapping[str, RiskCategory] = field(default_factory=dict)
    # The contract's own narrowing (e.g. a model-tool's `model_tool_id`). Merged
    # over the task's activated scope when the engine checks the grant, so a
    # grant narrowed to one model-tool cannot be spent on another.
    natural_scope: Mapping[str, str] | None = None
    # Upper-bound cost of one call, for the budget precheck (13 §3). Non-zero
    # only for paid model-tools.
    projected_cost_per_call: float = 0.0
