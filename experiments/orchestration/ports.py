"""What an orchestration backend must provide (docs/31 §3).

Two different contracts, because they are different components:

* `PlannerBackend` — returns a `Plan` and nothing else. A deterministic
  workflow engine (fixed templates) and a model-based planner both fit here;
  so would a classifier-style decision model. It never dispatches, and it is
  never asked to behave like an agent.
* `AgenticRuntimeBackend` — a multi-step coordinator. It is driven by JARVIS
  one turn at a time: each turn it may *propose* delegations (plan nodes) or a
  model call for its own reasoning, or finish. JARVIS validates every
  proposal, reserves budget, makes every call itself, and hands back only the
  results. The runtime is never given tools, credentials, SecretStore values,
  database access, memory or evidence content beyond titles — and it is not a
  text-generation endpoint either: `finish` carries a summary, not the answer
  (Central writes the answer).

Both report what they are (`BackendDescriptor`), can say whether they are
available, validate the snapshot they are given, and support cancellation.
Usage is never self-reported as one lump: every model call either backend
needs goes through JARVIS's metered models and is recorded individually. An
external runtime that would make model calls on its own, or keep memory of
its own across runs, does not fit this contract in this phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, Sequence, Union

from shared.schemas.intelligence import BackendKind, DelegationMode, ExperimentSnapshot, StateModel

from experiments.contracts import Plan, PlanNode, ResultBundle


@dataclass(frozen=True)
class BackendDescriptor:
    adapter: str
    kind: BackendKind
    delegation_mode: DelegationMode
    state_model: StateModel
    supports_parallel: bool
    supports_cancel: bool
    uses_model: bool


@dataclass(frozen=True)
class EvidenceRef:
    id: str
    title: str


@dataclass(frozen=True)
class OrchestrationRequest:
    task_id: str
    prompt: str
    # Ids and titles only: a backend plans over evidence, it does not read it.
    evidence: tuple[EvidenceRef, ...]
    allowed_specializations: tuple[str, ...]
    max_fan_out: int
    deadline_seconds: float
    config_hash: str
    # Hand-authored decomposition, for template backends (E2/E3). Empty when
    # the backend is expected to plan.
    template: tuple[PlanNode, ...] = ()


@dataclass(frozen=True)
class PlanOutcome:
    plan: Plan | None
    error: str | None = None


class PlannerBackend(Protocol):
    descriptor: BackendDescriptor

    def validate_config(self, snapshot: ExperimentSnapshot) -> list[str]: ...

    async def available(self) -> bool: ...

    async def plan(self, request: OrchestrationRequest) -> PlanOutcome: ...


# ── the agentic runtime's proposals ────────────────────────────────────────

@dataclass(frozen=True)
class DelegateProposal:
    nodes: tuple[PlanNode, ...]
    type: Literal["delegate"] = "delegate"


@dataclass(frozen=True)
class ModelCallProposal:
    """The runtime wants its own reasoning step. JARVIS makes the call on the
    orchestration model, under the budget, and returns the text."""

    prompt: str
    max_tokens: int = 400
    type: Literal["model_call"] = "model_call"


@dataclass(frozen=True)
class FinishProposal:
    summary: str
    type: Literal["finish"] = "finish"


RuntimeProposal = Union[DelegateProposal, ModelCallProposal, FinishProposal]


@dataclass
class RuntimeObservation:
    results: Sequence[ResultBundle] = ()
    model_text: str | None = None
    refused: list[str] = field(default_factory=list)   # why a proposal was refused, as codes


class RuntimeSession(Protocol):
    async def next(self, observation: RuntimeObservation) -> RuntimeProposal: ...

    async def cancel(self) -> None: ...


class AgenticRuntimeBackend(Protocol):
    descriptor: BackendDescriptor

    def validate_config(self, snapshot: ExperimentSnapshot) -> list[str]: ...

    async def available(self) -> bool: ...

    async def start(self, request: OrchestrationRequest) -> RuntimeSession: ...


__all__ = [
    "AgenticRuntimeBackend", "BackendDescriptor", "DelegateProposal", "EvidenceRef", "FinishProposal",
    "ModelCallProposal", "OrchestrationRequest", "PlanOutcome", "PlannerBackend", "RuntimeObservation",
    "RuntimeProposal", "RuntimeSession",
]
