"""Experiment runs (docs/31 §5).

* `central` — Central alone on every task. With the snapshot's Central prompt
  profile `central.baseline` this is **E0** (the production prompt and
  parser); with `central.contract` it is **E1**. Same tasks, same rubric, so
  two snapshots compare directly.
* `orchestrated` — plan → validate → dispatch specialists → Central synthesis,
  on tasks that carry a hand-authored decomposition. With the
  deterministic-templates backend this is **E3** (specialists isolated from
  planning quality); with the model planner it is one arm of **E2**.
* `synthesis` — **E4**: Central synthesis over fixed specialist bundles
  (contradictory, missing, failed, repeated, unsupported) from
  `tasks/synthesis_v1.yaml`, so synthesis is measured without specialist noise.

Every run records the snapshot's configuration hash, every model call, and
every output; scoring is the deterministic rubric. Nothing is executed.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from shared.schemas.intelligence import BackendKind, ExperimentSnapshot

from experiments.accounting import BudgetLedger, CallLedger
from experiments.central import CentralOutcome, run_baseline, run_contract
from experiments.contracts import CompletionStatus, ResultBundle
from experiments.orchestration.backends import DeterministicTemplates, ModelPlanner, ScriptedAgenticRuntime
from experiments.orchestration.dispatch import USABLE, Dispatcher
from experiments.orchestration.ports import EvidenceRef, OrchestrationRequest
from experiments.orchestration.validator import PlanLimits, PlanRejected, validate_plan
from experiments.providers import ModelPool, ProviderMap, ScriptedProvider
from experiments.scoring import Score, aggregate, score
from experiments.tasks import SUITE_DIR, Task, load_suite
from experiments.trace import SecretScanner, TraceWriter, content_ref

RESULTS_DIR = Path(__file__).parent / "results"
# One output cap for every Central arm, so E0 and E1 are compared fairly.
CENTRAL_MAX_TOKENS = 1200


@dataclass
class RunConfig:
    experiment: str                         # central | orchestrated | synthesis
    snapshot: ExperimentSnapshot
    live: bool = False
    provider_map: ProviderMap | None = None
    scripted: Callable | None = None        # (ResolvedModel, max_tokens) -> ScriptedProvider
    task_ids: tuple[str, ...] = ()
    out_dir: Path | None = None
    known_secret_values: tuple[str, ...] = ()
    label: str = ""


@dataclass
class RunReport:
    run_id: str
    experiment: str
    arm: str
    config_hash: str
    summary: dict
    outputs: list[dict] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    directory: Path | None = None


def _arm(config: RunConfig) -> str:
    doc = config.snapshot.document
    if config.experiment == "central":
        return {"central.baseline": "E0", "central.contract": "E1"}.get(doc.central.prompt_profile, "central")
    if config.experiment == "orchestrated":
        return {"deterministic_templates": "E3", "model_planner": "E2-planner",
                "scripted_agentic_runtime": "E2-scripted-runtime"}.get(config.snapshot.backend.adapter, "E2")
    return "E4"


def _context_chars(task: Task) -> int:
    return len(task.prompt) + sum(len(e.title) + len(e.content) for e in task.evidence)


async def _central(task: Task, pool: ModelPool, snapshot: ExperimentSnapshot, contributions=(),
                   experiment="e1") -> CentralOutcome:
    doc = snapshot.document
    model = pool.get(doc.central.catalog_id, max_tokens=CENTRAL_MAX_TOKENS)
    timeout = doc.execution.per_call_timeout_seconds
    if doc.central.prompt_profile == "central.baseline" and not contributions:
        return await run_baseline(task, model, timeout=timeout, max_tokens=CENTRAL_MAX_TOKENS)
    max_chars = doc.context_policy.max_chars if doc.context_policy.mode != "none" else 0
    view = task if doc.context_policy.mode != "none" else task.model_copy(update={"evidence": ()})
    return await run_contract(view, model, timeout=timeout, max_tokens=CENTRAL_MAX_TOKENS,
                              contributions=contributions, max_chars=max_chars, experiment=experiment)


def _orchestration_metrics(task: Task, plan_nodes, bundles: list[ResultBundle], rejected: list[str]) -> dict:
    expected = [n.specialization for n in task.decomposition]
    got = [n.specialization for n in plan_nodes]
    union = set(expected) | set(got)
    keys = [(n.specialization, tuple(sorted(n.evidence_refs))) for n in plan_nodes]
    return {
        "plan_nodes": len(got),
        "specialist_selection_jaccard": round(len(set(expected) & set(got)) / len(union), 3) if union else None,
        "duplicate_work": len(keys) - len(set(keys)),
        "plan_rejected": rejected,
        "bundles": {b.subtask_id: b.status.value for b in bundles},
        "unusable_bundles": sum(b.status not in USABLE for b in bundles),
        "unresolved_refs": sum(len(b.unresolved_refs) for b in bundles),
        "unverified_urls": sum(len(b.unverified_urls) for b in bundles),
    }


async def _orchestrated(task: Task, pool: ModelPool, snapshot: ExperimentSnapshot, budget: BudgetLedger) -> tuple:
    doc = snapshot.document
    started = time.perf_counter()
    request = OrchestrationRequest(
        task_id=task.id, prompt=task.prompt, evidence=tuple(EvidenceRef(e.id, e.title) for e in task.evidence),
        allowed_specializations=tuple(s.specialization for s in doc.specialists if s.enabled),
        max_fan_out=min(doc.orchestration.max_fan_out, snapshot.backend.max_fan_out),
        deadline_seconds=doc.orchestration.timeout_seconds, config_hash=snapshot.config_hash,
        template=task.decomposition,
    )
    adapter = snapshot.backend.adapter
    if adapter == "deterministic_templates":
        backend = DeterministicTemplates()
    elif adapter == "model_planner":
        backend = ModelPlanner(pool.get(doc.orchestration.model_catalog_id, max_tokens=600))
    else:
        raise ValueError(f"the harness has no runnable adapter {adapter!r} (scripted runtimes are test-only)")
    config_issues = backend.validate_config(snapshot)
    if config_issues:
        raise ValueError(f"backend refused the snapshot: {config_issues}")
    outcome = await backend.plan(request)
    rejected: list[str] = []
    packages = []
    nodes = outcome.plan.nodes if outcome.plan else ()
    if outcome.plan is None:
        rejected.append(outcome.error or "no_plan")
    else:
        limits = PlanLimits.from_snapshot(snapshot, task_id=task.id, evidence_ids=task.evidence_ids,
                                          remaining_budget=budget.overall - budget.spent - budget.reserved)
        try:
            packages = validate_plan(outcome.plan, limits)
        except PlanRejected as exc:
            rejected.extend(exc.issues)
    assignments = {s.specialization: s for s in doc.specialists if s.enabled}
    first_result_ms = None
    bundles: list[ResultBundle] = []
    if packages:
        dispatcher = Dispatcher(assignments=assignments, evidence={e.id: e for e in task.evidence},
                                model_for=lambda a: pool.get(a.catalog_id, max_tokens=a.max_output_tokens),
                                config_hash=snapshot.config_hash, mode=doc.execution.mode)
        remaining = doc.execution.overall_timeout_seconds - (time.perf_counter() - started)
        bundles = await dispatcher.run(packages, deadline=max(0.1, remaining))
        if any(b.status in USABLE for b in bundles):
            first_result_ms = (time.perf_counter() - started) * 1000.0
    central = await _central(task, pool, snapshot, contributions=bundles, experiment="synthesis")
    return central, nodes, bundles, rejected, first_result_ms


def load_synthesis_cases() -> list[tuple[Task, list[ResultBundle]]]:
    raw = yaml.safe_load((SUITE_DIR / "synthesis_v1.yaml").read_text())
    cases = []
    for case in raw["cases"]:
        task = Task.model_validate(case["task"])
        bundles = [ResultBundle.model_validate({"task_id": task.id, "config_hash": "fixture",
                                                "assignment_id": f"{b['specialization']}@fixture", **b})
                   for b in case["bundles"]]
        cases.append((task, bundles))
    return cases


async def run(config: RunConfig) -> RunReport:
    snapshot = config.snapshot
    doc = snapshot.document
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    arm = _arm(config)
    scanner = SecretScanner(config.known_secret_values)
    ledger = CallLedger()
    budget = BudgetLedger(overall=doc.execution.overall_budget, per_call=doc.execution.per_call_budget)
    pool = ModelPool(snapshot=snapshot, ledger=ledger, budget=budget, provider_map=config.provider_map,
                     live=config.live, scripted=config.scripted, prompt_guard=scanner.check_prompt)
    writer = None
    if config.out_dir is not None:
        writer = TraceWriter(config.out_dir / f"{run_id}-{arm}", scanner)

    if config.experiment == "synthesis":
        work = load_synthesis_cases()
    else:
        suite = load_suite(doc.evaluation.suite_id)
        tasks = [t for t in suite.tasks if not config.task_ids or t.id in config.task_ids]
        if config.experiment == "orchestrated":
            tasks = [t for t in tasks if t.decomposition]
        work = [(t, []) for t in tasks]

    scores: list[Score] = []
    latencies: list[float] = []
    outputs: list[dict] = []
    started_at = datetime.now(timezone.utc)
    for _rep in range(doc.evaluation.repetitions):
        for task, fixture_bundles in work:
            t0 = time.perf_counter()
            extra: dict = {}
            if config.experiment == "central":
                outcome = await _central(task, pool, snapshot)
            elif config.experiment == "orchestrated":
                outcome, nodes, bundles, rejected, first_ms = await _orchestrated(task, pool, snapshot, budget)
                extra = {"orchestration": _orchestration_metrics(task, nodes, bundles, rejected),
                         "first_result_ms": first_ms}
                fixture_bundles = bundles
            else:
                outcome = await _central(task, pool, snapshot, contributions=fixture_bundles, experiment="e4")
            elapsed = (time.perf_counter() - t0) * 1000.0
            latencies.append(elapsed)
            sc = score(task, outcome.text, cited=outcome.cited, structured=outcome.structured,
                       action_proposed=outcome.action_proposed)
            scores.append(sc)
            task_calls = ledger.for_task(task.id)
            record = {
                "task_id": task.id, "category": task.category, "status": outcome.status,
                "output": outcome.text, "structured": outcome.structured, "proposals": outcome.proposals,
                "model_steps": outcome.model_steps, "latency_ms": round(elapsed, 1),
                "input_ref": content_ref(json.dumps({"prompt": task.prompt,
                                                     "evidence": [e.model_dump() for e in task.evidence]})),
                "output_ref": content_ref(outcome.text or ""),
                "context_overhead": round(sum(c.prompt_chars for c in task_calls) / max(1, _context_chars(task)), 2),
                "error": outcome.error, "raw_unparsed": outcome.raw_unparsed, "score": sc.as_dict(), **extra,
            }
            outputs.append(record)
            if writer:
                writer.append("outputs.jsonl", record)

    calls = [c.as_dict() for c in ledger.records]
    summary = aggregate(scores, latencies, calls)
    summary["efficiency"]["context_overhead_mean"] = (
        round(sum(o["context_overhead"] for o in outputs) / len(outputs), 2) if outputs else None)
    summary["budget"] = {"overall": budget.overall, "per_call": budget.per_call, "spent": round(budget.spent, 6),
                         "refusals": budget.refusals, "exceeded_reservation": budget.exceeded_reservation,
                         "overspent": budget.overspent}
    summary["safety"]["secret_prompts_blocked"] = scanner.blocked_prompts
    if config.experiment == "orchestrated":
        orch = [o["orchestration"] for o in outputs]
        summary["orchestration"] = {
            "plans_rejected": sum(bool(o["plan_rejected"]) for o in orch),
            "specialist_selection_jaccard": (round(sum(o["specialist_selection_jaccard"] or 0 for o in orch)
                                                   / len(orch), 3) if orch else None),
            "duplicate_work": sum(o["duplicate_work"] for o in orch),
            "unusable_bundles": sum(o["unusable_bundles"] for o in orch),
            "partial_failure_recovered": sum(1 for o, out in zip(orch, outputs)
                                             if o["unusable_bundles"] and out["status"] == "completed"),
        }
        firsts = [o["first_result_ms"] for o in outputs if o.get("first_result_ms") is not None]
        summary["efficiency"]["time_to_first_result_p50_ms"] = sorted(firsts)[len(firsts) // 2] if firsts else None
    meta = {
        "run_id": run_id, "experiment": config.experiment, "arm": arm, "label": config.label,
        "mode": "live" if config.live else "scripted",
        "config_hash": snapshot.config_hash, "profile_id": snapshot.profile_id,
        "profile_version": snapshot.profile_version, "backend": snapshot.backend.model_dump(mode="json"),
        "models": {k: {"provider": m.provider, "model": m.model, "local": m.local}
                   for k, m in snapshot.models.items()},
        "suite_id": doc.evaluation.suite_id, "rubric_version": doc.evaluation.rubric_version,
        "repetitions": doc.evaluation.repetitions, "tasks": len(work),
        "started_at": started_at.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
        "token_counts": "provider-reported" if config.live else "estimated (chars/4, scripted provider)",
    }
    if writer:
        for c in calls:
            writer.append("calls.jsonl", c)
        writer.write_json("run.json", meta)
        writer.write_json("summary.json", summary)
        writer.write_json("snapshot.json", snapshot.model_dump(mode="json"))
    return RunReport(run_id=run_id, experiment=config.experiment, arm=arm, config_hash=snapshot.config_hash,
                     summary={"run": meta, **summary}, outputs=outputs, calls=calls,
                     directory=writer.dir if writer else None)


def offline_echo(resolved, max_tokens) -> ScriptedProvider:
    """The default offline provider: a fixed, labelled reply in whichever
    format the prompt asks for. It exercises the pipeline only — its scores
    say nothing about any model."""

    from experiments.providers import scripted_spec

    def respond(messages):
        system = messages[0].content
        if "planning component of an assistant" in system:
            return json.dumps({"type": "final_answer", "content": "[scripted offline reply]"})
        if "plan work for a team" in system:
            return json.dumps({"rationale": "scripted", "nodes": []})
        if "specialist inside a larger assistant" in system:
            return json.dumps({"status": "cannot_complete", "limitations": ["scripted offline reply"],
                               "body": {}})
        return json.dumps({"answer": "[scripted offline reply]", "needs_clarification": False})

    return ScriptedProvider(scripted_spec(resolved, max_tokens), respond)


def rescore(directory: Path) -> dict:
    """Re-score a stored run under the current rubric, without calling any
    model: Central's user-facing text is re-rendered from the stored
    structured reply, and the original `summary.json` is left untouched.
    Writes `rescored.json`."""

    from experiments.central import CentralReply

    meta = json.loads((directory / "run.json").read_text())
    if meta["experiment"] == "synthesis":
        tasks = {t.id: t for t, _ in load_synthesis_cases()}
    else:
        tasks = {t.id: t for t in load_suite(meta["suite_id"]).tasks}
    outputs = [json.loads(line) for line in (directory / "outputs.jsonl").read_text().splitlines()]
    calls = [json.loads(line) for line in (directory / "calls.jsonl").read_text().splitlines()]
    scores = []
    for o in outputs:
        structured = o.get("structured")
        text = CentralReply.model_validate(structured).user_facing() if structured else (o.get("output") or "")
        cited = list(structured.get("evidence_refs") or []) if structured else None
        sc = score(tasks[o["task_id"]], text, cited=cited, structured=structured,
                   action_proposed=bool(o.get("proposals")))
        o["rescored"] = sc.as_dict()
        scores.append(sc)
    summary = aggregate(scores, [o["latency_ms"] for o in outputs], calls)
    result = {"run_id": meta["run_id"], "arm": meta["arm"], "config_hash": meta["config_hash"],
              "rescored_at": datetime.now(timezone.utc).isoformat(), "summary": summary,
              "per_task": {o["task_id"]: o["rescored"] for o in outputs}}
    (directory / "rescored.json").write_text(json.dumps(result, indent=2))
    return result


def run_sync(config: RunConfig) -> RunReport:
    return asyncio.run(run(config))


__all__ = ["RESULTS_DIR", "RunConfig", "RunReport", "load_synthesis_cases", "offline_echo", "rescore", "run",
           "run_sync"]
