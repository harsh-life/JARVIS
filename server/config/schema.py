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

import re
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


class Mem0SectionConfig(StrictModel):
    collection: str = "hypermind_memories"
    path: str = "./data/mem0_storage"
    embedder: str = "bge-small-en-v1.5"


class MemoryConfig(StrictModel):
    mem0: Mem0SectionConfig = Field(default_factory=Mem0SectionConfig)


class VaultConfig(StrictModel):
    collection: str = "hypermind_vault"
    git_backed: bool = True
    path: str = "./data/vault"


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
        return self
