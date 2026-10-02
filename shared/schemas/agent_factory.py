"""The Agent Factory's shapes — docs/29 §4–§9, §11–§14, §23.

`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]` (docs/29, OD-AF-1). Everything in
this module is an *implemented recommendation* behind `agents.enabled: false`,
pending the owner's signature (docs/DECISION_REGISTER.md §2J); nothing here is
canonical because it has been implemented.

The one rule these shapes exist to make structural (docs/29 §1.1):

> **NO MODEL OR AGENT RUNTIME CAN CREATE AUTHORITY.**

So there are two kinds of shape here, and they never mix:

* **What a worker may write** — `AgentDraft` (and the future
  `ModelCallRequest`). Strict (`extra="forbid"`), frozen, bounded, closed
  vocabularies. Semantic intent only: a name, a goal, tags, *abilities* (a
  closed list of words the compiler maps through a code-reviewed table), data
  sources, a trigger request and preferences that can only reorder or lower.
  There is no field for an owner, a graph, a capability, a tier, a
  confirmation, a budget ceiling, a secret, a device, a runtime, an endpoint, a
  model reference, a mode, a version or a hash — so a draft naming any of them
  is malformed as a whole (AGENT-T2), never "partly applied".
* **What only deterministic server code writes** — templates (repository
  YAML), model/runtime profiles (operator config and code), the selection, and
  the `CompiledAgentSpec` (the compiler is its only writer, docs/29 §9.1). A
  spec holds a *ceiling*; it is never read as a grant (docs/29 §10.1).

Stdlib `zoneinfo`/`urllib` are used for validation only; this module imports
nothing from `server` (REPO-T3).
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.schemas.agent import AgentResult, TaskMode
from shared.schemas.enums import RiskCategory

# ── closed vocabularies (docs/29 §5.4, §6.2, §7.2, §9.3, §14) ──────────────
#
# Each is closed: adding a value is a code-reviewed enum change, never a
# runtime action, and a value outside the enum makes the containing shape
# invalid.


class TaskTag(str, Enum):
    """docs/29 §5.4 (v1). What a worker matches a request against."""

    RESEARCH = "research"
    MONITORING = "monitoring"
    SUMMARIZATION = "summarization"
    CLASSIFICATION = "classification"
    EXTRACTION = "extraction"
    DOCUMENT_PROCESSING = "document_processing"
    REPORTING = "reporting"
    NOTIFICATION_PREP = "notification_prep"
    KNOWLEDGE_MAINTENANCE = "knowledge_maintenance"
    BROWSER_AUTOMATION = "browser_automation"
    CODING_MAINTENANCE = "coding_maintenance"
    DATA_ANALYSIS = "data_analysis"
    API_WORKFLOW = "api_workflow"
    SECURITY_RESEARCH = "security_research"


class AbilityName(str, Enum):
    """docs/29 §9.3 — the words a worker may use for what an agent needs. The
    compiler maps each through `server/agents/abilities.py`; an ability is
    never itself a capability or a grant."""

    READ_WEB_ALLOWLISTED = "read_web_allowlisted"
    READ_USER_MEMORY = "read_user_memory"
    READ_VAULT = "read_vault"
    READ_SANDBOX_FILES = "read_sandbox_files"
    WRITE_SANDBOX_FILES = "write_sandbox_files"
    READ_AGENT_NOTEBOOK = "read_agent_notebook"
    WRITE_AGENT_NOTEBOOK = "write_agent_notebook"
    CREATE_REMINDER = "create_reminder"
    INVOKE_MODEL_TOOL = "invoke_model_tool"


class ModelFeature(str, Enum):
    """docs/29 §6.2 — **routing metadata, never authorization**.

    The first seven are docs/29's list. The last four are this build's
    additive extension (`[PROPOSED]`, recorded in docs/DECISION_REGISTER.md
    §2J): operator-declared roles a model profile can serve, so "use a
    writing model for this" is one routing preference among many rather than
    a provider-specific path. Declaring a feature changes which profiles a
    selection *may order first*; it never makes a profile eligible that the
    owner's model policy or budget excludes, and it never widens an envelope.
    """

    AGENTIC_REASONING = "agentic_reasoning"
    TOOL_CALLING = "tool_calling"
    STRUCTURED_OUTPUT = "structured_output"
    VISION = "vision"
    BROWSER_SUITABLE = "browser_suitable"
    CODING_SUITABLE = "coding_suitable"
    LONG_CONTEXT = "long_context"
    # [PROPOSED] extension beyond docs/29 §6.2 — see the class docstring.
    WRITING = "writing"
    IMAGE_GENERATION = "image_generation"
    DOCUMENT_STRUCTURED = "document_structured"
    SPEECH = "speech"


class ModelPreference(str, Enum):
    """docs/29 §6.2: changes ordering only, never the eligible set."""

    FASTER = "faster"
    CHEAPER = "cheaper"
    THOROUGH = "thorough"


class TriggerKind(str, Enum):
    ON_DEMAND = "on_demand"
    REMINDER = "reminder"
    # docs/29 §15 (Phase 5; OD-AF-2 ratified 2026-10-02, register §2K): runs
    # only under an active StandingDelegation, and only when the operator has
    # switched `agents.unattended_enabled` on. Otherwise the compiler rejects
    # it (`unattended_unavailable`).
    UNATTENDED = "unattended"


class OutputKind(str, Enum):
    """docs/29 §19: the owner's inbox only in v1 (OD-AF-8)."""

    INBOX = "inbox"


class IsolationMode(str, Enum):
    """docs/29 §7.2, ordered weakest → strongest (`ISOLATION_ORDER`)."""

    IN_PROCESS = "in_process"
    WORKER_PROCESS = "worker_process"
    CONTAINER_NETNS = "container_netns"


ISOLATION_ORDER: dict[IsolationMode, int] = {
    IsolationMode.IN_PROCESS: 0,
    IsolationMode.WORKER_PROCESS: 1,
    IsolationMode.CONTAINER_NETNS: 2,
}


class InfraRequirement(str, Enum):
    NONE = "none"
    CONTAINER = "container"
    NETNS = "netns"
    MCP_SERVER = "mcp_server"
    BROWSER_SANDBOX = "browser_sandbox"


class AgentStatus(str, Enum):
    """docs/29 §14.1. `draft`/`compiled` are never persisted: they exist only
    as a `CompileOutcome` preview."""

    AWAITING_CONFIRMATION = "awaiting_confirmation"
    ACTIVE = "active"
    PAUSED = "paused"
    NEEDS_REAPPROVAL = "needs_reapproval"
    REVOKED = "revoked"
    DELETED = "deleted"


class AgentRunStatus(str, Enum):
    """docs/29 §14.2 — declared for the provider interface; runs are Phase 2."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SourceKind(str, Enum):
    URL = "url"
    MEMORY_TOPIC = "memory_topic"
    VAULT_DOMAIN = "vault_domain"
    SANDBOX_PATH = "sandbox_path"


class NotebookAccess(str, Enum):
    NONE = "none"
    READ = "read"
    READ_WRITE = "read_write"


class LatencyClass(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CostClass(str, Enum):
    LOCAL = "local"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RequiredApi(str, Enum):
    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"


class RuntimeType(str, Enum):
    NATIVE = "native"
    GENERAL_RUNTIME = "general_runtime"
    FRAMEWORK_HOSTED = "framework_hosted"
    SPECIALIZED_RUNTIME = "specialized_runtime"


class ToolInterface(str, Enum):
    IN_PROCESS_TOOL_CATALOG = "in_process_tool_catalog"
    TOOL_GATEWAY_MCP = "tool_gateway_mcp"
    TOOL_GATEWAY_HTTP = "tool_gateway_http"
    CONTAINED_WORKSPACE = "contained_workspace"


class LifecycleInterface(str, Enum):
    TASK_RUNTIME = "task_runtime"
    HTTP_API = "http_api"
    SDK_IN_WORKER_PROCESS = "sdk_in_worker_process"
    WS_PROTOCOL = "ws_protocol"


class PersistenceModel(str, Enum):
    VOLATILE_TASK = "volatile_task"
    RUNTIME_STORE = "runtime_store"
    WORKSPACE_FILES = "workspace_files"


class NetworkRequirement(str, Enum):
    NONE = "none"
    JARVIS_GATEWAY_ONLY = "jarvis_gateway_only"
    JARVIS_GATEWAY_PLUS_EGRESS_PROXY = "jarvis_gateway_plus_egress_proxy"


class Observability(str, Enum):
    FULL_TRACE = "full_trace"
    GATEWAY_TRACE_ONLY = "gateway_trace_only"


class CancellationMode(str, Enum):
    COOPERATIVE_EVENT = "cooperative_event"
    API_CANCEL = "api_cancel"
    CONTAINER_KILL = "container_kill"


# ── bounded text and identifiers ───────────────────────────────────────────

_SLUG = r"^[a-z][a-z0-9_]{2,40}$"
_PROFILE_SLUG = r"^[a-z][a-z0-9_\-]{2,40}$"
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")


def _printable(value: str) -> str:
    """No control characters (newlines and tabs included where a value is a
    single line). Unicode letters are fine; a NUL, a bell or an escape
    sequence is never part of a name."""

    if any(unicodedata.category(ch).startswith("C") for ch in value):
        raise ValueError("must not contain control characters")
    return value


def _text_block(value: str) -> str:
    """Free text that may span lines (purpose, outcome): control characters
    other than newline and tab are refused."""

    if any(unicodedata.category(ch).startswith("C") and ch not in "\n\t" for ch in value):
        raise ValueError("must not contain control characters")
    return value


Name = Annotated[str, Field(min_length=1, max_length=80), AfterValidator(_printable)]
Line = Annotated[str, Field(min_length=1, max_length=200), AfterValidator(_printable)]


def _iana(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"{value!r} is not an IANA time zone") from None
    return value


TimeZoneName = Annotated[str, Field(min_length=1, max_length=64), AfterValidator(_iana)]


class _Strict(BaseModel):
    """Every Agent Factory shape: unknown fields are an error, instances are
    immutable. A worker-written shape that could be edited after validation,
    or that quietly dropped a field it did not know, would not be strict."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ── what a worker may write ───────────────────────────────────────────────


class SourceRef(_Strict):
    """docs/29 §9.2: a data source the owner named, validated per kind. It is
    **data**: a URL here is never an egress change (a URL outside the
    operator's `EgressPolicy` becomes a clarification, §9.3), and a sandbox
    path is a *label*, never a filesystem path."""

    kind: SourceKind
    value: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _per_kind(self) -> "SourceRef":
        value = self.value
        if self.kind is SourceKind.URL:
            _printable(value)
            parts = urlsplit(value)
            if parts.scheme not in ("http", "https"):
                raise ValueError("a url source must be http(s)")
            if not parts.hostname:
                raise ValueError("a url source must name a host")
            if "@" in parts.netloc or parts.username is not None or parts.password is not None:
                # Credentials never travel inside a source (12 §2).
                raise ValueError("a url source must not carry credentials")
            if len(parts.hostname) > 253:
                raise ValueError("host name too long")
        elif self.kind is SourceKind.SANDBOX_PATH:
            if not _LABEL_RE.match(value):
                raise ValueError("a sandbox source is a label (letters, digits, '_', '-'), never a path")
        elif self.kind is SourceKind.VAULT_DOMAIN:
            if not _DOMAIN_RE.match(value):
                raise ValueError("a vault domain is a lowercase slug")
        else:
            _printable(value)
        return self


class TriggerRequest(_Strict):
    """docs/29 §9.2. `schedule_text` is the owner's words ("every morning");
    the compiler never guesses a schedule from it — without an exact `cron`
    a reminder is a clarification (§9.4)."""

    kind: TriggerKind
    schedule_text: Line | None = None
    cron: Annotated[str, Field(min_length=9, max_length=120), AfterValidator(_printable)] | None = None
    timezone: TimeZoneName | None = None


# docs/29 §9.2 "Forbidden in a draft", plus every other way this build knows to
# name authority, identity or a credential. Enforcement is `extra="forbid"` —
# this set exists so tests (and the rejection audit) can name the list; adding
# a *field* to AgentDraft with one of these names is a test failure.
FORBIDDEN_DRAFT_FIELDS: frozenset[str] = frozenset({
    "user_id", "owner_user_id", "graph_id", "agent_id",
    "capabilities", "grants", "tier", "risk", "confirmation", "requires_confirmation",
    "budget_ceiling", "secret_ref", "secrets", "delegation", "delegation_id",
    "endpoint", "device_id", "target_device",
    "runtime", "runtime_id", "network", "egress", "destinations",
    "model_ref", "mode", "version", "spec_hash",
    "risk_ceiling", "run_mode", "envelope", "envelope_ceiling", "scope", "resource_scope",
    "provider", "api_key", "base_url", "model_endpoint", "tool_endpoint", "run_token",
    "principal", "session_id", "system_restricted", "visibility", "status",
})


class AgentDraft(_Strict):
    """docs/29 §9.2 — everything the factory worker may propose.

    `model_preference` and `preferred_model_features` reorder eligible model
    profiles; `budget_preference_per_run` can only *lower* the per-run budget
    (docs/29 §9.2). None of them can widen anything.

    `preferred_model_features` is this build's additive extension
    (`[PROPOSED]`, §2J): the bounded form of "use a writing model for this".
    """

    name: Name
    purpose: Annotated[str, Field(min_length=1, max_length=2000), AfterValidator(_text_block)]
    desired_outcome: Annotated[str, Field(max_length=1000), AfterValidator(_text_block)] = ""
    task_tags: tuple[TaskTag, ...] = Field(min_length=1, max_length=5)
    template_hint: Annotated[str, Field(pattern=_SLUG)] | None = None
    requested_abilities: tuple[AbilityName, ...] = Field(default=(), max_length=10)
    sources: tuple[SourceRef, ...] = Field(default=(), max_length=20)
    trigger_request: TriggerRequest = Field(default_factory=lambda: TriggerRequest(kind=TriggerKind.ON_DEMAND))
    output_request: OutputKind = OutputKind.INBOX
    model_preference: ModelPreference | None = None
    preferred_model_features: tuple[ModelFeature, ...] = Field(default=(), max_length=4)
    budget_preference_per_run: float | None = Field(default=None, ge=0.0, le=1_000_000.0)
    notes_for_user: Annotated[str, Field(max_length=500), AfterValidator(_text_block)] = ""


# ── what only deterministic server code writes ────────────────────────────


class MemoryPolicy(_Strict):
    """docs/29 §16.2. `user_memory_write` has one value: agent output never
    becomes user memory (AGENT-T17)."""

    user_memory_read: bool = False
    vault_read: bool = False
    vault_domains: tuple[Annotated[str, Field(pattern=_DOMAIN_RE.pattern)], ...] = ()
    notebook: NotebookAccess = NotebookAccess.NONE
    user_memory_write: Literal["never"] = "never"


_SEVERITY = {
    RiskCategory.LOW_READ: 0,
    RiskCategory.LOW_WRITE: 1,
    RiskCategory.CONSEQUENTIAL: 2,
    RiskCategory.HIGH_IRREVERSIBLE: 3,
}


def risk_severity(tier: RiskCategory) -> int:
    return _SEVERITY[tier]


class AgentTemplate(_Strict):
    """docs/29 §5.2 — the operator-approved *maximum* shape of a class of
    agent. Authority-limiting, never authority-granting: it caps an envelope,
    and the owner's live grants still apply on top (§5.1)."""

    template_id: Annotated[str, Field(pattern=_SLUG)]
    version: int = Field(ge=1)
    description: Annotated[str, Field(min_length=1, max_length=400), AfterValidator(_text_block)]
    task_tags: tuple[TaskTag, ...] = Field(min_length=1)
    abilities: tuple[AbilityName, ...] = ()
    run_mode: TaskMode
    risk_ceiling: RiskCategory
    required_model_features: tuple[ModelFeature, ...] = ()
    preferred_runtime: Annotated[str, Field(pattern=_SLUG)]
    fallback_runtimes: tuple[Annotated[str, Field(pattern=_SLUG)], ...] = ()
    min_isolation: IsolationMode = IsolationMode.IN_PROCESS
    memory_policy: MemoryPolicy = Field(default_factory=MemoryPolicy)
    trigger_support: tuple[TriggerKind, ...] = Field(min_length=1)
    output_support: tuple[OutputKind, ...] = Field(min_length=1)
    max_run_seconds: int = Field(ge=10, le=3600)
    max_model_calls_per_run: int = Field(ge=1, le=64)
    max_tool_calls_per_run: int = Field(ge=0, le=128)
    default_budget_per_run: float = Field(ge=0.0)
    default_budget_per_month: float = Field(ge=0.0)
    unattended_supported: bool = False
    supported_endpoints: tuple[Literal["android", "desktop", "browser"], ...] = ()
    required_infrastructure: tuple[InfraRequirement, ...] = (InfraRequirement.NONE,)
    provider_compatibility: dict[str, str] = Field(default_factory=dict)
    priority: int = 0

    @model_validator(mode="after")
    def _mode_bounds_the_ceiling(self) -> "AgentTemplate":
        # docs/29 §9.6 rule 3 / 18 §3: a non-execute mode is capped at
        # low_read before the engine is asked, so a template declaring a higher
        # ceiling for one would be a contradiction, not a wider template.
        if self.run_mode is not TaskMode.EXECUTE and self.risk_ceiling is not RiskCategory.LOW_READ:
            raise ValueError(f"a {self.run_mode.value} template's risk_ceiling must be low_read")
        return self


def _model_ref(value: str) -> str:
    if value in ("agent.primary", "agent.fallback"):
        return value
    prefix = "models_as_tools."
    if value.startswith(prefix) and re.fullmatch(r"[A-Za-z0-9_\-.]{1,64}", value[len(prefix):]):
        return value
    raise ValueError(
        "model_ref names an existing model entry of the server configuration — "
        "'agent.primary', 'agent.fallback' or 'models_as_tools.<id>' — never a provider, "
        "an endpoint or a credential"
    )


ModelRef = Annotated[str, AfterValidator(_model_ref)]


class AgentModelProfile(_Strict):
    """docs/29 §6.2 — a named routing profile over an **existing** model entry
    of the server configuration (06). The profile carries no provider, model
    name, endpoint or credential of its own: all of that stays on the entry it
    references, whose key is resolved by the existing SecretStore path inside
    `server.models` at call time (06 §1, 12 §2). Features are routing
    metadata, never authorization."""

    profile_id: Annotated[str, Field(pattern=_PROFILE_SLUG)]
    version: int = Field(default=1, ge=1)
    model_ref: ModelRef
    features: frozenset[ModelFeature] = frozenset()
    context_window: int = Field(gt=0, le=10_000_000)
    latency_class: LatencyClass = LatencyClass.MEDIUM
    cost_class: CostClass | None = None
    supported_runtimes: tuple[Annotated[str, Field(pattern=_SLUG)], ...] = ("native",)
    required_apis: tuple[RequiredApi, ...] = (RequiredApi.CHAT_COMPLETIONS,)
    guardrail_notes: Annotated[str, Field(max_length=400)] = ""
    enabled: bool = True
    display_name: Annotated[str, Field(max_length=80), AfterValidator(_printable)] = ""


class AgentRuntimeProfile(_Strict):
    """docs/29 §7.2 — registered in code; operators only enable. A profile
    has no endpoint: where a future external runtime is reached is the
    provider adapter's concern below the Agent Gateway, never data an agent
    or a draft can supply."""

    runtime_id: Annotated[str, Field(pattern=_SLUG)]
    runtime_type: RuntimeType
    version_pin: Annotated[str, Field(min_length=1, max_length=64)]
    supported_template_tags: frozenset[TaskTag] = frozenset()
    supported_model_features: frozenset[ModelFeature] = frozenset()
    tool_interface: ToolInterface
    lifecycle_interface: LifecycleInterface
    persistence_model: PersistenceModel
    isolation_mode: IsolationMode
    network_requirements: NetworkRequirement
    observability: Observability
    cancellation: CancellationMode
    export_supported: bool
    deprovision_supported: bool
    # docs/29 §30.3: framework-native approval flows are never relied on.
    human_approval_mode: Literal["jarvis_gateway"]
    known_limitations: tuple[str, ...] = ()
    required_infrastructure: tuple[InfraRequirement, ...] = (InfraRequirement.NONE,)
    enabled: bool = False
    display_name: Annotated[str, Field(max_length=80)] = ""


class AgentSelection(_Strict):
    """docs/29 §8.2 — the deterministic selector's output, with its rule
    trace (`selection_reason`) and the filters that removed candidates."""

    template_id: str
    template_version: int
    runtime_id: str
    runtime_version_pin: str
    model_profile_id: str
    model_profile_version: int
    selection_reason: tuple[str, ...]
    constraints_applied: tuple[str, ...] = ()


class EnvelopeEntry(_Strict):
    """One capability the agent may *ever* attempt, and exactly which of its
    enumerated operations, narrowed by `scope` (docs/29 §10.2). A ceiling:
    every call inside it still needs the owner's live grant, activation and
    the engine's decision."""

    capability: str
    operations: tuple[str, ...]
    scope: dict[str, str] = Field(default_factory=dict)


class HydrationSpec(_Strict):
    user_memory: bool = False
    vault: bool = False
    vault_domains: tuple[str, ...] = ()


class CompiledTrigger(_Strict):
    kind: TriggerKind
    cron: str | None = None
    timezone: str = "UTC"
    next_fire_preview: datetime | None = None


class SpecBudget(_Strict):
    per_run: float = Field(ge=0.0)
    per_month: float = Field(ge=0.0)


class SpecBounds(_Strict):
    max_run_seconds: int = Field(ge=1)
    max_model_calls: int = Field(ge=1)
    max_tool_calls: int = Field(ge=0)


class CompiledAgentSpec(_Strict):
    """docs/29 §9.5 — immutable; one row of `agent_spec_versions`.

    `spec_hash` is SHA-256 over the canonical JSON of every field except
    `created_at` and `spec_hash` itself (`server/agents/hashing.py`). A stored
    spec whose hash no longer recomputes is tampered: the agent is revoked
    and refuses runs (§9.6 rule 8, AGENT-T33).
    """

    agent_id: UUID
    version: int = Field(ge=1)
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    owner_user_id: UUID
    graph_id: UUID | None
    name: str
    purpose: str
    desired_outcome: str
    sources: tuple[SourceRef, ...]
    selection: AgentSelection
    template_id: str
    template_version: int
    run_mode: TaskMode
    risk_ceiling: RiskCategory
    envelope_ceiling: tuple[EnvelopeEntry, ...]
    hydration: HydrationSpec
    notebook_enabled: bool
    trigger: CompiledTrigger
    outputs: tuple[OutputKind, ...]
    budget: SpecBudget
    bounds: SpecBounds
    instructions_template_version: int
    compiler_version: int
    created_at: datetime


# ── what goes back to the worker and the owner ────────────────────────────


class ClarificationQuestion(_Strict):
    """docs/29 §9.4: a question with closed choices where possible."""

    code: str
    prompt: str
    choices: tuple[str, ...] = ()


class AgentConfirmationCard(_Strict):
    """docs/29 §2.2 — what the owner approves, rendered by deterministic code
    from the compiled spec (`server/agents/rendering.py`), never from worker
    prose."""

    title: str
    does: str
    can: tuple[str, ...]
    cannot: tuple[str, ...]
    runs: str
    results_go: str
    budget: str
    engine: str


class CompiledAgentSpecView(_Strict):
    """A user-safe rendering of a spec: no ids beyond the agent's own, no
    scope internals beyond what the card shows."""

    agent_id: UUID
    version: int
    spec_hash: str
    template_id: str
    template_description: str
    card: AgentConfirmationCard
    selection_reason: tuple[str, ...]


class CompileOutcome(_Strict):
    """docs/29 §9.4 — returned to the worker as the tool observation, and to
    the owner by `POST /agents/compile`."""

    kind: Literal["compiled", "needs_clarification", "rejected"]
    compile_id: UUID | None = None
    expires_at: datetime | None = None
    spec_preview: CompiledAgentSpecView | None = None
    questions: tuple[ClarificationQuestion, ...] = ()
    reason_codes: tuple[str, ...] = ()


class AgentView(_Strict):
    """docs/29 §23.3."""

    agent_id: UUID
    name: str
    status: AgentStatus
    current_version: int
    template_id: str
    template_description: str
    runtime_display_name: str
    model_profile_display_name: str
    can: tuple[str, ...]
    cannot: tuple[str, ...]
    trigger_display: str
    budget: SpecBudget
    created_at: datetime
    updated_at: datetime
    last_run: datetime | None = None


class AgentListResponse(_Strict):
    items: tuple[AgentView, ...]


class AgentDetail(_Strict):
    """`GET /agents/{id}`: the head and, when it verifies, its current spec's
    user-safe view (a revoked agent has none)."""

    agent: AgentView
    spec: CompiledAgentSpecView | None = None


class CreateAgentRequest(_Strict):
    """`POST /agents` and `PATCH /agents/{id}` (docs/29 §23.2): only the id of
    a preview the same owner compiled — never a spec."""

    compile_id: UUID


class RunAgentRequest(_Strict):
    """`POST /agents/{id}/runs` (docs/29 §23.2). The owner, the graph, the
    agent version, its hash, the runtime and the input all come from the
    session and the stored spec — a request can name none of them.

    `reminder_delivery_id` (Phase 4, §17.1): the reminder this run was tapped
    from. It is a label, never authority — accepted only if that very device
    of that very owner received that reminder, for that very agent — and it
    makes a double tap one run (`kind = reminder_tap`)."""

    reminder_delivery_id: UUID | None = None


class AgentRunView(_Strict):
    """docs/29 §23.3. `task` is the ordinary task the run is: a paused run is
    confirmed through `/agent/tasks/{task_id}/confirm` like any task. An
    `unattended` run (Phase 5) never pauses: it has nobody to confirm."""

    run_id: UUID
    agent_id: UUID
    version: int
    kind: Literal["on_demand", "reminder_tap", "unattended"] = "on_demand"
    status: AgentRunStatus
    failure_code: str | None = None
    task_id: UUID | None = None
    started_at: datetime
    finished_at: datetime | None = None
    cost_total: float = 0.0
    inbox_item_id: UUID | None = None
    task: AgentResult | None = None


class AgentRunListResponse(_Strict):
    items: tuple[AgentRunView, ...]


class AgentInboxItemView(_Strict):
    """docs/29 §19 / §23.3: one run's result for its owner. `body` is plain
    text written by the agent — data to show, never instructions to follow.
    `withheld` says the result looked like it carried a credential and was
    not stored; `truncated` that it was cut to the inbox bound.

    Phase 5 (OD-AF-8): a `notice` is the owner's notification about the
    agent's standing delegation (a skipped, missed or coalesced occurrence, an
    expiry, a revocation, a spent budget, the breaker). It names no run and
    carries a closed `notice` code; like a result it is data only."""

    item_id: UUID
    agent_id: UUID
    agent_name: str | None
    run_id: UUID | None
    status: Literal["completed", "failed", "cancelled", "notice"]
    kind: Literal["result", "notice"] = "result"
    notice: "AgentNotice | None" = None
    failure_code: str | None = None
    body: str
    withheld: bool = False
    truncated: bool = False
    created_at: datetime
    read_at: datetime | None = None


class AgentInboxResponse(_Strict):
    items: tuple[AgentInboxItemView, ...]


class NotebookEntryView(_Strict):
    """docs/29 §16.3: one of the agent's own notes, shown to its owner. The
    value is what the agent wrote — data, never instructions."""

    key: str
    value: str
    updated_at: datetime


class NotebookResponse(_Strict):
    items: tuple[NotebookEntryView, ...]


class AgentPurposeCandidateView(_Strict):
    """docs/29 §18: the Judge's suggested rewording of one of the owner's
    agents' purpose. Shown to that agent's owner only; it changes nothing until
    the owner compiles it into an update and confirms that update."""

    candidate_id: UUID
    agent_id: UUID
    run_id: UUID | None = None
    status: Literal["pending", "approved", "rejected"]
    proposed_purpose: str
    expected_effect: str = ""
    created_at: datetime
    decided_at: datetime | None = None


class AgentPurposeCandidateList(_Strict):
    items: tuple[AgentPurposeCandidateView, ...]


class AgentSpecVersionExport(_Strict):
    version: int
    spec_hash: str
    created_at: datetime
    spec: CompiledAgentSpec


class AgentExport(_Strict):
    """docs/29 §23.2 `GET /agents/{id}/export`: the owner's own agent — its
    definition, every (hash-verified) spec version, its runs, its notebook and
    its inbox. Structurally nothing else: no provider key or `secret_ref` (a
    spec names a model *profile*, never a key), no session, device or
    confirmation token, no grant, decision or audit row."""

    format: Literal["jarvis.agent.export"] = "jarvis.agent.export"
    format_version: Literal[1] = 1
    exported_at: datetime
    agent: AgentView
    spec_versions: tuple[AgentSpecVersionExport, ...]
    runs: tuple[AgentRunView, ...]
    notebook: tuple[NotebookEntryView, ...]
    inbox: tuple[AgentInboxItemView, ...]


# ── the runtime-provider boundary (docs/29 §7.3, §11.2; interface only) ────


class ToolDescriptor(_Strict):
    """One tool operation a run may call, derived from envelope ∩ enabled
    tools (docs/29 §11.2). A description, never a grant."""

    tool_id: str
    operation: str
    input_schema: dict = Field(default_factory=dict)
    description: str = ""


class AgentRunContext(_Strict):
    """Everything a runtime provider receives for one run (docs/29 §11.2).

    Structurally, there is no field for a principal, a session, a device, a
    SecretStore handle or a provider key (§7.3): a provider can act only by
    calling back into JARVIS through the Agent Gateway — in-process for the
    native runtime (Phase 3), over an authenticated network surface for a
    future external one (Phase 6) — presenting one of its two run tokens, and
    JARVIS authorizes every call.

    The two tokens (§11.3) are opaque, per run and per purpose, short-lived,
    revocable and never logged: `run_token` is the Tool Gateway's,
    `model_run_token` the Model Gateway's. Each is useless outside this run,
    this agent, this spec hash and its own gateway."""

    run_id: UUID
    agent_id: UUID
    version: int
    spec_hash: str
    input_text: str
    deadline: datetime
    model_endpoint: str | None = None
    tool_endpoint: str | None = None
    run_token: str | None = Field(default=None, repr=False)
    model_run_token: str | None = Field(default=None, repr=False)
    tool_manifest: tuple[ToolDescriptor, ...] = ()
    model_alias: Literal["agent-model"] = "agent-model"


# ── the Agent Gateway (docs/29 §11–§13; Phase 3 in-process) ────────────────


class RunTokenPurpose(str, Enum):
    """docs/29 §11.3: a run gets one token per gateway. A token presented to
    the other gateway is invalid."""

    MODEL = "model"
    TOOL = "tool"


class AgentGatewayErrorCode(str, Enum):
    """Why the Agent Gateway refused a request (docs/29 §12.3, §13.2). Plain
    identifiers: no policy internals beyond the refusal itself."""

    INVALID_RUN_TOKEN = "invalid_run_token"   # unknown, malformed, wrong purpose or binding, expired, revoked
    REPLAY = "replay"                         # the nonce was already used for something else
    STALE_REQUEST = "stale_request"           # |now - sent_at| beyond the freshness window
    SCHEMA_INVALID = "schema_invalid"         # a malformed nonce or request
    RUN_NOT_RUNNING = "run_not_running"       # the run finished, or is not this agent's
    AGENT_UNAVAILABLE = "agent_unavailable"   # deleted, paused, revoked, not the owner's, out of its graph
    SPEC_CHANGED = "spec_changed"             # the agent has a newer version than the run's
    MODEL_NOT_ALLOWED = "model_not_allowed"   # a model the run's spec does not select
    BUDGET_EXCEEDED = "budget_exceeded"
    AGENT_BUDGET_EXHAUSTED = "agent_budget_exhausted"
    # docs/29 §12.3 (Phase 6, the HTTP Model Gateway).
    RATE_LIMITED = "rate_limited"                       # the owner's usage rates
    MAX_MODEL_CALLS = "max_model_calls"                 # the run's model-call bound (`[IMPL]`, register §2J)
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"   # the configured provider failed


class AgentGatewayRequest(_Strict):
    """What every request an agent run sends to the Agent Gateway carries
    (docs/29 §11.3, §13.2), whichever gateway and whichever runtime.

    * `run_id`, `agent_id` — the run the caller claims; they must be exactly
      the run and agent the token was issued for.
    * `run_token` — that gateway's run token (never logged, never shown).
    * `request_nonce` — 128 bits or more, base64url; never reused for a
      different request on the same token.
    * `sent_at` — when the request was made; a request outside the freshness
      window is refused.
    * `request_digest` — the canonical hash of exactly what is asked
      (`operation_key` for a tool call). A retry of the same request with the
      same nonce is answered from the stored response, never executed twice.

    The gateway, not this shape, decides whether a token or nonce is
    well-formed: a malformed one is a refusal with a code, never a crash."""

    run_id: UUID
    agent_id: UUID
    purpose: RunTokenPurpose
    run_token: str = Field(max_length=512, repr=False)
    request_nonce: str = Field(max_length=512)
    sent_at: datetime
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ChatTextPart(_Strict):
    """One text part of an OpenAI-style message. Nothing but text is accepted:
    no images (docs/29 §29.2 — `use_vision` is off in v1), no audio, no files."""

    type: Literal["text"]
    text: Annotated[str, Field(max_length=200_000)]


class ChatCompletionMessage(_Strict):
    """docs/29 §12.2: what an external runtime may send the Model Gateway. The
    roles a text chat model takes; no `tool`/`function` role — v1 offers the
    external runtime no tools through the model (OD-TOOL-3: none)."""

    role: Literal["system", "user", "assistant"]
    content: Annotated[str, Field(max_length=200_000)] | Annotated[list[ChatTextPart], Field(min_length=1,
                                                                                            max_length=64)]
    name: Annotated[str, Field(max_length=64)] | None = None

    @property
    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "".join(part.text for part in self.content)


class ChatCompletionRequest(_Strict):
    """docs/29 §12.2 — an OpenAI-compatible `POST /v1/chat/completions` body,
    closed: the request names an *alias* and messages, never a provider, an
    endpoint, a key, tools, a response format or a budget. Sampling hints a
    runtime commonly sends are accepted and **not** forwarded — the selected
    profile's configured entry decides how the model is called. `stream` and
    `n` are accepted only to be refused with a clear reason
    (`chat_request_refusal`)."""

    model: Annotated[str, Field(max_length=128)]
    messages: Annotated[list[ChatCompletionMessage], Field(min_length=1, max_length=256)]
    stream: bool = False
    n: int = Field(default=1, ge=1, le=16)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    user: Annotated[str, Field(max_length=128)] | None = None


class RuntimeRef(_Strict):
    runtime_id: str
    agent_id: UUID
    version: int
    external_ref: str | None = None


class RunHandle(_Strict):
    runtime_id: str
    run_id: UUID
    external_ref: str | None = None


class ProviderHealth(_Strict):
    ok: bool
    detail: str = ""


# ── standing delegation (docs/29 §15; Phase 5) ─────────────────────────────

# docs/29 §15.7, OD-AF-4 (register §2K): the most an unattended run may ever
# reach. Consequential and high_irreversible are refused, never paused.
UNATTENDED_RISK_CEILING = RiskCategory.LOW_WRITE

# Capabilities an unattended run can never use, whatever a spec or a grant
# says: anything that acts on a device or an app (no device principal exists),
# the break-glass family, and the Agent Factory itself (no agent creation,
# no self-delegation).
_NEVER_UNATTENDED = ("device.", "app.", "system.restricted", "agent.")
# `net.request` reaches outside: only reads (`get`) are in the ceiling — no
# POST or other side-effecting method (docs/29 §15.7).
_NET_READ_ONLY = {"net.request": frozenset({"get"})}


def unattended_refusal(capability: str, operation: str, tier: RiskCategory | None, *,
                       platform: str = "server") -> str | None:
    """docs/29 §15.7: `None` when (capability, operation) at `tier` on
    `platform` is inside the unattended ceiling; otherwise why not.

    Pure and fail-closed: an unknown tier is outside. It only removes — a
    `None` means nothing more than "the ordinary path may now decide", where
    the owner's live grants, the envelope and the engine still apply."""

    name = (capability or "").strip().lower()
    if (platform or "").strip().lower() != "server":
        return "device_execution"
    if name.startswith(("device.", "app.")):
        return "device_execution"
    if any(name == n or name.startswith(n) for n in _NEVER_UNATTENDED):
        return "never_unattended"
    allowed = _NET_READ_ONLY.get(name)
    if allowed is not None and operation not in allowed:
        return "external_side_effect"
    if tier is None:
        return "tier_unknown"
    if risk_severity(tier) > risk_severity(UNATTENDED_RISK_CEILING):
        return "above_unattended_ceiling"
    return None


class DelegationStatus(str, Enum):
    """docs/29 §15.3 — only `active` authorizes anything (as a ceiling)."""

    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


class DelegationRequest(_Strict):
    """`POST /agents/{id}/delegation` (docs/29 §23.2): the terms the owner
    chooses — how often at most, how much, for how long. Everything else (the
    agent, the owner, the graph, the spec version and hash, the envelope, the
    schedule) comes from the stored spec and the session; the request can
    name none of it. Budgets are explicit and non-zero (OD-AF-7) and bounded
    by the spec's; the expiry is bounded by `agents.delegation_max_days`
    (OD-AF-5) and defaults to it."""

    max_runs_per_day: int = Field(ge=1, le=24)
    budget_per_run: float = Field(gt=0.0)
    budget_per_month: float = Field(gt=0.0)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class StandingDelegationView(_Strict):
    delegation_id: UUID
    agent_id: UUID
    status: DelegationStatus
    status_reason: str | None = None
    spec_version: int
    schedule: str
    timezone: str
    max_runs_per_day: int
    budget_per_run: float
    budget_per_month: float
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    last_occurrence_at: datetime | None = None


class AgentNotice(str, Enum):
    """OD-AF-8: what the owner is told about a standing delegation, as an
    inbox notice. A closed vocabulary — a notice carries no free text from the
    agent or a model."""

    DELEGATION_EXPIRING = "delegation_expiring"
    DELEGATION_EXPIRED = "delegation_expired"
    DELEGATION_REVOKED = "delegation_revoked"
    DELEGATION_INVALIDATED = "delegation_invalidated"
    RUN_MISSED = "run_missed"
    MISFIRE_COALESCED = "misfire_coalesced"
    RUN_LIMIT_REACHED = "run_limit_reached"
    BUDGET_EXHAUSTED = "budget_exhausted"
    BREAKER_STOPPED = "breaker_stopped"
    RUN_NOT_STARTED = "run_not_started"


class CancelReason(str, Enum):
    OWNER_STOP = "owner_stop"
    EMERGENCY_STOP = "emergency_stop"
    SPEC_CHANGED = "spec_changed"
    PRINCIPAL_REVOKED = "principal_revoked"
    BUDGET_EXCEEDED = "budget_exceeded"
    DELETED = "deleted"
    DEADLINE = "deadline"


class DeprovisionReceipt(_Strict):
    ref: RuntimeRef
    removed: bool
    detail: str = ""


# ── the future model-as-tool request (docs/29 §12; interface only) ─────────


class ModelCallRequest(_Strict):
    """The arguments of an agent run's `agent.model` call — a specialized
    model call (docs/29 §12; wired for native runs in Phase 2).

    It says *what kind of model* the subtask wants (`role`, `preference`) and
    the prompt. It has no field for a provider, a model name, a profile id, a
    model reference, an endpoint or a key: deterministic JARVIS code picks the
    profile from those the compiled spec already permits
    (`server/agents/gateway/model_routing.py`), resolves the configured entry,
    and the provider key stays inside `server.models` (06 §1). The result is
    untrusted data returned to the agent (06 §4)."""

    role: ModelFeature | None = None
    preference: ModelPreference | None = None
    prompt: Annotated[str, Field(min_length=1, max_length=32_000), AfterValidator(_text_block)]
    system: Annotated[str, Field(max_length=8_000), AfterValidator(_text_block)] = ""

    @field_validator("prompt")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt is empty")
        return value


__all__ = [
    "ChatCompletionMessage",
    "ChatCompletionRequest",
    "ChatTextPart",
    "AgentNotice",
    "DelegationRequest",
    "DelegationStatus",
    "StandingDelegationView",
    "UNATTENDED_RISK_CEILING",
    "unattended_refusal",
    "AbilityName",
    "AgentConfirmationCard",
    "AgentDetail",
    "AgentDraft",
    "AgentExport",
    "AgentGatewayErrorCode",
    "AgentGatewayRequest",
    "AgentInboxItemView",
    "AgentInboxResponse",
    "AgentListResponse",
    "AgentModelProfile",
    "AgentPurposeCandidateList",
    "AgentPurposeCandidateView",
    "AgentRunContext",
    "AgentRunListResponse",
    "AgentRunStatus",
    "AgentRunView",
    "AgentRuntimeProfile",
    "AgentSelection",
    "AgentSpecVersionExport",
    "AgentStatus",
    "AgentTemplate",
    "AgentView",
    "CancelReason",
    "CancellationMode",
    "ClarificationQuestion",
    "CompileOutcome",
    "CompiledAgentSpec",
    "CompiledAgentSpecView",
    "CompiledTrigger",
    "CostClass",
    "CreateAgentRequest",
    "DeprovisionReceipt",
    "EnvelopeEntry",
    "FORBIDDEN_DRAFT_FIELDS",
    "HydrationSpec",
    "ISOLATION_ORDER",
    "InfraRequirement",
    "IsolationMode",
    "LatencyClass",
    "LifecycleInterface",
    "MemoryPolicy",
    "ModelCallRequest",
    "ModelFeature",
    "ModelPreference",
    "ModelRef",
    "NetworkRequirement",
    "NotebookAccess",
    "NotebookEntryView",
    "NotebookResponse",
    "Observability",
    "OutputKind",
    "PersistenceModel",
    "ProviderHealth",
    "RequiredApi",
    "RunAgentRequest",
    "RunHandle",
    "RunTokenPurpose",
    "RuntimeRef",
    "RuntimeType",
    "SourceKind",
    "SourceRef",
    "SpecBounds",
    "SpecBudget",
    "TaskTag",
    "ToolDescriptor",
    "ToolInterface",
    "TriggerKind",
    "TriggerRequest",
    "risk_severity",
]
