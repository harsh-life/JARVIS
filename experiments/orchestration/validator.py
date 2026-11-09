"""The JARVIS-owned deterministic plan validator (docs/31 §3).

Whatever produced a plan — a template, a model, an external runtime — it runs
only after this says yes, and only as the work packages this builds. The
backend cannot widen any of it:

* the backend must be the snapshot's registered backend (checked by the runner);
* every node's specialization must be an *enabled* assignment in the snapshot;
* at most `max_fan_out` nodes (cumulatively, across an agentic runtime's turns);
* unique subtask ids, dependencies that exist, no cycles, a bounded chain;
* evidence references must be a subset of the task's supplied evidence, and a
  specialist whose policy is `none` gets no evidence at all;
* budget: the plan's declared specialist budgets must fit what is left;
* timeouts and output caps come from the assignment, never from the plan.

Recursion is impossible by construction: specialists return bodies, and no
body type has anything that could be read as a plan.
"""

from __future__ import annotations

from dataclasses import dataclass

from shared.schemas.intelligence import ExperimentSnapshot, SpecialistAssignment

from experiments.contracts import Budget, Plan, PlanNode, WorkPackage


class PlanRejected(Exception):
    def __init__(self, issues: list[str]) -> None:
        super().__init__(", ".join(issues))
        self.issues = issues


@dataclass(frozen=True)
class PlanLimits:
    task_id: str
    evidence_ids: frozenset[str]
    assignments: dict[str, SpecialistAssignment]
    max_fan_out: int
    per_call_timeout: float
    remaining_budget: float
    max_dependency_depth: int = 3
    already_dispatched: frozenset[str] = frozenset()   # for an agentic runtime's later turns

    @classmethod
    def from_snapshot(cls, snapshot: ExperimentSnapshot, *, task_id: str, evidence_ids: set[str],
                      remaining_budget: float) -> "PlanLimits":
        doc = snapshot.document
        return cls(
            task_id=task_id, evidence_ids=frozenset(evidence_ids),
            assignments={s.specialization: s for s in doc.specialists if s.enabled},
            max_fan_out=min(doc.orchestration.max_fan_out, snapshot.backend.max_fan_out),
            per_call_timeout=doc.execution.per_call_timeout_seconds, remaining_budget=remaining_budget,
        )


def validate_plan(plan: Plan, limits: PlanLimits) -> list[WorkPackage]:
    """Return the work packages in a dependency-respecting order, or raise
    `PlanRejected` with every reason (codes, stable for scoring)."""

    issues: list[str] = []
    nodes = list(plan.nodes)
    if plan.task_id != limits.task_id:
        issues.append("wrong_task")
    if not nodes:
        issues.append("empty_plan")
    if len(nodes) + len(limits.already_dispatched) > limits.max_fan_out:
        issues.append("fan_out_exceeded")
    ids = [n.subtask_id for n in nodes]
    if len(ids) != len(set(ids)) or set(ids) & limits.already_dispatched:
        issues.append("duplicate_subtask")
    known = set(ids) | set(limits.already_dispatched)
    for node in nodes:
        if node.specialization not in limits.assignments:
            issues.append(f"specialization_not_assigned:{node.specialization}")
        if set(node.evidence_refs) - limits.evidence_ids:
            issues.append(f"evidence_not_supplied:{node.subtask_id}")
        assignment = limits.assignments.get(node.specialization)
        if assignment is not None and assignment.context_policy.mode == "none" and node.evidence_refs:
            issues.append(f"context_not_permitted:{node.subtask_id}")
        for dep in node.depends_on:
            if dep == node.subtask_id or dep not in known:
                issues.append(f"bad_dependency:{node.subtask_id}")
    order = _topological(nodes, limits.already_dispatched)
    if order is None:
        issues.append("cycle")
    elif _depth(nodes) > limits.max_dependency_depth:
        issues.append("too_deep")
    declared = sum(limits.assignments[n.specialization].budget for n in nodes if n.specialization in limits.assignments)
    if declared > limits.remaining_budget + 1e-12:
        issues.append("budget_insufficient")
    if issues:
        raise PlanRejected(sorted(set(issues)))
    packages = []
    for node in order or []:
        a = limits.assignments[node.specialization]
        packages.append(WorkPackage(
            task_id=limits.task_id, subtask_id=node.subtask_id, specialization=node.specialization,
            instruction=node.instruction, evidence_refs=node.evidence_refs, context_policy=a.context_policy,
            output_schema=a.output_schema, depends_on=node.depends_on,
            budget=Budget(max_cost=a.budget, max_output_tokens=a.max_output_tokens),
            deadline_seconds=min(a.timeout_seconds, limits.per_call_timeout),
        ))
    return packages


def _topological(nodes: list[PlanNode], done: frozenset[str]) -> list[PlanNode] | None:
    remaining = {n.subtask_id: n for n in nodes}
    resolved = set(done)
    order: list[PlanNode] = []
    while remaining:
        ready = [n for n in remaining.values() if set(n.depends_on) <= resolved]
        if not ready:
            return None
        for n in sorted(ready, key=lambda x: x.subtask_id):
            order.append(n)
            resolved.add(n.subtask_id)
            del remaining[n.subtask_id]
    return order


def _depth(nodes: list[PlanNode]) -> int:
    by_id = {n.subtask_id: n for n in nodes}
    memo: dict[str, int] = {}

    def depth(node_id: str) -> int:
        if node_id not in memo:
            node = by_id.get(node_id)
            memo[node_id] = 1 + max((depth(d) for d in (node.depends_on if node else ()) if d in by_id), default=0)
        return memo[node_id]

    return max((depth(i) for i in by_id), default=0)


__all__ = ["PlanLimits", "PlanRejected", "validate_plan"]
