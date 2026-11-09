"""docs/30 — checking a user's intelligence profile against the operator's
catalogue, and describing it honestly.

Pure functions over `AppConfig` and the shared contract: no storage, no
network, no secrets. Used by the gateway's `/intelligence/*` routes; nothing in
the production request lifecycle calls them (no profile is active in
production in this phase).

The model catalogue is one catalogue, not two: M2's entries (`agent.primary`,
`agent.fallback` — the only ones a user may pick as their *production* model,
through `/config/model`) plus the operator's experiment-only entries
(`workbench.<id>`). Endpoints and keys stay in the configuration; a view never
contains them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from server.config.schema import (
    LOCAL_MODEL_PROVIDERS,
    AppConfig,
    ModelEntryConfig,
    OrchestrationBackendConfig,
    SpecializationConfig,
    WorkbenchConfig,
)
from shared.schemas.intelligence import (
    CENTRAL_PROMPT_PROFILES,
    HARNESS_ADAPTERS,
    ComponentState,
    ExecutionMode,
    ExperimentSnapshot,
    IntegrationStatus,
    IntelligenceProfileDocument,
    ResolvedBackend,
    ResolvedModel,
    StateModel,
    profile_hash,
)


@dataclass(frozen=True)
class CatalogModel:
    catalog_id: str
    entry: ModelEntryConfig
    display_name: str
    # Selectable as the user's production model through M2's `/config/model`.
    production_selectable: bool

    @property
    def local(self) -> bool:
        return self.entry.provider in LOCAL_MODEL_PROVIDERS


@dataclass(frozen=True)
class Issue:
    path: str
    code: str
    message: str
    severity: str = "error"  # "error" refuses a save; "warning" is shown

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message, "severity": self.severity}


def catalog_models(config: AppConfig) -> dict[str, CatalogModel]:
    entries: dict[str, CatalogModel] = {
        "agent.primary": CatalogModel("agent.primary", config.agent, "Server primary model", True),
    }
    if config.agent.fallback is not None:
        entries["agent.fallback"] = CatalogModel("agent.fallback", config.agent.fallback, "Server fallback model", True)
    for model in config.workbench.models:
        entries[f"workbench.{model.id}"] = CatalogModel(
            f"workbench.{model.id}", model, model.display_name or model.id, False
        )
    return entries


def integration_of(backend: OrchestrationBackendConfig) -> IntegrationStatus:
    """Decided by code, not by the catalogue: an entry is integrated only if
    the harness implements its adapter."""

    return HARNESS_ADAPTERS.get(backend.adapter, IntegrationStatus.NOT_INTEGRATED)


def offered_backends(workbench: WorkbenchConfig) -> dict[str, OrchestrationBackendConfig]:
    return {b.id: b for b in workbench.orchestration_backends if b.enabled}


def offered_specializations(workbench: WorkbenchConfig) -> dict[str, SpecializationConfig]:
    return {s.id: s for s in workbench.specializations if s.enabled}


def backend_state(backend: OrchestrationBackendConfig) -> ComponentState:
    integration = integration_of(backend)
    if integration is IntegrationStatus.NOT_INTEGRATED:
        return ComponentState.NOT_INTEGRATED
    if backend.state_model is StateModel.EXTERNAL_PERSISTENT:
        return ComponentState.UNAVAILABLE
    return ComponentState.EXPERIMENTAL_ONLY


def model_state(model: CatalogModel | None) -> ComponentState:
    if model is None:
        return ComponentState.UNAVAILABLE
    # A paid model needs a credential at run time. The harness takes it from
    # its own explicitly configured provider map, never from the SecretStore.
    return ComponentState.AVAILABLE if model.local else ComponentState.CONNECTION_REQUIRED


def validate_profile(document: IntelligenceProfileDocument, config: AppConfig) -> list[Issue]:
    """Every reason this document does not fit the operator's catalogue and
    ceilings, as stable codes. Schema-level problems were already refused
    when the document was parsed."""

    workbench = config.workbench
    limits = workbench.limits
    models = catalog_models(config)
    backends = offered_backends(workbench)
    specs = offered_specializations(workbench)
    issues: list[Issue] = []

    def model_ref(path: str, catalog_id: str | None) -> None:
        if catalog_id is not None and catalog_id not in models:
            issues.append(Issue(path, "unknown_model", f"{catalog_id!r} is not in this server's model catalogue"))

    central = document.central
    model_ref("central.catalog_id", central.catalog_id)
    versions = CENTRAL_PROMPT_PROFILES.get(central.prompt_profile)
    if versions is None:
        issues.append(Issue("central.prompt_profile", "unknown_prompt_profile",
                            f"known Central prompt profiles: {sorted(CENTRAL_PROMPT_PROFILES)}"))
    elif central.prompt_version not in versions:
        issues.append(Issue("central.prompt_version", "unknown_prompt_version",
                            f"{central.prompt_profile} has versions {sorted(versions)}"))

    orch = document.orchestration
    backend = backends.get(orch.backend_id)
    if backend is None:
        issues.append(Issue("orchestration.backend_id", "unknown_backend",
                            "that orchestration backend is not offered by this server"))
    else:
        if backend.requires_model and orch.model_catalog_id is None:
            issues.append(Issue("orchestration.model_catalog_id", "model_required",
                                f"{backend.display_name} needs a model"))
        if not backend.requires_model and orch.model_catalog_id is not None:
            issues.append(Issue("orchestration.model_catalog_id", "model_not_used",
                                f"{backend.display_name} uses no model"))
        model_ref("orchestration.model_catalog_id", orch.model_catalog_id)
        if (orch.model_catalog_id is not None and backend.allowed_models is not None
                and orch.model_catalog_id not in backend.allowed_models):
            issues.append(Issue("orchestration.model_catalog_id", "model_not_allowed",
                                f"{backend.display_name} may use only {sorted(backend.allowed_models)}"))
        fan_out_cap = min(backend.max_fan_out, limits.max_fan_out)
        if orch.max_fan_out > fan_out_cap:
            issues.append(Issue("orchestration.max_fan_out", "fan_out_exceeds_limit", f"at most {fan_out_cap}"))
        if orch.timeout_seconds > backend.max_timeout_seconds:
            issues.append(Issue("orchestration.timeout_seconds", "timeout_exceeds_limit",
                                f"at most {backend.max_timeout_seconds:g}s for this backend"))
        if orch.budget > backend.max_budget and orch.budget > 0:
            issues.append(Issue("orchestration.budget", "budget_exceeds_limit",
                                f"at most {backend.max_budget:g} for this backend"))
        if document.execution.mode is ExecutionMode.PARALLEL and not backend.supports_parallel:
            issues.append(Issue("execution.mode", "parallel_unsupported",
                                f"{backend.display_name} does not support parallel execution"))
        if backend.state_model is StateModel.EXTERNAL_PERSISTENT:
            issues.append(Issue("orchestration.backend_id", "external_state_not_permitted",
                                "a backend that keeps its own memory cannot run in this phase"))
        if integration_of(backend) is IntegrationStatus.NOT_INTEGRATED:
            issues.append(Issue("orchestration.backend_id", "not_integrated",
                                "saved for a future experiment: no adapter exists for this backend yet",
                                severity="warning"))

    enabled_specialists = [s for s in document.specialists if s.enabled]
    if len(enabled_specialists) > orch.max_fan_out:
        issues.append(Issue("specialists", "fan_out_exceeds_limit",
                            f"{len(enabled_specialists)} enabled specialists, but max_fan_out is {orch.max_fan_out}"))
    for i, assignment in enumerate(document.specialists):
        at = f"specialists[{i}]"
        spec = specs.get(assignment.specialization)
        if spec is None:
            issues.append(Issue(f"{at}.specialization", "unknown_specialization",
                                "that specialization is not offered by this server"))
            continue
        model_ref(f"{at}.catalog_id", assignment.catalog_id)
        if assignment.output_schema != spec.output_schema:
            issues.append(Issue(f"{at}.output_schema", "schema_mismatch",
                                f"{spec.display_name} produces {spec.output_schema}"))
        profile = next((p for p in spec.prompt_profiles if p.id == assignment.prompt_profile), None)
        if profile is None:
            issues.append(Issue(f"{at}.prompt_profile", "unknown_prompt_profile",
                                f"known: {[p.id for p in spec.prompt_profiles]}"))
        elif assignment.prompt_version not in profile.versions:
            issues.append(Issue(f"{at}.prompt_version", "unknown_prompt_version", f"known: {profile.versions}"))
        if assignment.timeout_seconds > min(limits.max_call_timeout_seconds,
                                            document.execution.per_call_timeout_seconds):
            issues.append(Issue(f"{at}.timeout_seconds", "timeout_exceeds_limit",
                                "a specialist call cannot outlast the per-call timeout"))
        if assignment.budget > document.execution.per_call_budget:
            issues.append(Issue(f"{at}.budget", "budget_exceeds_limit",
                                "a specialist's budget cannot exceed the per-call budget"))
        if assignment.context_policy.mode == "task_only":
            issues.append(Issue(f"{at}.context_policy.mode", "context_too_broad",
                                "a specialist sees only the evidence its work package names (evidence_subset or none)"))

    ex = document.execution
    if ex.per_call_timeout_seconds > limits.max_call_timeout_seconds:
        issues.append(Issue("execution.per_call_timeout_seconds", "timeout_exceeds_limit",
                            f"at most {limits.max_call_timeout_seconds:g}s"))
    if ex.overall_timeout_seconds > limits.max_overall_timeout_seconds:
        issues.append(Issue("execution.overall_timeout_seconds", "timeout_exceeds_limit",
                            f"at most {limits.max_overall_timeout_seconds:g}s"))
    if ex.per_call_budget > limits.max_per_call_budget:
        issues.append(Issue("execution.per_call_budget", "budget_exceeds_limit",
                            f"at most {limits.max_per_call_budget:g}"))
    if ex.overall_budget > limits.max_overall_budget:
        issues.append(Issue("execution.overall_budget", "budget_exceeds_limit",
                            f"at most {limits.max_overall_budget:g}"))
    declared = orch.budget + sum(s.budget for s in enabled_specialists)
    if declared > ex.overall_budget + 1e-12:
        issues.append(Issue("execution.overall_budget", "budget_not_reserved",
                            f"orchestrator and specialist budgets add up to {declared:g}, above the overall budget"))

    paid = [m for m in _referenced_models(document) if m in models and not models[m].local]
    if paid and ex.overall_budget == 0:
        issues.append(Issue("execution.overall_budget", "paid_model_without_budget",
                            "a paid model is selected but the overall budget is 0, so every paid call will be refused",
                            severity="warning"))
    return issues


def _referenced_models(document: IntelligenceProfileDocument) -> list[str]:
    refs = [document.central.catalog_id]
    if document.orchestration.model_catalog_id:
        refs.append(document.orchestration.model_catalog_id)
    refs += [s.catalog_id for s in document.specialists if s.enabled]
    return refs


def profile_status(document: IntelligenceProfileDocument, config: AppConfig,
                   issues: list[Issue]) -> dict[str, object]:
    """What the Workbench shows next to a version. `runnable_in_harness` is
    true only if every part has an implemented adapter and validation passes;
    `production_active` is always false in this phase."""

    models = catalog_models(config)
    backends = offered_backends(config.workbench)
    errors = [i for i in issues if i.severity == "error"]
    backend = backends.get(document.orchestration.backend_id)
    orch_state = backend_state(backend) if backend is not None else ComponentState.UNAVAILABLE
    return {
        "production_active": False,
        "overall": (ComponentState.VALIDATION_FAILED if errors else ComponentState.EXPERIMENTAL_ONLY).value,
        "central": model_state(models.get(document.central.catalog_id)).value,
        "orchestration": orch_state.value,
        "specialists": {
            s.specialization: (model_state(models.get(s.catalog_id)).value if s.enabled else "disabled")
            for s in document.specialists
        },
        "runnable_in_harness": not errors and orch_state is ComponentState.EXPERIMENTAL_ONLY,
    }


def build_snapshot(*, profile_id: str, version: int, document: IntelligenceProfileDocument,
                   config: AppConfig) -> ExperimentSnapshot:
    """Resolve the catalogue entries a valid version names. Called only after
    `validate_profile` reported no errors against the *current* catalogue."""

    models = catalog_models(config)
    backend = offered_backends(config.workbench)[document.orchestration.backend_id]
    resolved: dict[str, ResolvedModel] = {}
    for ref in _referenced_models(document):
        model = models[ref]
        pricing = model.entry.pricing
        resolved[ref] = ResolvedModel(
            catalog_id=ref, provider=model.entry.provider, model=model.entry.model, local=model.local,
            input_per_1k_tokens=pricing.input_per_1k_tokens if pricing else 0.0,
            output_per_1k_tokens=pricing.output_per_1k_tokens if pricing else 0.0,
            timeout_seconds=model.entry.timeout_seconds,
        )
    return ExperimentSnapshot(
        profile_id=profile_id, profile_version=version, config_hash=profile_hash(document), document=document,
        models=resolved,
        backend=ResolvedBackend(
            backend_id=backend.id, kind=backend.kind, adapter=backend.adapter, integration=integration_of(backend),
            delegation_mode=backend.delegation_mode, state_model=backend.state_model,
            supports_parallel=backend.supports_parallel, max_fan_out=backend.max_fan_out,
            max_timeout_seconds=backend.max_timeout_seconds, config_version=backend.config_version,
        ),
        exported_at=datetime.now(timezone.utc),
    )


__all__ = [
    "CatalogModel", "Issue", "backend_state", "build_snapshot", "catalog_models", "integration_of",
    "model_state", "offered_backends", "offered_specializations", "profile_status", "validate_profile",
]
