"""Configuration surface schema.

Source: 15_CONFIGURATION_SELF_HOSTING.md §2 (the config shape is
`[LOCKED shape; [IMPL] format`). Format choice (OD-CFG-1) is YAML — see
loader.py for the rationale.

[LOCKED] rules enforced here at the type level:
  - every credential is a `secret_ref` or an env-var *name*, never a literal
    value (SECRET-004) — see `SecretRef` below.
  - `intelligence.enabled` defaults `false` (INTEL-003).
  - `mem0.collection` and `vault.collection` must be distinct (VAULT-003) —
    enforced in `AppConfig` model validation, not per-section, since it's a
    cross-section rule.
"""

from __future__ import annotations

import os
import re
from pathlib import PurePath
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

# A secret reference names *where* to obtain a secret; it is never the
# secret itself. Two forms are accepted:
#   env:VAR_NAME          -> resolve from that environment variable at use
#   secretstore:<handle>  -> an opaque SecretStore handle (12_SECRETSTORE.md);
#                            resolution is not implemented in this branch.
_SECRET_REF_RE = re.compile(r"^(env|secretstore):[A-Za-z0-9_\-./]+$")


def _validate_secret_ref(value: str) -> str:
    if not _SECRET_REF_RE.match(value):
        raise ValueError(
            "secret reference must be 'env:VAR_NAME' or 'secretstore:<handle>' "
            "— a literal-looking value is a load-time validation failure "
            "(SECRET-004), not a warning"
        )
    return value


SecretRef = Annotated[str, AfterValidator(_validate_secret_ref)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TunnelConfig(StrictModel):
    provider: str = "cloudflare"
    config_ref: SecretRef | None = None


class ServerConfig(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, gt=0, lt=65536)
    base_url: str = "http://127.0.0.1:8000"
    tunnel: TunnelConfig = Field(default_factory=TunnelConfig)


LOCAL_MODEL_PROVIDERS = frozenset({"ollama"})


class ModelPricingConfig(StrictModel):
    """Per-1k-token prices, in the same currency as `security.budgets`.

    Required for every non-local provider (see `_require_pricing`): 13 §3 refuses
    a paid call that would breach budget, and a call whose cost cannot be
    projected cannot be checked — so an unpriced paid provider is a load-time
    failure rather than a silently unbudgeted one.
    """

    input_per_1k_tokens: float = Field(default=0.0, ge=0.0)
    output_per_1k_tokens: float = Field(default=0.0, ge=0.0)


def _require_pricing(provider: str, pricing: "ModelPricingConfig | None", where: str) -> None:
    if provider not in LOCAL_MODEL_PROVIDERS and pricing is None:
        raise ValueError(
            f"{where}: provider {provider!r} is not local, so it must declare "
            "`pricing` — an unpriced paid call cannot be checked against a budget "
            "(13 §3, fail-closed)"
        )


class ModelEntryConfig(StrictModel):
    """One model selection (06 §1). `secret_ref` is a handle, never a key."""

    provider: str
    model: str
    endpoint: str | None = None
    secret_ref: SecretRef | None = None
    timeout_seconds: float = Field(default=60.0, gt=0)
    pricing: ModelPricingConfig | None = None
    generation_policy: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _priced_if_paid(self) -> "ModelEntryConfig":
        _require_pricing(self.provider, self.pricing, "model entry")
        return self


class AgentBoundsConfig(StrictModel):
    """05 §3's hard ceilings. `[LOCKED]` that every one exists and is enforced;
    the numbers are `[IMPL]` / OD-02, `[PROPOSED]` in
    docs/DECISION_REGISTER.md §2."""

    max_iterations: int = Field(default=12, gt=0)
    max_model_calls: int = Field(default=16, gt=0)
    max_tool_calls: int = Field(default=24, ge=0)
    # OD-RT-1 — a model-tool is single-shot, so depth 1 is the natural cap; 0
    # disables model-tools entirely.
    max_model_tool_nesting_depth: int = Field(default=1, ge=0)
    wall_clock_timeout_seconds: float = Field(default=120.0, gt=0)
    # Paid spend allowed within one task. 0.0 → a paid call is refused.
    per_task_budget: float = Field(default=0.0, ge=0.0)
    max_parse_retries: int = Field(default=2, ge=0)
    max_input_chars: int = Field(default=8000, gt=0)
    max_observation_chars: int = Field(default=4000, gt=0)
    max_context_chars: int = Field(default=24000, gt=0)
    memory_top_k: int = Field(default=5, ge=0)


class AgentBreakerConfig(StrictModel):
    """18 §5.1/§9 — the circuit breaker's in-task triggers. Each is the count at
    which the task is stopped. Bounds, not authority: removing the section
    keeps these defaults, and no value can resume a stopped task. The numbers
    are `[IMPL]` (OD-SUP-2)."""

    denial_limit: int = Field(default=5, ge=1)
    violation_limit: int = Field(default=3, ge=1)
    rejection_limit: int = Field(default=3, ge=1)


class AgentRecoveryConfig(StrictModel):
    """18 §4/§9 — the worker chain and supervisory recovery. Optional: without
    this section the runtime behaves exactly as before (per-step primary →
    `fallback`, a malformed proposal fails the task). With it, the task has an
    active worker in an ordered chain — its resolved primary, then `fallback`,
    then `chain` — and the supervisor switches workers on failure.

    Every value is a bound, not authority: no worker in the chain gets any
    permission the task did not already have (18 §4.2). `chain` entries are
    operator-configured, so a worker one user configured is never used for
    another user's task (the data-flow rule)."""

    chain: list[ModelEntryConfig] = Field(default_factory=list)
    max_worker_switches: int = Field(default=2, ge=0)
    # OD-SUP-3 (default no): escalating an unresolved answer never picks a paid worker.
    escalate_on_unresolved: bool = False
    stall_window: int = Field(default=3, ge=1)
    loop_repeat_limit: int = Field(default=3, ge=2)


class AgentSectionConfig(StrictModel):
    """The primary agent's model selection (00_CANONICAL_PRD.md §19,
    P4 — swappable by configuration alone), its optional deterministic fallback
    (05 §5), and the runtime's bounds (05 §3)."""

    provider: str = "ollama"
    model: str = "qwen2.5:3b-instruct"
    endpoint: str | None = None
    secret_ref: SecretRef | None = None
    timeout_seconds: float = Field(default=60.0, gt=0)
    pricing: ModelPricingConfig | None = None
    generation_policy: dict = Field(default_factory=dict)
    # 05 §5: used only if configured, decided by the runtime, never the model.
    fallback: ModelEntryConfig | None = None
    bounds: AgentBoundsConfig = Field(default_factory=AgentBoundsConfig)
    breaker: AgentBreakerConfig = Field(default_factory=AgentBreakerConfig)
    recovery: AgentRecoveryConfig | None = None

    @model_validator(mode="after")
    def _priced_if_paid(self) -> "AgentSectionConfig":
        _require_pricing(self.provider, self.pricing, "agent")
        return self


class ModelToolEntryConfig(StrictModel):
    id: str
    provider: str
    model: str
    description: str = ""
    endpoint: str | None = None
    secret_ref: SecretRef | None = None
    timeout_seconds: float = Field(default=60.0, gt=0)
    pricing: ModelPricingConfig | None = None
    generation_policy: dict = Field(default_factory=dict)
    enabled: bool = False

    @model_validator(mode="after")
    def _priced_if_paid(self) -> "ModelToolEntryConfig":
        _require_pricing(self.provider, self.pricing, f"models_as_tools[{self.id}]")
        return self


class ToolEntryConfig(StrictModel):
    tool_id: str
    enabled: bool = False
    config: dict = Field(default_factory=dict)
    secret_ref: SecretRef | None = None
    overrides: dict = Field(default_factory=dict)


_EMBEDDER_CACHE_DEFAULT = "./data/models"


class Mem0SectionConfig(StrictModel):
    """docs/21 §2/§6. `path` holds the Chroma store and Mem0's own directory;
    `embedder_cache` holds the locally provisioned embedding model, which the
    server only ever loads offline (MP-T8)."""

    collection: str = "hypermind_memories"
    path: str = "./data/mem0_storage"
    embedder: str = "bge-small-en-v1.5"
    embedder_cache: str = _EMBEDDER_CACHE_DEFAULT
    # docs/21 §2.2: option (a) is the only mechanism implemented. Mem0 stores and
    # retrieves; every model call memory needs is made by JARVIS, metered.
    mode: str = Field(default="jarvis_extraction", pattern="^jarvis_extraction$")


class MemoryConfig(StrictModel):
    """11 / docs/21. Disabled by default: persistent memory needs a provisioned
    embedding model (`python -m server.memory provision`), and a fresh clone
    must start without one (HOST-001). Disabled, hydration degrades with the
    explicit FAIL-008 note and the memory endpoints answer `503`."""

    enabled: bool = False
    provider: str = Field(default="mem0", pattern="^mem0$")
    # Server-wide switch for memory *formation* (explicit adds and extraction).
    # Listing, correcting and deleting stay available when it is off (docs/21 §3).
    writes_enabled: bool = True
    # docs/21 §3: runtime-owned extraction at task completion, through the task's
    # own metered model call. Off by default — it costs a model call per task.
    auto_extract: bool = False
    max_fact_chars: int = Field(default=500, ge=20, le=2000)
    max_facts_per_task: int = Field(default=3, ge=1, le=10)
    mem0: Mem0SectionConfig = Field(default_factory=Mem0SectionConfig)


def _git_backed_only(value: bool) -> bool:
    if value is not True:
        raise ValueError(
            "vault.git_backed must be true: in the pilot, vault content changes only "
            "through Git review and a reindex (docs/21 §5, OD-VLT-1, MP-T10)"
        )
    return value


class VaultConfig(StrictModel):
    """11 §7 / docs/21 §5. `path` is the Git repository of curated markdown (the
    source of truth); `index_path` is the vault's own Chroma store, which is never
    the memory store's (VAULT-003)."""

    enabled: bool = False
    collection: str = "hypermind_vault"
    git_backed: Annotated[bool, AfterValidator(_git_backed_only)] = True
    path: str = "./data/vault"
    index_path: str = "./data/vault_index"
    embedder: str = "bge-small-en-v1.5"
    embedder_cache: str = _EMBEDDER_CACHE_DEFAULT
    chunk_chars: int = Field(default=1200, ge=200, le=8000)
    hydration_top_k: int = Field(default=3, ge=0, le=10)
    hydration_max_chars: int = Field(default=3000, ge=0, le=12000)


class IntelligenceConfig(StrictModel):
    """PRD INTEL-001..003 — disabled by default; Track B must remain fully
    functional this way (CFG-T5)."""

    enabled: bool = False
    provider: str | None = None
    config: dict = Field(default_factory=dict)


class VoiceConfig(StrictModel):
    stt: str | None = None
    diarization: str | None = None
    speaker_id: str | None = None
    tts: str | None = None


class FilesystemSandboxConfig(StrictModel):
    """09_FILESYSTEM_SANDBOX.md §1/§7/§10 (OD-FS-1/2/3). `[IMPL]`.

    `base_root` is the *only* place on the host every sandbox root is
    allocated under (`…/data/users/{user_id}/…`, `…/data/graphs/{graph_id}/…`,
    `…/data/tasks/{task_id}/tmp/…`, 09 §1) — never a path the agent supplies.

    `containment_mode` records 09 §8's open item (OD-FS-1) honestly rather
    than silently defaulting: `mediated` is realpath-verified Python-level
    containment (what this branch ships everywhere, including this
    development environment, which has no privilege to create mount
    namespaces); `mount_isolated` is 09 §8's `[REC]` physical-impossibility
    mechanism for a real multi-tenant deployment, not implemented by this
    branch. A tool contract's boundary is enforced either way — this field
    only affects *how strong* the containment guarantee is, and every audit
    event/test result records which mode produced it.
    """

    base_root: str = "./data/sandboxes"
    containment_mode: str = Field(default="mediated", pattern="^(mediated|mount_isolated)$")
    max_file_bytes: int = Field(default=25_000_000, gt=0)
    max_sandbox_bytes: int = Field(default=250_000_000, gt=0)
    max_archive_entries: int = Field(default=10_000, gt=0)
    max_archive_uncompressed_bytes: int = Field(default=250_000_000, gt=0)


class NetworkEgressConfig(StrictModel):
    """10_NETWORK_EGRESS.md §1/§3/§9 (OD-NET-1). `[IMPL]`.

    Baseline is default-deny (NET-001/003): with no network-capable tool
    enabled, nothing here grants any egress. `enforcement_mode` records the
    same honesty 09 §8 requires of the fs boundary: `mediated_proxy` is an
    application-level egress gateway (DNS-then-checked-IP-pinned connect,
    §5) — everything this branch ships and can run in this development
    environment without host firewall/netns privileges; `netns_filtered` is
    10 §3's `[REC]` kernel-level mechanism for real deployment, not built by
    this branch. The *guarantee* (NET-005 — a compromised tool cannot reach
    a denied destination via any mechanism) holds only as strongly as the
    active mode; `mediated_proxy` holds it for any tool that goes through
    this module's client, not against a tool that opens a raw socket of its
    own — precisely the gap `netns_filtered` closes and this config field
    exists to never let the weaker mode be mistaken for the stronger one.
    """

    enforcement_mode: str = Field(default="mediated_proxy", pattern="^(mediated_proxy|netns_filtered)$")
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    read_timeout_seconds: float = Field(default=10.0, gt=0)
    max_response_bytes: int = Field(default=5_000_000, gt=0)
    max_redirects: int = Field(default=3, ge=0)
    # The generic `net.request` tool's EgressPolicy (server/tools/platforms.py).
    # Closed by default — same "existence locked, values [IMPL]" posture as
    # `process.allowed_executables`: registering the tool does not by itself
    # grant it anywhere to go (OD-NET-2 is `[OPEN — OWNER]`, "default minimal").
    default_destinations: list[str] = Field(default_factory=list)
    default_internet: bool = False
    default_private_net: bool = False


class BreakGlassConfig(StrictModel):
    """20 §2.2 key one — operator enablement of break-glass (unconfined)
    execution. It only makes a per-task activation *possible*: nothing runs
    unconfined until a superuser activates a record for one live task
    (`POST /api/v1/admin/control/break-glass`), and then only the executables
    that record names, at most `max_invocations` times, for at most
    `max_window_minutes`.

    `allowed_executables` is its own list (20 §2.3): enabling break-glass
    never widens `process.allowed_executables`."""

    enabled: bool = False
    allowed_executables: list[str] = Field(default_factory=list)
    # Ceilings on what one activation may ask for — not the values it gets.
    max_window_minutes: int = Field(default=15, ge=1, le=15)
    max_invocations: int = Field(default=1, ge=1, le=100)


def _landlock_only(value: str) -> str:
    # 20 §2.5: the global `unconfined` switch is gone. Unconfined execution
    # exists only as a task-bound, superuser-activated break-glass record.
    if value != "landlock":
        raise ValueError(
            f"confinement_mode {value!r} is not supported: the only mode is 'landlock'. "
            "Unconfined execution is available only through execution.process.break_glass — "
            "a per-task, superuser-activated record (docs/20_CONFINEMENT_BREAK_GLASS.md §2)"
        )
    return value


class ProcessExecutionConfig(StrictModel):
    """The `system.restricted` executor's ceilings (08 §6 / 07 §5). No
    executable is allowed unless a tool declares it — an empty default
    allowlist means `run_shell_command` refuses everything until an operator
    opts specific executables in (§7 of this branch's brief: 'if shell
    execution exists at all, it must remain a separately governed high-risk
    mechanism')."""

    allowed_executables: list[str] = Field(default_factory=list)
    default_timeout_seconds: float = Field(default=10.0, gt=0)
    max_timeout_seconds: float = Field(default=60.0, gt=0)
    max_output_bytes: int = Field(default=1_000_000, gt=0)
    # Every child is confined by the kernel — no reads outside system
    # directories and its own task temp, no sockets, no signalling the server
    # (server/execution/confinement.py). Where the kernel cannot do that
    # (macOS, pre-5.13 Linux), nothing runs. `landlock` is the only value
    # (20 §2.5); a config still saying `unconfined` fails to load.
    confinement_mode: Annotated[str, AfterValidator(_landlock_only)] = "landlock"
    # Extra read-only paths an allow-listed program needs (its own libraries or
    # data outside /usr). Read-only, never writable.
    read_only_paths: list[str] = Field(default_factory=list)
    break_glass: BreakGlassConfig = Field(default_factory=BreakGlassConfig)


class ExecutionConfig(StrictModel):
    """The execution branch's own config surface — additive to 15 §2, not a
    redesign of any existing section."""

    filesystem: FilesystemSandboxConfig = Field(default_factory=FilesystemSandboxConfig)
    network: NetworkEgressConfig = Field(default_factory=NetworkEgressConfig)
    process: ProcessExecutionConfig = Field(default_factory=ProcessExecutionConfig)


class OIDCConfig(StrictModel):
    client_id: str
    issuer: str


class RateLimitsConfig(StrictModel):
    """13 §2. The per-minute rates apply to metered calls (model calls and tool
    executions) and are evaluated against the usage ledger."""

    per_user_requests_per_minute: int = Field(default=60, gt=0)
    per_device_requests_per_minute: int = Field(default=60, gt=0)
    global_requests_per_minute: int = Field(default=600, gt=0)
    per_session_concurrent_tasks: int = Field(default=1, gt=0)
    per_user_concurrent_tasks: int = Field(default=2, gt=0)
    global_concurrent_tasks: int = Field(default=8, gt=0)


class BudgetsConfig(StrictModel):
    per_user_daily_cost_limit: float = Field(default=0.0, ge=0.0)
    global_daily_cost_limit: float = Field(default=0.0, ge=0.0)


class SecurityConfig(StrictModel):
    oidc: OIDCConfig
    rate_limits: RateLimitsConfig = Field(default_factory=RateLimitsConfig)
    budgets: BudgetsConfig = Field(default_factory=BudgetsConfig)
    capability_defaults: dict = Field(default_factory=dict)


class SecretsStoreConfig(StrictModel):
    """`kek_source` names *where* the KEK comes from (e.g. an env var an
    operator sets out-of-band) — never the KEK itself (12_SECRETSTORE.md
    §2/§3). Foundation does not implement the SecretStore; this is only the
    declared source."""

    store: str = "encrypted_local"
    kek_source: SecretRef


class AppConfig(StrictModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    agent: AgentSectionConfig = Field(default_factory=AgentSectionConfig)
    models_as_tools: list[ModelToolEntryConfig] = Field(default_factory=list)
    tools: list[ToolEntryConfig] = Field(default_factory=list)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    vault: VaultConfig = Field(default_factory=VaultConfig)
    intelligence: IntelligenceConfig = Field(default_factory=IntelligenceConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    security: SecurityConfig
    secrets: SecretsStoreConfig

    # database_url is foundation's own addition (storage abstraction, §11
    # of this branch's instructions) — not part of 15 §2's shape verbatim,
    # since 15 predates any concrete storage decision (STORE-004 is [IMPL]).
    # Documented explicitly rather than silently folded into `server`.
    database_url: str = "sqlite+aiosqlite:///./data/hypermind.db"

    @model_validator(mode="after")
    def _distinct_mem0_and_vault_collections(self) -> "AppConfig":
        # VAULT-003 / CFG-T4: never the same collection name. Raised as a
        # plain ValueError (not ConfigError) so pydantic folds it into the
        # same ValidationError as every other field-level failure; the
        # loader is the single place that translates validation failures
        # into the user-facing ConfigError (fail-closed, one exception type
        # at the boundary).
        if self.memory.mem0.collection == self.vault.collection:
            raise ValueError(
                "memory.mem0.collection and vault.collection must be "
                f"distinct (VAULT-003) — both are "
                f"'{self.vault.collection}'"
            )
        # VAULT-003 again, one level down: two Chroma clients opened on one
        # directory share one underlying store, so distinct collection names
        # alone would not keep the stores apart. The three locations must be
        # pairwise disjoint — neither equal nor nested.
        locations = {
            "memory.mem0.path": self.memory.mem0.path,
            "vault.index_path": self.vault.index_path,
            "vault.path": self.vault.path,
        }
        resolved = {name: PurePath(os.path.abspath(p)) for name, p in locations.items()}
        names = list(resolved)
        for i, first in enumerate(names):
            for second in names[i + 1 :]:
                a, b = resolved[first], resolved[second]
                if a == b or a in b.parents or b in a.parents:
                    raise ValueError(
                        f"{first} and {second} must be separate, non-nested directories "
                        "(VAULT-003: memory and vault never share a store)"
                    )
        return self
