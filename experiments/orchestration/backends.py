"""The backends the harness implements (docs/31 §3). Their adapter names are
`shared.schemas.intelligence.HARNESS_ADAPTERS` — a test holds the two equal,
so the Workbench never reports an integration that does not exist."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import ValidationError

from shared.schemas.intelligence import BackendKind, DelegationMode, ExperimentSnapshot, IntegrationStatus, StateModel

from experiments.accounting import BudgetRefused, MeteredModel
from experiments.contracts import Plan, PlanNode
from experiments.orchestration.ports import (
    BackendDescriptor,
    DelegateProposal,
    FinishProposal,
    ModelCallProposal,
    OrchestrationRequest,
    PlanOutcome,
    RuntimeObservation,
    RuntimeProposal,
)
from experiments.parsing import Unparseable, extract_object
from experiments.prompts import planner_messages
from experiments.contracts import EvidenceItem


def _same_adapter(snapshot: ExperimentSnapshot, descriptor: BackendDescriptor) -> list[str]:
    issues = []
    if snapshot.backend.adapter != descriptor.adapter:
        issues.append("adapter_mismatch")
    if snapshot.backend.kind is not descriptor.kind:
        issues.append("kind_mismatch")
    if snapshot.backend.state_model is StateModel.EXTERNAL_PERSISTENT:
        issues.append("external_state_not_permitted")
    return issues


class DeterministicTemplates:
    """Workflow engine: the plan is the task's hand-authored decomposition."""

    descriptor = BackendDescriptor(adapter="deterministic_templates", kind=BackendKind.WORKFLOW_ENGINE,
                                   delegation_mode=DelegationMode.FIXED_TEMPLATE, state_model=StateModel.STATELESS,
                                   supports_parallel=True, supports_cancel=True, uses_model=False)

    def validate_config(self, snapshot: ExperimentSnapshot) -> list[str]:
        return _same_adapter(snapshot, self.descriptor)

    async def available(self) -> bool:
        return True

    async def plan(self, request: OrchestrationRequest) -> PlanOutcome:
        if not request.template:
            return PlanOutcome(None, "no_template")
        return PlanOutcome(Plan(task_id=request.task_id, nodes=request.template, rationale="hand-authored template"))


class ModelPlanner:
    """Planner: one metered model call returns a JSON plan; JARVIS validates it."""

    descriptor = BackendDescriptor(adapter="model_planner", kind=BackendKind.PLANNER,
                                   delegation_mode=DelegationMode.PLAN_ONLY, state_model=StateModel.STATELESS,
                                   supports_parallel=True, supports_cancel=True, uses_model=True)

    def __init__(self, model: MeteredModel, *, max_tokens: int = 600) -> None:
        self.model = model
        self.max_tokens = max_tokens

    def validate_config(self, snapshot: ExperimentSnapshot) -> list[str]:
        issues = _same_adapter(snapshot, self.descriptor)
        if snapshot.document.orchestration.model_catalog_id is None:
            issues.append("model_required")
        return issues

    async def available(self) -> bool:
        return await self.model.provider.health()

    async def plan(self, request: OrchestrationRequest) -> PlanOutcome:
        evidence = [EvidenceItem(id=e.id, title=e.title, content="") for e in request.evidence]
        messages = planner_messages(request.prompt, evidence, specializations=request.allowed_specializations,
                                    max_nodes=request.max_fan_out)
        try:
            result = await self.model.call(messages, task_id=request.task_id, role="planner",
                                           max_tokens=self.max_tokens, timeout=request.deadline_seconds)
        except BudgetRefused:
            return PlanOutcome(None, "budget_refused")
        except Exception as exc:  # noqa: BLE001 — timeout / unavailable: no plan
            return PlanOutcome(None, type(exc).__name__)
        try:
            raw = extract_object(result.content)
            nodes = tuple(PlanNode.model_validate(n) for n in raw.get("nodes", []))
            rationale = raw.get("rationale")
            return PlanOutcome(Plan(task_id=request.task_id, nodes=nodes,
                                    rationale=str(rationale)[:2000] if rationale else None))
        except (Unparseable, ValidationError, TypeError, AttributeError):
            return PlanOutcome(None, "unparseable_plan")


@dataclass
class ScriptedAgenticRuntime:
    """A test double of an agentic runtime: it replays scripted proposals and
    records what it observed. Not an integration of any product."""

    script: list[RuntimeProposal]
    observed: list[RuntimeObservation] = field(default_factory=list)
    cancelled: bool = False

    descriptor = BackendDescriptor(adapter="scripted_agentic_runtime", kind=BackendKind.AGENTIC_RUNTIME,
                                   delegation_mode=DelegationMode.JARVIS_MEDIATED, state_model=StateModel.RUN_SCOPED,
                                   supports_parallel=True, supports_cancel=True, uses_model=True)

    def validate_config(self, snapshot: ExperimentSnapshot) -> list[str]:
        return _same_adapter(snapshot, self.descriptor)

    async def available(self) -> bool:
        return True

    async def start(self, request: OrchestrationRequest) -> "ScriptedAgenticRuntime":
        self.request = request
        self._script = list(self.script)
        return self

    async def next(self, observation: RuntimeObservation) -> RuntimeProposal:
        self.observed.append(observation)
        return self._script.pop(0) if self._script else FinishProposal(summary="script exhausted")

    async def cancel(self) -> None:
        self.cancelled = True


IMPLEMENTED: dict[str, IntegrationStatus] = {
    DeterministicTemplates.descriptor.adapter: IntegrationStatus.INTEGRATED,
    ModelPlanner.descriptor.adapter: IntegrationStatus.INTEGRATED,
    ScriptedAgenticRuntime.descriptor.adapter: IntegrationStatus.SCRIPTED,
}

__all__ = ["DeterministicTemplates", "IMPLEMENTED", "ModelPlanner", "ScriptedAgenticRuntime",
           "DelegateProposal", "FinishProposal", "ModelCallProposal"]
