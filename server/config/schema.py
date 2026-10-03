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

import ipaddress
import os
import re
from pathlib import PurePath
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from shared.schemas.agent_factory import AgentModelProfile
from shared.schemas.push import FcmClientOptions, PushProvider

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

# H-1: PostgreSQL is the pilot's runtime store; SQLite stays for development
# and tests (it cannot meet PRD #32 — one writer at a time).
_DATABASE_DRIVERS = ("postgresql+asyncpg", "sqlite+aiosqlite")


def validate_database_url(value: str) -> str:
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    try:
        url = make_url(value)
    except ArgumentError:
        raise ValueError("database_url is not a database URL") from None
    if url.drivername not in _DATABASE_DRIVERS:
        raise ValueError(
            f"database_url must use one of {', '.join(_DATABASE_DRIVERS)} "
            f"(got {url.drivername!r}); PostgreSQL is the pilot's runtime store"
        )
    # SECRET-004 applies here too: a password never sits in configuration.
    # asyncpg reads it from PGPASSWORD or ~/.pgpass (the libpq conventions).
    if url.password is not None or any(k.lower() in ("password", "passfile") for k in url.query):
        raise ValueError(
            "database_url must not carry a password — supply it out of band "
            "through PGPASSWORD or a ~/.pgpass file (SECRET-004)"
        )
    return value


DatabaseUrl = Annotated[str, AfterValidator(validate_database_url)]


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


class EvaluationPostHocConfig(StrictModel):
    """19 §8: post-hoc evaluation may sample to control cost."""

    sample_successful: float = Field(default=0.1, ge=0.0, le=1.0)
    always_on_failure: bool = True


class EvaluationLiveConfig(StrictModel):
    """19 §6: live-window evaluation. Off by default; asynchronous — the task
    never waits for it."""

    enabled: bool = False
    every_n_steps: int = Field(default=1, ge=1)
    window_steps: int = Field(default=20, ge=1, le=200)


class EvaluationConfig(StrictModel):
    """19 §10 — the Judge. Optional: with `enabled: false` (the default) Track B
    is fully functional and every breaker trigger except the evaluator's still
    works (JDG-T1). Nothing here is authority: the Judge scores and records, and
    its only effect on a running task is a stop *request* to the deterministic
    breaker, which the operator must opt into (`may_request_stop`).

    `provider` is an entry shaped like `agent` (06): an operator-configured
    model, so a cloud Judge is a disclosed configuration choice, never a hidden
    egress (19 §4). `evaluator: rules` needs no model at all."""

    enabled: bool = False
    evaluator: Literal["llm", "rules"] = "llm"
    provider: ModelEntryConfig | None = None
    post_hoc: EvaluationPostHocConfig = Field(default_factory=EvaluationPostHocConfig)
    live: EvaluationLiveConfig = Field(default_factory=EvaluationLiveConfig)
    # The operator must opt in before any evaluator can call `trip()`.
    may_request_stop: bool = False
    # 19 §6: more evaluator stops than this in `stop_alert_window_minutes`
    # alerts the operator (audited), who may disable the Judge.
    stop_alert_threshold: int = Field(default=5, ge=1)
    stop_alert_window_minutes: int = Field(default=60, ge=1)
    # 19 §8: the Judge's own daily budget, separate from every task's. 0.0 → a
    # paid Judge call is refused (a local Judge costs nothing).
    budget: float = Field(default=0.0, ge=0.0)
    # OD-JDG-2 is open (own / per-user / global). Judge spend is never charged
    # to the evaluated task or to the user's own budget or rates (19 §8). This
    # switch only says whether it *also* counts toward the global daily
    # budget; the default is the more restrictive reading, pending the owner.
    budget_scope: Literal["own", "own_and_global"] = "own_and_global"
    max_observation_chars: int = Field(default=2000, ge=100, le=20000)
    queue_size: int = Field(default=64, ge=1, le=10000)

    @model_validator(mode="after")
    def _provider_when_llm(self) -> "EvaluationConfig":
        if self.enabled and self.evaluator == "llm" and self.provider is None:
            raise ValueError(
                "evaluation.enabled with evaluator 'llm' needs evaluation.provider — the "
                "Judge's model is an explicit operator choice (19 §4), never a silent default"
            )
        return self


class SchedulerConfig(StrictModel):
    """docs/22 §4 — task-linked reminders. A firing reminder delivers a message;
    it never executes (docs/22 §0), so nothing here can authorize anything: these
    are bounds on what a user may schedule and how late a reminder may arrive.

    `backend` names the `SchedulerBackend` implementation. `apscheduler_db` is
    APScheduler's trigger semantics (cron / one-shot) over the application
    database as the one job store (docs/22 §3); it is the only one shipped.
    `agent_tool_enabled` registers the `scheduler.reminders` tool (the
    `scheduler.create` capability, OD-SCH-1 still open) — off removes the agent
    path while leaving `POST /api/v1/jobs` and firing untouched."""

    enabled: bool = True
    backend: str = Field(default="apscheduler_db", pattern="^apscheduler_db$")
    misfire_grace_minutes: int = Field(default=60, ge=0, le=24 * 60)
    max_active_jobs_per_user: int = Field(default=50, ge=1)
    agent_tool_enabled: bool = True
    # How often the runner looks for due jobs when nothing wakes it sooner.
    poll_seconds: float = Field(default=30.0, gt=0, le=300)
    # A recurring reminder may not fire more often than this (a per-minute cron
    # would turn the owner's phone into a notification stream).
    min_recurrence_minutes: int = Field(default=5, ge=1)
    max_horizon_days: int = Field(default=730, ge=1)
    max_task_reason_chars: int = Field(default=1000, ge=20, le=8000)
    # docs/22 §3: reminders for an offline device wait for its reconnect — but
    # not forever; an undelivered one past this age is dropped and audited.
    pending_delivery_ttl_hours: int = Field(default=72, ge=1, le=24 * 30)


_VOICE_PROVIDER_ID = r"^[a-z0-9][a-z0-9_\-]{0,63}$"
VOICE_ON_DEVICE = "device"


def _voice_endpoint(value: str) -> str:
    """A server voice provider is a declared network destination (docs/27 §1,
    10): an https origin, or plain http only to this machine's loopback."""

    from urllib.parse import urlsplit

    parts = urlsplit(value)
    loopback = parts.hostname in ("127.0.0.1", "localhost", "::1")
    if parts.scheme not in ("https", "http") or not parts.hostname or (parts.scheme == "http" and not loopback):
        raise ValueError("voice provider endpoint must be https://…, or http:// only on loopback")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("voice provider endpoint must be a plain URL (no credentials, query or fragment)")
    import ipaddress

    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        address = None
    # Cloud-metadata and other non-routable targets are never a voice provider
    # (10 §4); a self-hosted provider on the LAN or loopback is allowed.
    if address is not None and (address.is_link_local or address.is_multicast or address.is_unspecified):
        raise ValueError("voice provider endpoint may not be a link-local, multicast or unspecified address")
    return value.rstrip("/")


class VoicePricingConfig(StrictModel):
    """Deterministic, projectable prices (13 §3) in `security.budgets`' currency:
    STT by the audio's size, TTS by the text's length."""

    per_audio_mb: float = Field(default=0.0, ge=0.0)
    per_1k_chars: float = Field(default=0.0, ge=0.0)


class VoiceProviderConfig(StrictModel):
    """One server-side voice provider (docs/27 §1) — an explicit network
    component. `endpoint` is the only host it may contact; its key is a
    `secret_ref` (class `model_api_key` in the SecretStore), resolved per call."""

    id: str = Field(pattern=_VOICE_PROVIDER_ID)
    kind: str = Field(default="openai_compatible", pattern="^openai_compatible$")
    endpoint: Annotated[str, AfterValidator(_voice_endpoint)]
    secret_ref: SecretRef | None = None
    stt_model: str | None = Field(default=None, min_length=1, max_length=128)
    tts_model: str | None = Field(default=None, min_length=1, max_length=128)
    tts_voice: str = Field(default="alloy", pattern=r"^[A-Za-z0-9_\-]{1,64}$")
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    # Required for anything not on this machine: an unpriced paid call cannot
    # be checked against a budget (13 §3, fail-closed).
    pricing: VoicePricingConfig | None = None

    @model_validator(mode="after")
    def _shape(self) -> "VoiceProviderConfig":
        if self.id == VOICE_ON_DEVICE:
            raise ValueError("voice provider id 'device' is reserved for on-device placement")
        if self.stt_model is None and self.tts_model is None:
            raise ValueError(f"voice provider {self.id!r} declares neither stt_model nor tts_model")
        from urllib.parse import urlsplit

        if urlsplit(self.endpoint).hostname not in ("127.0.0.1", "localhost", "::1") and self.pricing is None:
            raise ValueError(
                f"voice provider {self.id!r} is not local, so it must declare `pricing` "
                "(13 §3 — an unpriced call cannot be budget-checked)"
            )
        return self


class VoiceConfig(StrictModel):
    """docs/27 §4. Placement per direction: `device` (the default — Android
    speech recognition and system TTS; raw audio never leaves the phone), a
    configured provider id (server-side, behind `/api/v1/voice/*`), or `null`
    (off). Track B is fully functional with every one of them `null` (VOI-T1).

    `diarization` and `speaker_id` are `[FUTURE]` provider slots: only `null`
    loads today, so no speaker signal exists that anything could misuse
    (VOICE-002, INV-14). There is no audio-retention switch: raw audio is
    discarded after transcription, and the per-user opt-in (LIFE-002) is not
    built — so nothing can turn retention on."""

    stt: str | None = VOICE_ON_DEVICE
    diarization: str | None = None
    speaker_id: str | None = None
    tts: str | None = VOICE_ON_DEVICE
    providers: list[VoiceProviderConfig] = Field(default_factory=list)
    max_audio_bytes: int = Field(default=10_000_000, gt=0, le=25_000_000)
    max_transcript_chars: int = Field(default=8000, gt=0, le=32_000)
    max_tts_chars: int = Field(default=2000, gt=0, le=10_000)
    max_tts_audio_bytes: int = Field(default=10_000_000, gt=0, le=25_000_000)

    @model_validator(mode="after")
    def _placements(self) -> "VoiceConfig":
        ids = [p.id for p in self.providers]
        if len(ids) != len(set(ids)):
            raise ValueError("voice.providers ids must be unique")
        by_id = {p.id: p for p in self.providers}
        for direction, model_field in (("stt", "stt_model"), ("tts", "tts_model")):
            value = getattr(self, direction)
            if value is None or value == VOICE_ON_DEVICE:
                continue
            provider = by_id.get(value)
            if provider is None:
                raise ValueError(f"voice.{direction}: {value!r} is not a configured provider id")
            if getattr(provider, model_field) is None:
                raise ValueError(f"voice.{direction}: provider {value!r} declares no {model_field}")
        for future in ("diarization", "speaker_id"):
            if getattr(self, future) is not None:
                raise ValueError(
                    f"voice.{future} is a future provider slot (docs/27 §1) and must be null: "
                    "speaker processing is not built, and is never an authorization signal"
                )
        return self


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


_APP_LINK_FINGERPRINT = re.compile(r"^(?:[0-9A-F]{2}:){31}[0-9A-F]{2}$")
_ANDROID_PACKAGE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")


class AndroidAppLinksConfig(StrictModel):
    """docs/23 §3: the browser → app return after Google login. `base_url` is
    the server's stable public HTTPS origin (the named tunnel hostname that is
    also Google's redirect URI host); Android verifies the app owns links there
    through `/.well-known/assetlinks.json`, built from the two fields below.
    Signing-certificate fingerprints are public values, not secrets."""

    base_url: str | None = None
    package_name: str = "com.hypermind.jarvis"
    sha256_cert_fingerprints: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _shape(self) -> "AndroidAppLinksConfig":
        if self.base_url is not None:
            if not self.base_url.startswith("https://") or self.base_url.rstrip("/").count("/") != 2:
                raise ValueError("android.app_links.base_url must be a bare https:// origin")
        if not _ANDROID_PACKAGE.match(self.package_name):
            raise ValueError("android.app_links.package_name is not a package name")
        for fingerprint in self.sha256_cert_fingerprints:
            if not _APP_LINK_FINGERPRINT.match(fingerprint):
                raise ValueError("android.app_links.sha256_cert_fingerprints: expected AA:BB:… (32 bytes)")
        return self

    @property
    def configured(self) -> bool:
        return self.base_url is not None and bool(self.sha256_cert_fingerprints)


class AndroidAppClassificationConfig(StrictModel):
    """docs/CAPABILITY_MATRIX.md §5.1 — the owner's sensitive-app
    classification, by package name. Every package absent from all three lists
    is *unclassified*: UI control there is denied and screenshots refused.
    `sensitive` raises UI operations to at least `consequential`; `payment` to
    `high_irreversible`. Screenshots are allowed only for `non_sensitive`."""

    non_sensitive: list[str] = Field(default_factory=list)
    sensitive: list[str] = Field(default_factory=list)
    payment: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _valid(self) -> "AndroidAppClassificationConfig":
        seen: set[str] = set()
        for name in (*self.non_sensitive, *self.sensitive, *self.payment):
            if not _ANDROID_PACKAGE.match(name):
                raise ValueError(f"android.app_classification: {name!r} is not a package name")
            if name in seen:
                raise ValueError(f"android.app_classification: {name!r} is classified more than once")
            seen.add(name)
        return self


class AndroidFcmConfig(StrictModel):
    """docs/23 §4: Firebase Cloud Messaging as the wake provider.

    `client` holds the public Firebase identifiers the phone uses to obtain a
    registration token (served to enrolled devices by
    `GET /devices/push-config`). `service_account_ref` names where the
    server's *sending* credential lives — a Google service-account key (JSON)
    in the SecretStore (class `oauth_token`, server-owned) or an environment
    variable; never a literal. The phone never sees it."""

    client: FcmClientOptions
    service_account_ref: SecretRef
    # Wakes to one device closer together than this are coalesced into one.
    min_interval_seconds: int = Field(default=30, ge=5, le=600)


class AndroidPushConfig(StrictModel):
    """docs/23 §4 push wake — optional, and **off by default**.

    `none` (the default): the server never sends a push and phones never
    initialize a push SDK; a sleeping phone reconnects when the user opens the
    app or the network returns. `fcm`: a content-free wake
    (`shared/schemas/push.py`, ANDC-T9) asks a phone to reconnect its
    authenticated channel. A push never carries or triggers anything else."""

    provider: PushProvider = PushProvider.NONE
    fcm: AndroidFcmConfig | None = None

    @model_validator(mode="after")
    def _shape(self) -> "AndroidPushConfig":
        if self.provider is PushProvider.FCM and self.fcm is None:
            raise ValueError("android.push.provider 'fcm' needs an android.push.fcm section")
        return self


class AndroidConfig(StrictModel):
    """docs/23 — the Android client's server-side surface.

    `enabled` turns on the device channel (the WebSocket `DeviceTransport`).
    Off by default: until an operator enables it, every device operation fails
    `device_unavailable` exactly as before this section existed."""

    enabled: bool = False
    # docs/23 §4: short-lived operation authority (default 30 s, never above
    # the contract's 60 s ceiling).
    operation_ttl_seconds: int = Field(default=30, ge=5, le=60)
    app_links: AndroidAppLinksConfig = Field(default_factory=AndroidAppLinksConfig)
    app_classification: AndroidAppClassificationConfig = Field(
        default_factory=AndroidAppClassificationConfig
    )
    # docs/23 §6 level 4 (OD-AND-4): the vision-capable model that describes a
    # `capture_screenshot` image server-side. Absent (the default) means no
    # vision rung: a screenshot is dropped unread (`vision_not_configured`).
    # A paid provider must be priced, like every other model entry (13 §3).
    vision: ModelEntryConfig | None = None
    # docs/23 §4: the optional push wake (FCM). Default `none`.
    push: AndroidPushConfig = Field(default_factory=AndroidPushConfig)


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
    # docs/22 §4: reminder creations per user per rolling hour, over the jobs
    # table itself (API and agent path alike). Breach → `429` (FAIL-010).
    scheduler_creations_per_hour: int = Field(default=20, gt=0)


class BudgetsConfig(StrictModel):
    per_user_daily_cost_limit: float = Field(default=0.0, ge=0.0)
    global_daily_cost_limit: float = Field(default=0.0, ge=0.0)


class SecurityConfig(StrictModel):
    oidc: OIDCConfig
    rate_limits: RateLimitsConfig = Field(default_factory=RateLimitsConfig)
    budgets: BudgetsConfig = Field(default_factory=BudgetsConfig)
    capability_defaults: dict = Field(default_factory=dict)


class AgentRuntimeToggle(StrictModel):
    enabled: bool = False


_RUNTIME_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")

_LISTEN_TCP_RE = re.compile(r"^(?:\[(?P<v6>[0-9A-Fa-f:]+)\]|(?P<v4>[0-9.]+)):(?P<port>[0-9]{1,5})$")


def _internal_listen(value: str) -> str:
    """docs/29 §21 item 8: the Model Gateway listens on an internal binding
    only — a Unix socket at an absolute, normalized path, or a loopback
    address with an explicit port. Never a wildcard, a routable or private
    address (a container network's address is OD-AF-12's to decide, not this
    setting's), a hostname (its resolution is not this setting's to trust),
    or a URL."""

    if value.startswith("unix:"):
        path = value[len("unix:"):]
        if not path.startswith("/") or path.endswith("/") or any(p in ("", ".", "..") for p in path.split("/")[1:]):
            raise ValueError("agents.model_gateway.listen: a unix: binding needs an absolute, normalized path")
        return value
    match = _LISTEN_TCP_RE.fullmatch(value)
    if match is None:
        raise ValueError("agents.model_gateway.listen must be unix:/abs/path or a loopback address:port")
    try:
        address = ipaddress.ip_address(match.group("v6") or match.group("v4"))
    except ValueError:
        raise ValueError("agents.model_gateway.listen: not an IP address") from None
    if not address.is_loopback:
        raise ValueError("agents.model_gateway.listen: only a loopback address is internal")
    if not 1 <= int(match.group("port")) <= 65535:
        raise ValueError("agents.model_gateway.listen: the port must be 1-65535")
    return value


class AgentModelGatewayConfig(StrictModel):
    """docs/29 §12 (Phase 6, slice 6A): the HTTP Model Gateway an external
    runtime calls instead of holding a provider key. `[PROPOSAL]` — off by
    default; a separate listener, never mounted on the public API.

    * `listen` — the internal binding (`unix:/abs/path`, or a loopback
      `address:port`); required when enabled;
    * `max_request_bytes` — a body larger than this is refused unread;
    * `max_completion_chars` — an answer longer than this is cut, with a
      marker (05 §6; `[IMPL]`, register §2J)."""

    enabled: bool = False
    listen: Annotated[str, AfterValidator(_internal_listen)] | None = None
    max_request_bytes: int = Field(default=1_048_576, ge=1024, le=16_777_216)
    max_completion_chars: int = Field(default=16_000, ge=1_000, le=200_000)


def _run_dir(value: str | None) -> str | None:
    if value is None:
        return None
    if not value.startswith("/") or os.path.normpath(value) != value.rstrip("/") or value.rstrip("/") in ("", "/"):
        raise ValueError("agents.containers.run_dir must be an absolute, normalized directory other than /")
    return value.rstrip("/")


class AgentContainersConfig(StrictModel):
    """docs/29 §21 (Phase 6, slice 6C; OD-AF-11/12, ratified 2026-10-02): the
    external runtime's container. Off by default. The launch itself — gVisor
    `runsc`, no network interface, read-only, no capabilities,
    `no-new-privileges`, non-root, digest-pinned image — is fixed in
    `server/execution/containers.py` and not configurable here.

    * `podman` — the rootless Podman binary (absolute path);
    * `run_dir` — where each run's private workspace (sockets, scratch) is
      made; required when enabled;
    * `ignore_cgroups` — only where the host does not delegate cgroups to the
      server's user: the memory, CPU and process limits are then not enforced
      (recorded as a residual in BR-T2);
    * `reconcile_interval_seconds` — at most 600 (docs/29 §25.2: every 10
      minutes)."""

    enabled: bool = False
    podman: str = "/usr/bin/podman"
    run_dir: Annotated[str, AfterValidator(_run_dir)] | None = None
    ignore_cgroups: bool = False
    memory_mb: int = Field(default=2048, ge=256, le=16384)
    cpus: float = Field(default=2.0, ge=0.25, le=16.0)
    pids_limit: int = Field(default=512, ge=64, le=4096)
    kill_grace_seconds: int = Field(default=10, ge=1, le=60)
    reconcile_interval_seconds: int = Field(default=600, ge=30, le=600)

    @model_validator(mode="after")
    def _consistent(self) -> "AgentContainersConfig":
        if not self.podman.startswith("/"):
            raise ValueError("agents.containers.podman must be an absolute path")
        if self.enabled and self.run_dir is None:
            raise ValueError("agents.containers.enabled requires agents.containers.run_dir")
        return self


class AgentsConfig(StrictModel):
    """The Agent Factory (docs/29 §24). `[PROPOSAL — NOT CANONICAL UNTIL
    RATIFIED]` — additive to `15` §2, and every default is the safe reading:

    * `enabled: false` — the whole feature is off: no agent tool is registered,
      the `/agents` endpoints answer `503`, and the existing system is
      unchanged.
    * no template is enabled and no model profile exists until an operator
      adds them;
    * budgets default to `0.0`, which refuses every paid model call (OD-02) —
      an agent whose only eligible model is paid cannot be compiled;
    * unattended execution (docs/29 §15, Phase 5) is off: PRD §22 now allows
      it under a StandingDelegation (OD-AF-2, ratified 2026-10-02, register
      §2K), and the operator switches it on only explicitly —
      `unattended_enabled: true` loads only with `enabled: true` and
      `standing_delegation_ratified: true`. Delegations last at most
      `delegation_max_days` (OD-AF-5); a missed schedule occurrence runs at
      most once, within `misfire_grace_minutes`; the trigger loop looks for
      due agents every `trigger_interval_seconds`.

    Cross-references (template ids, `model_ref` targets, implemented
    providers, cost classes, runtime providers) are checked when the registries
    are built at startup (`server/agents/registry`), fail-closed.
    """

    enabled: bool = False
    enabled_templates: list[str] = Field(default_factory=list)
    max_agents_per_user: int = Field(default=5, ge=1, le=100)
    compile_preview_ttl_minutes: int = Field(default=15, ge=1, le=120)
    default_budget_per_run: float = Field(default=0.0, ge=0.0)
    default_budget_per_month: float = Field(default=0.0, ge=0.0)
    delegation_max_days: int = Field(default=30, ge=1, le=365)
    unattended_enabled: bool = False
    standing_delegation_ratified: bool = False
    # `[IMPL]` docs/29 §15.5 names the grace without fixing a value.
    misfire_grace_minutes: int = Field(default=15, ge=1, le=1440)
    trigger_interval_seconds: int = Field(default=60, ge=5, le=3600)
    model_profiles: list[AgentModelProfile] = Field(default_factory=list)
    model_profiles_open_to_all: list[str] = Field(default_factory=list)
    runtimes: dict[str, AgentRuntimeToggle] = Field(
        default_factory=lambda: {"native": AgentRuntimeToggle(enabled=True)}
    )
    model_gateway: AgentModelGatewayConfig = Field(default_factory=AgentModelGatewayConfig)
    containers: AgentContainersConfig = Field(default_factory=AgentContainersConfig)

    @model_validator(mode="after")
    def _consistent(self) -> "AgentsConfig":
        if self.unattended_enabled and not self.standing_delegation_ratified:
            raise ValueError(
                "agents.unattended_enabled requires agents.standing_delegation_ratified "
                "(docs/29 §15.8: the PRD §22 amendment must land first)"
            )
        if self.unattended_enabled and not self.enabled:
            raise ValueError("agents.unattended_enabled requires agents.enabled")
        if self.model_gateway.enabled and not self.enabled:
            raise ValueError("agents.model_gateway.enabled requires agents.enabled")
        if self.model_gateway.enabled and self.model_gateway.listen is None:
            raise ValueError("agents.model_gateway.enabled requires agents.model_gateway.listen")
        if self.containers.enabled and not self.enabled:
            raise ValueError("agents.containers.enabled requires agents.enabled")
        if len(set(self.enabled_templates)) != len(self.enabled_templates):
            raise ValueError("agents.enabled_templates lists a template twice")
        ids = [p.profile_id for p in self.model_profiles]
        if len(set(ids)) != len(ids):
            raise ValueError("agents.model_profiles defines a profile_id twice")
        unknown = sorted(set(self.model_profiles_open_to_all) - set(ids))
        if unknown:
            raise ValueError(f"agents.model_profiles_open_to_all names unknown profiles {unknown}")
        bad = sorted(k for k in self.runtimes if not _RUNTIME_KEY_RE.match(k))
        if bad:
            raise ValueError(f"agents.runtimes: invalid runtime ids {bad}")
        return self


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
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    android: AndroidConfig = Field(default_factory=AndroidConfig)
    # docs/29 — the Agent Factory, off by default.
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    security: SecurityConfig
    secrets: SecretsStoreConfig

    # database_url is foundation's own addition (storage abstraction, §11
    # of this branch's instructions) — not part of 15 §2's shape verbatim,
    # since 15 predates any concrete storage decision (STORE-004 is [IMPL]).
    # Documented explicitly rather than silently folded into `server`.
    # H-1: the pilot runs on PostgreSQL (`postgresql+asyncpg://user@host:5432/db`,
    # password via PGPASSWORD / ~/.pgpass). The SQLite default is for local
    # development and tests only.
    database_url: DatabaseUrl = "sqlite+aiosqlite:///./data/hypermind.db"

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
