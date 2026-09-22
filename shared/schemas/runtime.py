"""Agent-runtime vocabulary — 05_AGENT_RUNTIME.md / 06_MODEL_PROVIDER_LLM_TOOL.md.

Source: `05` (the propose→authorize→execute loop, bounds, task lifecycle) and
`06` (the normalized `ModelProvider` invoke contract). Neither document is a
`01` §1.2 data-model entity — `05 §3` is explicit that task state is
"session-scoped... no cross-task memory lives in the runtime" — so nothing
here is a persisted entity either; these are the runtime's own in-process
contracts, following the same "engine vocabulary lives in shared/schemas, not
inside the package whose imports are restricted" pattern
`shared/schemas/authorization.py` already establishes for the authorization
engine.

**Why this lives in `shared/schemas` and not `server/agent`:** the runtime
orchestrator (`server/agent`) is mechanically forbidden from importing
`server.graph`, `server.capabilities`, `server.secrets`, `server.storage`, or
`server.gateway` (pyproject's contracts) — every capability it needs arrives
as a constructor-injected port (`server/agent/ports.py`). Those ports are
implemented by modules on the *other* side of that boundary
(`server/gateway/runtime.py`, `server/models`, `server/tools`,
`server/modeltools`, `server/memory`), which therefore need the same request/
response shapes agent uses, without importing `server.agent` itself (siblings
under the layering contract cannot import each other either). Shared schemas
are the one layer everything may import (16 §1), exactly as already used for
`Principal`/`AccessRequest`/`ActionBinding`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Mapping
from uuid import UUID

from pydantic import Field, model_validator

from shared.schemas.common import ORMBase, utcnow
from shared.schemas.enums import ModelProvider as ModelProviderName

# ── task lifecycle (05 §9) ───────────────────────────────────────────────────


class TaskStatus(str, Enum):
    """05 §9's task lifecycle states.

    Not a `01` §1.2 registry value: `05 §3` is explicit that task state is
    runtime-owned and session-scoped, never a persisted first-class entity, so
    there is no data-model column this enum has to match.
    """

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FailureReason(str, Enum):
    """Why a task ended `FAILED` — 05 §6/§12's named failure modes, kept as a
    closed enum so a router can map each one to the right HTTP status
    (02 §1.7) without string-matching a free-form message."""

    RUNAWAY_ITERATIONS = "runaway_iterations"
    RUNAWAY_TOOL_CALLS = "runaway_tool_calls"
    RUNAWAY_MODEL_CALLS = "runaway_model_calls"
    RUNAWAY_NESTING = "runaway_nesting"
    TIMEOUT = "timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    MODEL_UNAVAILABLE = "model_unavailable"
    MALFORMED_PROPOSAL = "malformed_proposal"
    CONTEXT_ASSEMBLY_FAILED = "context_assembly_failed"
    CANCELLED = "cancelled"


# ── the model-invocation contract (06 §1) ────────────────────────────────────


class ModelMessage(ORMBase):
    """One turn of the normalized `messages` `06 §1` speaks of."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    # Set only on a `role="tool"` message — which tool/model-tool produced
    # this observation, so a provider adapter that supports native tool-role
    # messages can attribute it; ignored by adapters that flatten to text.
    name: str | None = None


class GenerationPolicy(ORMBase):
    """`ModelConfiguration.generation_policy` (`01` §9.1), typed at the call
    boundary instead of passed through as a bare `dict`."""

    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stop: list[str] | None = None


class ModelResult(ORMBase):
    """06 §1's normalized `ModelResult` — every adapter returns this shape;
    provider-specific response fields never leak past `server/models`."""

    content: str
    tokens_used: int = Field(ge=0, default=0)
    finish_reason: str = "stop"


class ModelUnavailable(Exception):
    """06 §4 / 05 §5 / FAIL-CORE-002: a model call could not be completed.

    The runtime's response to this is always an explicit failure — never a
    fabricated answer, and (05 §5) a *deterministic* fallback decision if a
    fallback model is configured, never the model deciding to fall back.
    """


class UnsupportedModelProvider(ModelUnavailable):
    """06 §1 (MODEL-002): "adding one is an adapter + config, not a runtime
    change" — but until an adapter exists, selecting that provider is an
    explicit, honest failure rather than a silent no-op or a fabricated call.
    """


# ── tool / model-tool invocation (07 §8, 06 §3) ──────────────────────────────


class ToolInvocationRequest(ORMBase):
    """What the runtime hands a dispatcher after `04` has already authorized
    the call — a dispatcher never re-decides whether this was permitted, it
    only executes (07 §8's `EXEC` node)."""

    task_id: str
    tool_id: str
    operation: str
    arguments: Mapping[str, Any] = Field(default_factory=dict)
    # Carried through for a dispatcher that needs it (e.g. a model-tool
    # executor resolving *which* configured model), never re-derived from
    # anything model-supplied — the runtime sets this from the already-
    # authorized capability/resource_scope, not from the proposal directly.
    resource_scope: Mapping[str, str] | None = None


class ToolResult(ORMBase):
    """07 §8's `OBS` node input — an observation, not a security decision.

    `success=False` with `error` set is the *normal* shape for a tool failure
    (05 §6: "tool failure -> result-as-observation"); it is not itself a
    reason to fail the task, the model may replan within bounds.
    """

    tool_id: str
    success: bool
    # Bounded/truncated before this is constructed (05 §6 "oversized tool
    # output -> truncate + note") — never the raw unbounded payload.
    output: str | None = None
    error: str | None = None
    tokens_or_units: int = Field(ge=0, default=0)
    estimated_cost: float = Field(ge=0.0, default=0.0)


class ToolExecutionFailed(Exception):
    """A dispatcher could not even attempt the call (unknown tool id, disabled
    configuration, provider outage) — distinct from a `ToolResult(success=False)`,
    which is a completed-but-unsuccessful execution the model can observe and
    replan around. This is closer to `05 §6`'s "dependency down" row."""


# ── the model's proposal (05 §1/§2 — deterministic parse target) ────────────


class ProposalKind(str, Enum):
    """05 §1's `KIND` branch. Exhaustive: a proposal that parses to none of
    these is, by construction, a malformed proposal (05 §6)."""

    FINAL_ANSWER = "final_answer"
    TOOL_CALL = "tool_call"
    MODEL_TOOL_CALL = "model_tool_call"


class AgentProposal(ORMBase):
    """The one shape a model's raw output is deterministically parsed into
    (05 §2 `PARSE`) before anything downstream ever looks at it again.

    `[LOCKED]` by construction, not by convention: this type has no
    `user_id`/`device_id`/`session_id`/`role`/`graph_id`-as-grant field, and no
    field through which the model could assert a capability is already
    granted. `graph_id` is present because 04 §0 treats a request body's
    `graph_id` as *"a claim to check, never a grant"* — the runtime passes it
    into the `AccessRequest` it builds itself, and `04` re-derives membership
    from the database on every request regardless of what this field says.
    Identity is never read from here; it comes from the authenticated
    `Principal` the runtime already holds (05 §8 confused-deputy prevention).
    """

    kind: ProposalKind

    # kind == FINAL_ANSWER
    final_text: str | None = None

    # kind == TOOL_CALL
    tool_id: str | None = None
    capability: str | None = None
    operation: str | None = None

    # kind == MODEL_TOOL_CALL
    model_tool_id: str | None = None
    prompt: str | None = None

    # shared by both call kinds
    arguments: Mapping[str, Any] | None = None
    resource_scope: Mapping[str, str] | None = None
    # A claim to check (04 §0), never a grant — see class docstring.
    graph_id: UUID | None = None

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> "AgentProposal":
        if self.kind is ProposalKind.FINAL_ANSWER:
            if not self.final_text:
                raise ValueError("final_answer proposal requires final_text")
        elif self.kind is ProposalKind.TOOL_CALL:
            if not self.tool_id or not self.capability or not self.operation:
                raise ValueError(
                    "tool_call proposal requires tool_id, capability, and operation"
                )
        elif self.kind is ProposalKind.MODEL_TOOL_CALL:
            if not self.model_tool_id or not self.prompt:
                raise ValueError("model_tool_call proposal requires model_tool_id and prompt")
        return self


class ProposalParseError(Exception):
    """05 §6: an unparseable model proposal — bounded-retry, then fail."""


# ── runtime events (05 §13 / this branch's observability requirement) ───────


class RuntimeEventKind(str, Enum):
    """The structured event stream this branch's instructions ask for.
    Distinct from `server.security.events.AuditAction` (the persisted,
    security-audited registry) — the gateway-side event recorder maps a
    curated subset of these onto real `AuditEvent` rows; the rest are
    observability-only and never touch the database (server.agent cannot
    reach server.storage at all, see pyproject's boundary contract)."""

    TASK_CREATED = "task_created"
    MODEL_PROPOSAL = "model_proposal"
    AUTHORIZATION_REQUESTED = "authorization_requested"
    AUTHORIZATION_DECIDED = "authorization_decided"
    CONFIRMATION_REQUESTED = "confirmation_requested"
    CONFIRMATION_ACCEPTED = "confirmation_accepted"
    CONFIRMATION_REJECTED = "confirmation_rejected"
    TOOL_INVOCATION = "tool_invocation"
    TOOL_RESULT = "tool_result"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"


@dataclass(frozen=True)
class AgentEvent:
    """One entry in the runtime's event stream.

    `data` is a small, bounded mapping of already-safe fields — never a raw
    tool argument blob, a model message, or anything from `shared.schemas`'s
    "do not expose" list (SecretStore contents, raw auth credentials, another
    user's data). Callers populate it only with fields explicitly meant for
    observability (a tool_id, a risk tier, a byte count) — see call sites.
    """

    kind: RuntimeEventKind
    task_id: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    data: Mapping[str, Any] = field(default_factory=dict)


# ── memory hydration (11 §3, consumed by context assembly) ──────────────────


class MemoryItem(ORMBase):
    """One already-visibility-filtered piece of context 11's hydration would
    return (11 §2's `mem0_readable` predicate is applied by the memory
    subsystem, never re-derived here or in `server/agent` — see
    `server/memory/hydrator.py`)."""

    content: str
    fact_type: str | None = None
    source: Literal["memory", "vault"] = "memory"


# ── bounds (05 §3 — RATE-001, a runaway agent must be impossible) ───────────


class RuntimeBounds(ORMBase):
    """05 §3's ceilings. `[LOCKED]` that every one of these exists and is
    enforced by the runtime; the numeric defaults are `[IMPL]` (OD-02),
    documented in `server/config/schema.py`."""

    max_iterations: int = Field(gt=0, default=12)
    max_tool_calls: int = Field(gt=0, default=8)
    max_model_calls: int = Field(gt=0, default=12)
    max_model_tool_nesting_depth: int = Field(ge=0, default=1)
    max_parse_retries: int = Field(ge=0, default=2)
    wall_clock_timeout_seconds: float = Field(gt=0, default=120.0)
    model_call_timeout_seconds: float = Field(gt=0, default=30.0)
    max_cost: float = Field(ge=0.0, default=0.0)  # 0.0 == no paid-provider budget


# ── the API-facing request/result (02 §5) ────────────────────────────────────


class AgentRequest(ORMBase):
    """`POST /api/v1/agent/tasks` body (02 §5)."""

    input: str = Field(min_length=1)


class AgentResult(ORMBase):
    """`AgentResult` (02 §5) — what every agent-task endpoint returns.

    `confirmation_token` is present iff `status == AWAITING_CONFIRMATION`; the
    client's `/confirm` call echoes it back (05 §4).
    """

    task_id: str
    status: TaskStatus
    output: str | None = None
    iterations_used: int = 0
    tool_calls_used: int = 0
    model_calls_used: int = 0
    failure_reason: FailureReason | None = None
    confirmation_token: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class ConfirmRequest(ORMBase):
    """`POST /api/v1/agent/tasks/{task_id}/confirm` body (02 §5)."""

    confirmation_token: str
    approve: bool


__all__ = [
    "AgentEvent",
    "AgentProposal",
    "AgentRequest",
    "AgentResult",
    "ConfirmRequest",
    "FailureReason",
    "GenerationPolicy",
    "MemoryItem",
    "ModelMessage",
    "ModelProviderName",
    "ModelResult",
    "ModelUnavailable",
    "ProposalKind",
    "ProposalParseError",
    "RuntimeBounds",
    "RuntimeEventKind",
    "TaskStatus",
    "ToolExecutionFailed",
    "ToolInvocationRequest",
    "ToolResult",
    "UnsupportedModelProvider",
]
