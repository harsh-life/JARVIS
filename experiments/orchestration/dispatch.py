"""Dispatching validated work packages, and driving an agentic runtime
(docs/31 §3). JARVIS makes every call; a backend only proposes.

* Sequential or parallel, by dependency level. A package whose dependency did
  not produce a usable result is `skipped`, not run on missing input.
* Parallel calls each reserve budget *before* their provider call (inside
  `MeteredModel.call`, under the ledger's lock), so concurrent dispatch cannot
  overspend.
* An overall deadline cancels whatever is still running; cancelled packages
  are reported `cancelled`, never silently dropped.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable

from shared.schemas.intelligence import ExecutionMode, SpecialistAssignment

from experiments.accounting import BudgetRefused, MeteredModel
from experiments.contracts import CompletionStatus, ErrorInfo, EvidenceItem, Plan, ResultBundle, WorkPackage
from experiments.orchestration.ports import (
    DelegateProposal,
    FinishProposal,
    ModelCallProposal,
    OrchestrationRequest,
    RuntimeObservation,
    RuntimeSession,
)
from experiments.orchestration.validator import PlanLimits, PlanRejected, validate_plan
from experiments.specialists import run_specialist
from server.models.provider import ChatMessage

USABLE = {CompletionStatus.COMPLETED, CompletionStatus.PARTIAL}
ModelFor = Callable[[SpecialistAssignment], MeteredModel]


@dataclass
class Dispatcher:
    assignments: dict[str, SpecialistAssignment]
    model_for: ModelFor
    evidence: dict[str, EvidenceItem]
    config_hash: str
    mode: ExecutionMode

    def _bundle(self, package: WorkPackage, status: CompletionStatus, code: str) -> ResultBundle:
        return ResultBundle(task_id=package.task_id, subtask_id=package.subtask_id,
                            specialization=package.specialization,
                            assignment_id=f"{package.specialization}@{self.assignments[package.specialization].catalog_id}",
                            config_hash=self.config_hash, status=status, error=ErrorInfo(code=code, message=code))

    async def _one(self, package: WorkPackage) -> ResultBundle:
        assignment = self.assignments[package.specialization]
        return await run_specialist(package, assignment_id=f"{package.specialization}@{assignment.catalog_id}",
                                    config_hash=self.config_hash, evidence=self.evidence,
                                    model=self.model_for(assignment))

    async def run(self, packages: list[WorkPackage], *, deadline: float,
                  prior: dict[str, ResultBundle] | None = None) -> list[ResultBundle]:
        results: dict[str, ResultBundle] = dict(prior or {})
        todo = {p.subtask_id: p for p in packages}
        started = time.perf_counter()
        try:
            while todo:
                progressed = False
                for sid, p in list(todo.items()):
                    deps = [results.get(d) for d in p.depends_on]
                    if any(d is not None and d.status not in USABLE for d in deps):
                        results[sid] = self._bundle(p, CompletionStatus.SKIPPED, "dependency_failed")
                        del todo[sid]
                        progressed = True
                ready = [p for p in todo.values() if all(d in results for d in p.depends_on)]
                if not ready:
                    if progressed:
                        continue
                    break   # unreachable for a validated (acyclic) plan
                batch = ready if self.mode is ExecutionMode.PARALLEL else ready[:1]
                remaining = deadline - (time.perf_counter() - started)
                if remaining <= 0:
                    raise asyncio.TimeoutError
                done = await asyncio.wait_for(asyncio.gather(*(self._one(p) for p in batch)), timeout=remaining)
                for bundle in done:
                    results[bundle.subtask_id] = bundle
                    del todo[bundle.subtask_id]
        except asyncio.TimeoutError:
            for p in packages:
                if p.subtask_id not in results:
                    results[p.subtask_id] = self._bundle(p, CompletionStatus.CANCELLED, "overall_deadline")
        return [results[p.subtask_id] for p in packages]


@dataclass
class AgenticOutcome:
    bundles: list[ResultBundle] = field(default_factory=list)
    summary: str | None = None
    refused: list[str] = field(default_factory=list)
    turns: int = 0
    cancelled: bool = False


async def run_agentic(session: RuntimeSession, request: OrchestrationRequest, *, limits: PlanLimits,
                      dispatcher: Dispatcher, orchestration_model: MeteredModel | None, max_turns: int = 6,
                      deadline: float = 60.0) -> AgenticOutcome:
    """Drive a JARVIS-mediated runtime turn by turn. Every delegation goes
    through the validator (fan-out counted across turns), every model call
    is metered on the orchestration model, and the runtime sees only results."""

    outcome = AgenticOutcome()
    results: dict[str, ResultBundle] = {}
    observation = RuntimeObservation()
    started = time.perf_counter()
    while outcome.turns < max_turns:
        if time.perf_counter() - started > deadline:
            break
        outcome.turns += 1
        proposal = await session.next(observation)
        observation = RuntimeObservation()
        if isinstance(proposal, FinishProposal):
            outcome.summary = proposal.summary[:2000]
            break
        if isinstance(proposal, ModelCallProposal):
            if orchestration_model is None:
                observation.refused.append("no_orchestration_model")
                outcome.refused.append("no_orchestration_model")
                continue
            try:
                result = await orchestration_model.call(
                    [ChatMessage("user", proposal.prompt[:8000])], task_id=request.task_id, role="orchestrator",
                    max_tokens=min(proposal.max_tokens, 1024), timeout=request.deadline_seconds)
                observation.model_text = result.content
            except BudgetRefused:
                observation.refused.append("budget_refused")
                outcome.refused.append("budget_refused")
            except Exception as exc:  # noqa: BLE001
                observation.refused.append(type(exc).__name__)
                outcome.refused.append(type(exc).__name__)
            continue
        if isinstance(proposal, DelegateProposal):
            turn_limits = PlanLimits(**{**limits.__dict__, "already_dispatched": frozenset(results)})
            try:
                packages = validate_plan(Plan(task_id=request.task_id, nodes=proposal.nodes), turn_limits)
            except PlanRejected as exc:
                observation.refused.extend(exc.issues)
                outcome.refused.extend(exc.issues)
                continue
            remaining = max(0.1, deadline - (time.perf_counter() - started))
            bundles = await dispatcher.run(packages, deadline=remaining, prior=results)
            for b in bundles:
                results[b.subtask_id] = b
            observation.results = bundles
            continue
        observation.refused.append("unknown_proposal")
        outcome.refused.append("unknown_proposal")
    else:
        await session.cancel()
        outcome.cancelled = True
    if outcome.summary is None and not outcome.cancelled and time.perf_counter() - started > deadline:
        await session.cancel()
        outcome.cancelled = True
    outcome.bundles = list(results.values())
    return outcome


__all__ = ["AgenticOutcome", "Dispatcher", "run_agentic"]
