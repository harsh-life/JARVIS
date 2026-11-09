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

Phase 0.1 (docs/31 §9): every Central output now passes the response
finalizer before it counts as what the user sees. Per task the harness keeps
its own `ActionLedger` (the actions the task fixture says the user asked for;
in a live run each is `not_available`, because the harness has no tools), and
the finalizer renders the action status from it. An output record keeps the
model's raw text (`raw_output`), the Phase 0 rendering (`output`, scored by
rubric 1 as before: axis A), the finalized text (`final_output`) and the
finalization record, and adds axis B (`protocol`) and axis C
(`truthfulness`), see `axes.py`.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from shared.schemas.intelligence import BackendKind, ExperimentSnapshot

from experiments.accounting import BudgetLedger, CallLedger
from experiments.actions import ActionLedger, ActionStatus, apply_fixture
from experiments.axes import TRUTHFULNESS_RUBRIC, aggregate_protocol, aggregate_truthfulness, protocol, truthfulness
from experiments.central import REPLY_MODELS, CentralOutcome, run_baseline, run_contract
from experiments.contracts import CompletionStatus, ResultBundle
from experiments.orchestration.backends import DeterministicTemplates, ModelPlanner, ScriptedAgenticRuntime
from experiments.orchestration.dispatch import USABLE, Dispatcher
from experiments.orchestration.ports import EvidenceRef, OrchestrationRequest
from experiments.finalizer import FINALIZER_VERSION, finalize
from experiments.orchestration.validator import PlanLimits, PlanRejected, validate_plan
from experiments.prompts import BASELINE_SYSTEM_V1, CENTRAL_CONTRACT, action_record
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
        if doc.central.prompt_profile == "central.contract" and doc.central.prompt_version != "1":
            return f"E1-v{doc.central.prompt_version}"
        return {"central.baseline": "E0", "central.contract": "E1"}.get(doc.central.prompt_profile, "central")
    if config.experiment == "orchestrated":
        return {"deterministic_templates": "E3", "model_planner": "E2-planner",
                "scripted_agentic_runtime": "E2-scripted-runtime"}.get(config.snapshot.backend.adapter, "E2")
    return "E4"


def _context_chars(task: Task) -> int:
    return len(task.prompt) + sum(len(e.title) + len(e.content) for e in task.evidence)


def _context_text(task: Task) -> str:
    return "\n".join([task.prompt, *(f"{e.id} {e.title} {e.content}" for e in task.evidence)])


def build_ledger(task: Task, *, proposed_by_model: bool = False) -> ActionLedger:
    """The harness's trusted record for one task (docs/31 §9.2). Nothing a
    model writes reaches it: it is built from the task fixture and, for E0,
    from the harness's own observation that the model proposed a tool call or
    capability (which the harness refused; there is nothing to run)."""

    ledger = ActionLedger(task.id)
    for spec in task.actions:
        if task.execution is not None:
            apply_fixture(ledger, spec, task.execution)
        elif proposed_by_model:
            action_id = ledger.request(spec, status=ActionStatus.PROPOSED, reason="model_proposed")
            ledger.transition(action_id, ActionStatus.NOT_AVAILABLE, reason="emulated_refusal_no_capability")
        else:
            ledger.request(spec, status=ActionStatus.NOT_AVAILABLE, reason="harness_has_no_tools")
    return ledger


_ANSWER_FIELD = re.compile(r'"(?:answer|content)"\s*:\s*"((?:[^"\\]|\\.)*)"')


def raw_answer_text(outcome: CentralOutcome) -> str:
    """What the model wrote for the user, before the finalizer: the Phase 0
    rendering when the reply parsed; otherwise the `answer`/`content` string
    pulled out of the raw output (an unparseable reply was never shown, but
    what it tried to say still counts as model behaviour)."""

    if outcome.text:
        return outcome.text
    raw = outcome.raw_output or outcome.raw_unparsed or ""
    found = _ANSWER_FIELD.findall(raw)
    return " ".join(found) if found else ""


def _prompt_text(doc) -> str:
    if doc.central.prompt_profile == "central.baseline":
        return BASELINE_SYSTEM_V1
    return CENTRAL_CONTRACT.get(doc.central.prompt_version, "")


async def _central(task: Task, pool: ModelPool, snapshot: ExperimentSnapshot, contributions=(),
                   experiment="e1", ledger: ActionLedger | None = None) -> CentralOutcome:
    doc = snapshot.document
    model = pool.get(doc.central.catalog_id, max_tokens=CENTRAL_MAX_TOKENS)
    timeout = doc.execution.per_call_timeout_seconds
    if doc.central.prompt_profile == "central.baseline" and not contributions:
        return await run_baseline(task, model, timeout=timeout, max_tokens=CENTRAL_MAX_TOKENS)
    max_chars = doc.context_policy.max_chars if doc.context_policy.mode != "none" else 0
    view = task if doc.context_policy.mode != "none" else task.model_copy(update={"evidence": ()})
    version = doc.central.prompt_version if doc.central.prompt_profile == "central.contract" else "1"
    record = action_record(ledger.outcomes()) if ledger is not None else action_record(())
    return await run_contract(view, model, timeout=timeout, max_tokens=CENTRAL_MAX_TOKENS,
                              contributions=contributions, max_chars=max_chars, experiment=experiment,
                              version=version, record=record)


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
    central = await _central(task, pool, snapshot, contributions=bundles, experiment="synthesis",
                             ledger=build_ledger(task))
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
    final_scores: list[Score] = []
    truth_rows: list[dict] = []
    protocol_rows: list[dict] = []
    latencies: list[float] = []
    outputs: list[dict] = []
    started_at = datetime.now(timezone.utc)
    for repetition in range(doc.evaluation.repetitions):
        for task, fixture_bundles in work:
            t0 = time.perf_counter()
            extra: dict = {}
            record_of_actions = build_ledger(task)
            if config.experiment == "central":
                outcome = await _central(task, pool, snapshot, ledger=record_of_actions)
            elif config.experiment == "orchestrated":
                outcome, nodes, bundles, rejected, first_ms = await _orchestrated(task, pool, snapshot, budget)
                extra = {"orchestration": _orchestration_metrics(task, nodes, bundles, rejected),
                         "first_result_ms": first_ms}
                fixture_bundles = bundles
            else:
                outcome = await _central(task, pool, snapshot, contributions=fixture_bundles, experiment="e4",
                                         ledger=record_of_actions)
            elapsed = (time.perf_counter() - t0) * 1000.0
            latencies.append(elapsed)
            if outcome.proposals and task.execution is None:
                # E0 proposed something; the harness refused it (emulated).
                record_of_actions = build_ledger(task, proposed_by_model=True)
            context = _context_text(task)
            final = finalize(outcome.model_text, record_of_actions, context=context, parsed=outcome.status == "completed",
                             model_fields=outcome.model_fields)
            sc = score(task, outcome.text, cited=outcome.cited, structured=outcome.structured,
                       action_proposed=outcome.action_proposed)
            scores.append(sc)
            # Quality of the model text the finalizer admitted (the harness's
            # status line is excluded: it names the action, and its wording
            # would otherwise be credited as the model's answer).
            sc_final = score(task, final.model_text, cited=outcome.cited, structured=outcome.structured,
                             action_proposed=outcome.action_proposed)
            final_scores.append(sc_final)
            truth = truthfulness(final, raw_answer_text(outcome), context=context, task=task,
                                 structured=outcome.structured)
            truth_rows.append(truth)
            proto = protocol(outcome, task, arm="E0" if arm == "E0" else "E1")
            protocol_rows.append(proto)
            task_calls = ledger.for_task(task.id)
            record = {
                "task_id": task.id, "category": task.category, "repetition": repetition, "status": outcome.status,
                "prompt_version": outcome.prompt_version if arm != "E0" else "1",
                "output": outcome.text, "final_output": final.text, "raw_output": outcome.raw_output,
                "model_text": outcome.model_text, "model_fields": outcome.model_fields,
                "structured": outcome.structured, "proposals": outcome.proposals,
                "model_steps": outcome.model_steps, "latency_ms": round(elapsed, 1),
                "input_ref": content_ref(json.dumps({"prompt": task.prompt,
                                                     "evidence": [e.model_dump() for e in task.evidence]})),
                "output_ref": content_ref(outcome.text or ""),
                "context_overhead": round(sum(c.prompt_chars for c in task_calls) / max(1, _context_chars(task)), 2),
                "error": outcome.error, "raw_unparsed": outcome.raw_unparsed, "score": sc.as_dict(),
                "score_final": sc_final.as_dict(), "finalization": final.as_dict(), "truthfulness": truth,
                "protocol": proto, **extra,
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
    summary["quality_final"] = aggregate(final_scores, latencies, calls)["quality"]
    summary["truthfulness"] = aggregate_truthfulness(truth_rows)
    summary["protocol"] = aggregate_protocol(protocol_rows)
    summary["per_category"] = per_category(outputs)
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
        "prompt_profile": doc.central.prompt_profile, "prompt_version": doc.central.prompt_version,
        "prompt_sha256": content_ref(_prompt_text(doc)), "finalizer_version": FINALIZER_VERSION,
        "truthfulness_rubric": TRUTHFULNESS_RUBRIC,
        "generation": {"temperature": 0, "central_max_tokens": CENTRAL_MAX_TOKENS},
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

    meta = json.loads((directory / "run.json").read_text())
    reply_model = REPLY_MODELS[meta.get("prompt_version", "1") if meta.get("arm") != "E0" else "1"]
    if meta["experiment"] == "synthesis":
        tasks = {t.id: t for t, _ in load_synthesis_cases()}
    else:
        tasks = {t.id: t for t in load_suite(meta["suite_id"]).tasks}
    outputs = [json.loads(line) for line in (directory / "outputs.jsonl").read_text().splitlines()]
    calls = [json.loads(line) for line in (directory / "calls.jsonl").read_text().splitlines()]
    scores = []
    for o in outputs:
        structured = o.get("structured")
        text = reply_model.model_validate(structured).user_facing() if structured else (o.get("output") or "")
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


def per_category(outputs: list[dict]) -> dict:
    """Per-category means of the headline numbers on each axis."""

    groups: dict[str, list[dict]] = {}
    for o in outputs:
        groups.setdefault(o["category"], []).append(o)

    def mean(values):
        values = [float(v) for v in values if v is not None]
        return round(sum(values) / len(values), 4) if values else None

    return {
        cat: {
            "tasks": len(rows),
            "answered": sum(bool(r["score"]["answered"]) for r in rows),
            "correctness": mean(r["score"]["correctness"] for r in rows),
            "completeness": mean(r["score"]["completeness"] for r in rows),
            "correctness_final": mean(r["score_final"]["correctness"] for r in rows),
            "raw_completion_claims": sum(r["truthfulness"]["raw_completion_claim"] for r in rows),
            "unsupported_completion_claims": sum(r["truthfulness"]["unsupported_completion_claims"] for r in rows),
            "unsupported_goal_claims": sum(r["truthfulness"]["unsupported_goal_claims"] for r in rows),
            "withheld_all": sum(r["truthfulness"]["withheld_all"] for r in rows),
            "latency_p50_ms": sorted(r["latency_ms"] for r in rows)[len(rows) // 2],
        }
        for cat, rows in sorted(groups.items())
    }


def run_sync(config: RunConfig) -> RunReport:
    return asyncio.run(run(config))


__all__ = ["RESULTS_DIR", "RunConfig", "RunReport", "build_ledger", "raw_answer_text", "load_synthesis_cases", "offline_echo",
           "per_category", "rescore", "run", "run_sync"]
