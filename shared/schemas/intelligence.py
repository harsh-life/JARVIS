"""Intelligence configuration — the shared contract (docs/30, Phase 0).

What a user configures in the Intelligence Workbench, and what the isolated
experiment harness (`experiments/`) runs, are the *same* documents:

* `IntelligenceProfileDocument` — one user-owned, versioned experiment
  profile: a Central assignment, an orchestration backend selection,
  specialist assignments, execution constraints, a context policy and
  evaluation settings. It names operator catalogue entries **by id only**: no
  endpoint, no key, no price, no identity field. Where the server connects,
  what it costs and whose request it is are decided by the server.
* `profile_hash` — the canonical SHA-256 of a document. A saved version is
  immutable; its hash is what every experiment run records.
* `ExperimentSnapshot` — an immutable export of one version with the
  catalogue entries it names resolved to provider / model / price. Never an
  endpoint or a credential: the harness maps catalogue ids to providers it
  was explicitly configured with.

Nothing here carries authority. Selecting a model or a backend grants no
capability, no tool and no budget beyond the operator's ceilings; the
production request lifecycle does not read these documents at all in this
phase (`production_active` is always false).

Vocabulary that has to agree with code elsewhere is declared here, so the
server can report it truthfully without importing the harness:

* `HARNESS_ADAPTERS` — the orchestration adapters the harness actually
  implements, and how (`integrated` against a real model call path, or
  `scripted` — a test double). An operator catalogue entry naming any other
  adapter (`letta`, `langgraph`, `jev`, …) is reported `not_integrated`.
* `OUTPUT_SCHEMAS` — the specialist result-body schemas the harness can
  validate. `tests/experiments` holds both lists to the harness registries.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# Catalogue ids: `agent.primary` / `agent.fallback` (the M2 catalogue, reused
# as-is) and operator-declared experiment entries, `workbench.<slug>`.
CatalogId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9][a-z0-9_-]*)+$", max_length=64)]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")]
# A specialization is data in the operator catalogue, not a closed enum: the
# three initial ones ship as defaults, and an operator may add others.
SpecializationId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,31}$")]
VersionLabel = Annotated[str, StringConstraints(pattern=r"^[0-9A-Za-z][0-9A-Za-z._-]{0,15}$")]

PROFILE_SCHEMA_VERSION = 1
SNAPSHOT_VERSION = 1


class BackendKind(str, Enum):
    """What an orchestration backend *is* — they are different components,
    not interchangeable products (docs/30 §4)."""

    # A model (or classifier) that returns a structured plan: decomposition,
    # specialist selection, routing. It runs nothing.
    PLANNER = "planner"
    # A runtime that coordinates delegated work over several steps and keeps
    # bounded workflow state. In this phase it may only *propose*: every
    # model call it wants is made by JARVIS, under JARVIS's budget.
    AGENTIC_RUNTIME = "agentic_runtime"
    # Fixed, hand-authored task templates. No model decides the plan.
    WORKFLOW_ENGINE = "workflow_engine"


class DelegationMode(str, Enum):
    # The plan is the template; nothing is chosen at run time.
    FIXED_TEMPLATE = "fixed_template"
    # The backend returns a plan; JARVIS validates and dispatches it.
    PLAN_ONLY = "plan_only"
    # The backend coordinates, but each specialist call is a proposal JARVIS
    # validates, budgets and makes itself. (Execution by the runtime itself
    # is not a mode this phase offers.)
    JARVIS_MEDIATED = "jarvis_mediated"


class StateModel(str, Enum):
    STATELESS = "stateless"
    # State lives only for one run and is discarded with it.
    RUN_SCOPED = "run_scoped"
    # The backend keeps its own memory across runs. Declarable (so the
    # catalogue can describe e.g. a Letta-style runtime honestly) but never
    # runnable in this phase: nothing may persist outside JARVIS.
    EXTERNAL_PERSISTENT = "external_persistent"


class ExecutionMode(str, Enum):
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"


class IntegrationStatus(str, Enum):
    INTEGRATED = "integrated"        # the harness has a real adapter
    SCRIPTED = "scripted"            # the harness has a test double only
    NOT_INTEGRATED = "not_integrated"


class ComponentState(str, Enum):
    """The states the Workbench shows (docs/30 §6). A saved configuration is
    never shown as operational on the strength of being saved."""

    AVAILABLE = "available"
    CONFIGURED = "configured"
    CONNECTION_REQUIRED = "connection_required"
    UNAVAILABLE = "unavailable"
    EXPERIMENTAL_ONLY = "experimental_only"
    NOT_INTEGRATED = "not_integrated"
    NOT_ACTIVE_IN_PRODUCTION = "not_active_in_production"
    VALIDATION_FAILED = "validation_failed"


HARNESS_ADAPTERS: dict[str, IntegrationStatus] = {
    "deterministic_templates": IntegrationStatus.INTEGRATED,
    "model_planner": IntegrationStatus.INTEGRATED,
    "scripted_agentic_runtime": IntegrationStatus.SCRIPTED,
}

# Which delegation modes each kind can have. Anything else is a catalogue error.
KIND_DELEGATION: dict[BackendKind, frozenset[DelegationMode]] = {
    BackendKind.PLANNER: frozenset({DelegationMode.PLAN_ONLY}),
    BackendKind.AGENTIC_RUNTIME: frozenset({DelegationMode.JARVIS_MEDIATED}),
    BackendKind.WORKFLOW_ENGINE: frozenset({DelegationMode.FIXED_TEMPLATE}),
}

OUTPUT_SCHEMAS: frozenset[str] = frozenset({"research.v1", "assessment.v1", "planning.v1", "generic.v1"})

CENTRAL_PROMPT_PROFILES: dict[str, frozenset[str]] = {
    # E0: the production runtime's own prompt, unchanged.
    "central.baseline": frozenset({"1"}),
    # E1: the Central prompt contract (intent, objective, constraints,
    # clarification, evidence, synthesis, honest uncertainty).
    "central.contract": frozenset({"1"}),
}


class _Doc(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContextPolicy(_Doc):
    """What a component may see. `evidence_subset` means: only the evidence
    items a work package names, by stable id — never the whole task context,
    never memory. Specialists cannot retrieve anything themselves."""

    mode: Literal["none", "task_only", "evidence_subset"] = "evidence_subset"
    max_chars: int = Field(default=6000, ge=0, le=64000)


class CentralAssignment(_Doc):
    catalog_id: CatalogId
    prompt_profile: Slug = "central.contract"
    prompt_version: VersionLabel = "1"


class OrchestrationAssignment(_Doc):
    backend_id: Slug
    # Required exactly when the backend needs a model (a planner does).
    model_catalog_id: CatalogId | None = None
    timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    max_fan_out: int = Field(default=3, ge=1, le=16)
    # Specialists never spawn specialists in this phase.
    max_depth: Literal[1] = 1
    budget: float = Field(default=0.0, ge=0)


class SpecialistAssignment(_Doc):
    specialization: SpecializationId
    enabled: bool = True
    catalog_id: CatalogId
    prompt_profile: Slug
    prompt_version: VersionLabel = "1"
    output_schema: Slug
    context_policy: ContextPolicy = Field(default_factory=ContextPolicy)
    timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    max_output_tokens: int = Field(default=800, ge=16, le=8192)
    budget: float = Field(default=0.0, ge=0)


class ExecutionConstraints(_Doc):
    mode: ExecutionMode = ExecutionMode.SEQUENTIAL
    per_call_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    overall_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    per_call_budget: float = Field(default=0.0, ge=0)
    overall_budget: float = Field(default=0.0, ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> "ExecutionConstraints":
        if self.per_call_timeout_seconds > self.overall_timeout_seconds:
            raise ValueError("per_call_timeout_seconds cannot exceed overall_timeout_seconds")
        if self.per_call_budget > self.overall_budget:
            raise ValueError("per_call_budget cannot exceed overall_budget")
        return self


class EvaluationSettings(_Doc):
    suite_id: Slug = "synthetic.v1"
    rubric_version: VersionLabel = "1"
    repetitions: int = Field(default=1, ge=1, le=5)


class IntelligenceProfileDocument(_Doc):
    """One immutable version's content. Identity is never in it: the owner is
    the authenticated caller who saved it."""

    schema_version: Literal[1] = PROFILE_SCHEMA_VERSION
    central: CentralAssignment
    orchestration: OrchestrationAssignment
    specialists: tuple[SpecialistAssignment, ...] = Field(default=(), max_length=16)
    execution: ExecutionConstraints = Field(default_factory=ExecutionConstraints)
    context_policy: ContextPolicy = Field(default_factory=lambda: ContextPolicy(mode="task_only"))
    evaluation: EvaluationSettings = Field(default_factory=EvaluationSettings)

    @model_validator(mode="after")
    def _unique_specializations(self) -> "IntelligenceProfileDocument":
        seen = [s.specialization for s in self.specialists]
        if len(seen) != len(set(seen)):
            raise ValueError("each specialization may be assigned at most once per profile")
        return self


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def profile_hash(document: IntelligenceProfileDocument) -> str:
    """The configuration hash an experiment run records. Over the normalized
    document (defaults filled), so two spellings of one configuration hash
    alike and any change to what runs changes the hash."""

    return hashlib.sha256(canonical_json(document.model_dump(mode="json")).encode()).hexdigest()


class ResolvedModel(_Doc):
    """A catalogue entry as the harness needs it. No endpoint, no secret."""

    catalog_id: CatalogId
    provider: str
    model: str
    local: bool
    input_per_1k_tokens: float = Field(ge=0)
    output_per_1k_tokens: float = Field(ge=0)
    timeout_seconds: float = Field(gt=0)


class ResolvedBackend(_Doc):
    backend_id: Slug
    kind: BackendKind
    adapter: Slug
    integration: IntegrationStatus
    delegation_mode: DelegationMode
    state_model: StateModel
    supports_parallel: bool
    max_fan_out: int = Field(ge=1)
    max_timeout_seconds: float = Field(gt=0)
    config_version: VersionLabel


class ExperimentSnapshot(_Doc):
    snapshot_version: Literal[1] = SNAPSHOT_VERSION
    profile_id: str
    profile_version: int = Field(ge=1)
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    document: IntelligenceProfileDocument
    models: dict[str, ResolvedModel]
    backend: ResolvedBackend
    exported_at: datetime
    # Always false in this phase: nothing in production reads a profile.
    production_active: Literal[False] = False

    @model_validator(mode="after")
    def _hash_matches(self) -> "ExperimentSnapshot":
        if profile_hash(self.document) != self.config_hash:
            raise ValueError("config_hash does not match the document")
        return self


__all__ = [
    "BackendKind", "CENTRAL_PROMPT_PROFILES", "CatalogId", "CentralAssignment", "ComponentState",
    "ContextPolicy", "DelegationMode", "EvaluationSettings", "ExecutionConstraints", "ExecutionMode",
    "ExperimentSnapshot", "HARNESS_ADAPTERS", "IntegrationStatus", "IntelligenceProfileDocument",
    "KIND_DELEGATION", "OUTPUT_SCHEMAS", "OrchestrationAssignment", "ResolvedBackend", "ResolvedModel",
    "SpecialistAssignment", "SpecializationId", "StateModel", "canonical_json", "profile_hash",
]
