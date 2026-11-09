"""Running one work package against one specialist assignment (E3/E4).

The specialist sees only the evidence its package names (`evidence_subset`) or
none at all, and only up to its policy's character limit. It makes exactly one
metered call (plus at most one re-ask for unparseable output, also metered),
cannot call tools, cannot read or write memory, and cannot create subtasks:
nothing in its output is read as a plan. Its output is validated against the
package's schema; evidence references that do not resolve to supplied ids are
dropped and counted, URLs are kept only as `unverified_urls`.
"""

from __future__ import annotations

import asyncio
import time

from pydantic import ValidationError

from server.models.provider import ChatMessage, ModelUnavailable

from experiments.accounting import BudgetRefused, MeteredModel
from experiments.contracts import (
    BODY_MODELS,
    URL_PATTERN,
    CompletionStatus,
    ErrorInfo,
    EvidenceItem,
    ResultBundle,
    Usage,
    WorkPackage,
    body_evidence_refs,
)
from experiments.parsing import Unparseable, extract_object
from experiments.prompts import specialist_messages


def permitted_evidence(package: WorkPackage, evidence: dict[str, EvidenceItem]) -> list[EvidenceItem]:
    if package.context_policy.mode == "none":
        return []
    # The validator already refused a package naming unknown ids; this is the
    # second, local guard: only named, supplied items, never the rest.
    return [evidence[i] for i in package.evidence_refs if i in evidence]


async def run_specialist(package: WorkPackage, *, assignment_id: str, config_hash: str,
                         evidence: dict[str, EvidenceItem], model: MeteredModel) -> ResultBundle:
    items = permitted_evidence(package, evidence)
    supplied = {e.id for e in items}
    messages = specialist_messages(package, items)
    base = dict(task_id=package.task_id, subtask_id=package.subtask_id, specialization=package.specialization,
                assignment_id=assignment_id, config_hash=config_hash)
    started = time.perf_counter()
    role = f"specialist:{package.specialization}:{package.subtask_id}"

    def usage() -> Usage:
        # Only this package's own calls, even when siblings run in parallel.
        records = [r for r in model.ledger.for_task(package.task_id) if r.role == role]
        return Usage(calls=len(records), prompt_tokens=sum(r.prompt_tokens for r in records),
                     completion_tokens=sum(r.completion_tokens for r in records),
                     cost=sum(r.cost for r in records), latency_ms=(time.perf_counter() - started) * 1000.0)

    raw = None
    try:
        for _ in range(2):
            result = await model.call(messages, task_id=package.task_id, role=role,
                                      max_tokens=package.budget.max_output_tokens, timeout=package.deadline_seconds,
                                      max_cost=package.budget.max_cost)
            try:
                raw = extract_object(result.content)
                break
            except Unparseable as exc:
                messages = [*messages, ChatMessage("assistant", result.content),
                            ChatMessage("user", f"That was not one valid JSON object ({exc}). Reply with exactly one.")]
    except BudgetRefused:
        return ResultBundle(**base, status=CompletionStatus.BUDGET_REFUSED,
                            error=ErrorInfo(code="budget_refused", message="the call did not fit the budget"),
                            usage=usage())
    except asyncio.TimeoutError:
        return ResultBundle(**base, status=CompletionStatus.TIMED_OUT,
                            error=ErrorInfo(code="timeout", message="the call timed out"), usage=usage())
    except ModelUnavailable as exc:
        return ResultBundle(**base, status=CompletionStatus.FAILED,
                            error=ErrorInfo(code="model_unavailable", message=str(exc)[:200]), usage=usage())

    if raw is None:
        return ResultBundle(**base, status=CompletionStatus.FAILED,
                            error=ErrorInfo(code="unparseable", message="no valid JSON object"), usage=usage())
    body_model = BODY_MODELS[package.output_schema]
    try:
        body = body_model.model_validate(raw.get("body") if isinstance(raw.get("body"), dict) else {})
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        return ResultBundle(**base, status=CompletionStatus.FAILED,
                            error=ErrorInfo(code="schema_invalid", message=", ".join(fields)[:200]), usage=usage())

    cited = body_evidence_refs(body)
    resolved = tuple(dict.fromkeys(r for r in cited if r in supplied))
    unresolved = tuple(dict.fromkeys(r for r in cited if r not in supplied))
    urls = tuple(dict.fromkeys(URL_PATTERN.findall(body.model_dump_json())))
    declared = raw.get("status")
    status = CompletionStatus.COMPLETED
    if declared == "cannot_complete" or unresolved:
        status = CompletionStatus.PARTIAL

    def strs(key: str) -> tuple[str, ...]:
        value = raw.get(key)
        return tuple(str(v)[:300] for v in value[:10]) if isinstance(value, list) else ()

    return ResultBundle(**base, status=status, assumptions=strs("assumptions"), limitations=strs("limitations"),
                        evidence_refs=resolved, unresolved_refs=unresolved, unverified_urls=urls,
                        usage=usage(), body=body.model_dump())


__all__ = ["permitted_evidence", "run_specialist"]
