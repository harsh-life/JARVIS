"""The corrected comparison (Phase 0.1, docs/31 §9.5).

Reads stored runs, Phase 0 and Phase 0.1 alike, and reports them on three
separate axes (`axes.py`). It **never writes into a run directory**: the
Phase 0 results stay exactly as they were, and this report is a new, labelled
file beside them.

Every run is re-finalized here from what the model actually wrote (Phase 0.1
runs store `model_text`; for Phase 0 runs it is rebuilt from the stored
structured reply or final answer, exactly as the Phase 0 renderer would have
built it, minus the declined-actions line). So every arm is judged by the same
finalizer version, whichever code was current when it ran. No model is called.

Known gaps for Phase 0 runs, reported rather than guessed: the raw model
output of a parsed reply and the top-level keys of its JSON were not stored,
so invented state fields cannot be detected after the fact; parse failures
are inferred from the step count.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from experiments.axes import aggregate_protocol, aggregate_truthfulness, protocol, truthfulness
from experiments.central import REPLY_MODELS, CentralOutcome
from experiments.finalizer import FINALIZER_VERSION, finalize
from experiments.runner import _ANSWER_FIELD, _context_text, build_ledger, load_synthesis_cases
from experiments.scoring import _pct, aggregate, score
from experiments.tasks import load_suite


def _load(directory: Path) -> tuple[dict, list[dict], list[dict]]:
    meta = json.loads((directory / "run.json").read_text())
    outputs = [json.loads(line) for line in (directory / "outputs.jsonl").read_text().splitlines() if line]
    calls_path = directory / "calls.jsonl"
    calls = [json.loads(line) for line in calls_path.read_text().splitlines() if line] if calls_path.exists() else []
    return meta, outputs, calls


def _arm_label(meta: dict) -> str:
    phase = "P0.1" if "finalizer_version" in meta else "P0"
    return f"{phase} {meta['arm']}"


def _rebuild(meta: dict, o: dict) -> tuple[str, str, list[str], CentralOutcome]:
    """→ (model text for the finalizer, the Phase 0 rendering, JSON keys, an
    outcome carrying what the protocol axis needs)."""

    e0 = meta["arm"] == "E0"
    version = "1" if e0 else meta.get("prompt_version", "1")
    structured = o.get("structured")
    if "model_text" in o:                      # Phase 0.1 run: stored verbatim
        model_text, fields = o["model_text"], list(o.get("model_fields") or [])
        rendered = o.get("output") or ""
        failures = (o.get("protocol") or {}).get("parse_failures", 0)
    else:                                      # Phase 0 run: rebuilt, see module docstring
        fields = []
        if e0:
            model_text = rendered = (o.get("output") or "") if o["status"] == "completed" else ""
            failures = o["model_steps"] - len(o.get("proposals") or []) - (1 if o["status"] == "completed" else 0)
        else:
            reply = REPLY_MODELS[version].model_validate(structured) if structured else None
            model_text = reply.model_text() if reply else ""
            rendered = reply.user_facing() if reply else ""
            failures = o["model_steps"] - (1 if o["status"] == "completed" else 0)
    outcome = CentralOutcome(task_id=o["task_id"], experiment="e0" if e0 else "e1", status=o["status"],
                             text=rendered, structured=structured, proposals=list(o.get("proposals") or []),
                             model_steps=o["model_steps"], model_fields=fields, parse_failures=max(0, failures),
                             prompt_version=version, raw_output=o.get("raw_output"),
                             raw_unparsed=o.get("raw_unparsed"))
    return model_text, rendered, fields, outcome


def _raw_answer(outcome: CentralOutcome) -> str:
    if outcome.text:
        return outcome.text
    found = _ANSWER_FIELD.findall(outcome.raw_output or outcome.raw_unparsed or "")
    return " ".join(found)


def evaluate_run(directory: Path) -> dict:
    meta, outputs, calls = _load(directory)
    if meta["experiment"] == "synthesis":
        tasks = {t.id: t for t, _ in load_synthesis_cases()}
    else:
        tasks = {t.id: t for t in load_suite(meta["suite_id"]).tasks}
    rows = []
    for o in outputs:
        task = tasks[o["task_id"]]
        model_text, rendered, fields, outcome = _rebuild(meta, o)
        ledger = build_ledger(task, proposed_by_model=bool(outcome.proposals) and task.execution is None)
        context = _context_text(task)
        final = finalize(model_text, ledger, context=context, parsed=o["status"] == "completed", model_fields=fields)
        cited = list(o["structured"].get("evidence_refs") or []) if o.get("structured") else None
        rows.append({
            "task_id": task.id, "category": task.category, "repetition": o.get("repetition", 0),
            "scenario": task.execution.scenario if task.execution else None,
            "variant": task.id.split("--")[0] if task.execution else None,
            "status": o["status"], "latency_ms": o["latency_ms"],
            "score": score(task, rendered, cited=cited, structured=o.get("structured"),
                           action_proposed=bool(outcome.proposals)),
            "score_final": score(task, final.model_text, cited=cited, structured=o.get("structured"),
                                 action_proposed=bool(outcome.proposals)),
            "truthfulness": truthfulness(final, _raw_answer(outcome), context=context, task=task,
                                         structured=o.get("structured")),
            "explanation_withheld": final.explanation_withheld,
            "protocol": protocol(outcome, task, arm="E0" if meta["arm"] == "E0" else "E1"),
            "raw": _raw_answer(outcome), "final": final.text, "withheld_reason": final.withheld_reason,
        })
    reps = sorted({r["repetition"] for r in rows})
    calls_by_rep = _calls_by_repetition(calls, [o["task_id"] for o in outputs if o.get("repetition", 0) == reps[0]])
    per_rep = {rep: _aggregate([r for r in rows if r["repetition"] == rep], calls_by_rep.get(rep, []))
               for rep in reps}
    return {
        "label": _arm_label(meta), "directory": str(directory), "run_id": meta["run_id"], "arm": meta["arm"],
        "suite_id": meta["suite_id"], "config_hash": meta["config_hash"],
        "prompt_profile": meta.get("prompt_profile"), "prompt_version": meta.get("prompt_version", "1"),
        "prompt_sha256": meta.get("prompt_sha256"), "models": meta.get("models"), "mode": meta.get("mode"),
        "repetitions": len(reps), "tasks_per_repetition": len(rows) // max(1, len(reps)),
        "token_counts": meta.get("token_counts"),
        "per_repetition": per_rep,
        "summary": _over_reps(per_rep),
        "all": _aggregate(rows, calls),
        "per_category": _per_group(rows, "category"),
        "per_scenario": _per_group(rows, "scenario") if any(r["scenario"] for r in rows) else None,
        "per_variant": _per_group(rows, "variant") if any(r["variant"] for r in rows) else None,
        "action_task_examples": [
            {k: r[k] for k in ("task_id", "repetition", "raw", "final", "withheld_reason")}
            for r in rows if r["truthfulness"]["policy"] == "action_intent"
        ][:400],
        "admitted_on_action_tasks": [
            {"task_id": r["task_id"], "repetition": r["repetition"], "final": r["final"]}
            for r in rows if r["truthfulness"]["policy"] == "action_intent" and r["truthfulness"]["admitted_model_sentences"]
        ],
    }


def _calls_by_repetition(calls: list[dict], order: list[str]) -> dict[int, list[dict]]:
    """Calls carry a task id but no repetition. Tasks run in the same order in
    every repetition, so a step back in that order starts the next one."""

    index = {t: i for i, t in enumerate(order)}
    out: dict[int, list[dict]] = defaultdict(list)
    rep, prev = 0, -1
    for c in sorted(calls, key=lambda c: c["call_id"]):
        i = index.get(c["task_id"], prev)
        if i < prev:
            rep += 1
        prev = i
        out[rep].append(c)
    return out


def _aggregate(rows: list[dict], rep_calls: list[dict]) -> dict:
    if not rows:
        return {}
    base = aggregate([r["score"] for r in rows], [r["latency_ms"] for r in rows], rep_calls)
    final_quality = aggregate([r["score_final"] for r in rows], [], [])["quality"]
    return {
        "tasks": len(rows),
        "A_quality": base["quality"],
        "A_quality_final": final_quality,
        "B_protocol": aggregate_protocol([r["protocol"] for r in rows]),
        "C_truthfulness": aggregate_truthfulness([r["truthfulness"] for r in rows]),
        "safety_rubric1": base["safety"],
        "efficiency": {**base["efficiency"],
                       "latency_p50_ms": _pct([r["latency_ms"] for r in rows], 0.5),
                       "latency_p95_ms": _pct([r["latency_ms"] for r in rows], 0.95),
                       "calls_errors": [c["error"] for c in rep_calls if c.get("error")][:20]},
    }


def _over_reps(per_rep: dict) -> dict:
    """mean / min / max across repetitions for every numeric leaf."""

    if not per_rep:
        return {}
    first = next(iter(per_rep.values()))
    out: dict = {}
    for section, values in first.items():
        if not isinstance(values, dict):
            continue
        out[section] = {}
        for key, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            series = [r[section][key] for r in per_rep.values() if isinstance(r[section].get(key), (int, float))]
            if series:
                out[section][key] = {"mean": round(sum(series) / len(series), 4), "min": min(series),
                                     "max": max(series)}
    return out


def _per_group(rows: list[dict], key: str) -> dict:
    groups: dict = defaultdict(list)
    for r in rows:
        groups[r[key]].append(r)
    out = {}
    for name, items in sorted(groups.items(), key=lambda kv: str(kv[0])):
        t = aggregate_truthfulness([r["truthfulness"] for r in items])
        q = aggregate([r["score"] for r in items], [r["latency_ms"] for r in items], [])["quality"]
        qf = aggregate([r["score_final"] for r in items], [], [])["quality"]
        out[str(name)] = {"tasks": len(items), "answered": len(items) - q["unanswered"],
                          "correctness": q["correctness"], "completeness": q["completeness"],
                          "correctness_final": qf["correctness"],
                          "raw_completion_claims": t["raw_completion_claims"],
                          "unsupported_completion_claims": t["unsupported_completion_claims"],
                          "unsupported_goal_claims": t["unsupported_goal_claims"],
                          "fabricated_evidence_in_final": t["fabricated_evidence_in_final"],
                          "withheld_all": t["withheld_all"],
                          "latency_p50_ms": _pct([r["latency_ms"] for r in items], 0.5)}
    return out


def compare(directories: list[Path], out_dir: Path) -> dict:
    runs = [evaluate_run(d) for d in directories]
    report = {
        "title": "Phase 0.1 corrected comparison (re-finalized; Phase 0 originals untouched)",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "finalizer_version": FINALIZER_VERSION,
        "runs": runs,
    }
    out_dir.mkdir(parents=True, exist_ok=False)
    (out_dir / "compare.json").write_text(json.dumps(report, indent=2, default=str))
    (out_dir / "compare.md").write_text(render_markdown(report))
    return report


def _m(summary: dict, section: str, key: str, single: dict | None = None) -> str:
    v = summary.get(section, {}).get(key)
    if v is None:
        return "n/a"
    if v["min"] == v["max"]:
        return f"{v['mean']:g}"
    return f"{v['mean']:g} ({v['min']:g}–{v['max']:g})"


def render_markdown(report: dict) -> str:
    lines = [f"# {report['title']}", "", f"Generated {report['generated_at']}; finalizer v{report['finalizer_version']}.",
             "Values are per repetition: mean (min–max) when there were several. Counts are per repetition.", ""]
    by_suite: dict = defaultdict(list)
    for r in report["runs"]:
        by_suite[r["suite_id"]].append(r)
    rows_a = [("Unanswered", "A_quality", "unanswered"), ("Correctness", "A_quality", "correctness"),
              ("Completeness", "A_quality", "completeness"), ("Citation recall", "A_quality", "citation_recall"),
              ("Citation precision", "A_quality", "citation_precision"),
              ("Contradiction detection", "A_quality", "contradiction_detection"),
              ("Clarification when needed", "A_quality", "clarification_when_needed"),
              ("Uncertainty when missing", "A_quality", "uncertainty_when_missing"),
              ("Unsupported claims (rubric 1)", "A_quality", "unsupported_claims"),
              ("Correctness of admitted model text", "A_quality_final", "correctness"),
              ("Completeness of admitted model text", "A_quality_final", "completeness")]
    rows_c = [("Raw completion claims (screen)", "C_truthfulness", "raw_completion_claims"),
              ("Raw completion claims (rubric-1 regex)", "C_truthfulness", "raw_rubric1_claims"),
              ("Raw goal claims", "C_truthfulness", "raw_goal_claims"),
              ("Raw fabricated receipts/ids", "C_truthfulness", "raw_fabricated_evidence"),
              ("**Unsupported completion claims, final**", "C_truthfulness", "unsupported_completion_claims"),
              ("**Unsupported goal claims, final**", "C_truthfulness", "unsupported_goal_claims"),
              ("**Fabricated receipts/ids, final**", "C_truthfulness", "fabricated_evidence_in_final"),
              ("Rubric-1 regex hits in final model text", "C_truthfulness", "final_rubric1_claims"),
              ("Tasks with correct status lines", "C_truthfulness", "status_lines_ok"),
              ("Screen missed a claim the rubric-1 regex saw", "C_truthfulness", "screen_missed_vs_rubric1"),
              ("Informational tasks with a sentence suppressed", "C_truthfulness",
               "informational_tasks_with_suppression"),
              ("Action-intent tasks", "C_truthfulness", "action_intent_tasks"),
              ("— with a model explanation (never rendered)", "C_truthfulness", "action_tasks_with_explanation"),
              ("— explanation contradicted the record", "C_truthfulness",
               "action_tasks_explanation_contradicted_record"),
              ("— explanation was screen-clean (useful content lost?)", "C_truthfulness",
               "action_tasks_explanation_clean_only"),
              ("Explanation sentences withheld", "C_truthfulness", "explanation_sentences_withheld"),
              ("— of which screen-clean", "C_truthfulness", "explanation_clean_sentences_withheld"),
              ("Expected facts withheld (rubric fact groups)", "C_truthfulness", "expected_facts_withheld"),
              ("Clarification questions withheld", "C_truthfulness", "clarifications_withheld")]
    rows_e = [("Latency p50 (ms)", "efficiency", "latency_p50_ms"), ("Latency p95 (ms)", "efficiency", "latency_p95_ms"),
              ("Model calls", "efficiency", "calls"), ("Failed calls", "efficiency", "calls_failed"),
              ("Prompt tokens", "efficiency", "prompt_tokens"), ("Completion tokens", "efficiency", "completion_tokens"),
              ("Cost", "efficiency", "cost")]
    for suite, runs in by_suite.items():
        lines += [f"## Suite `{suite}`", "", "| Run | Model | Prompt | Config hash | Reps × tasks |",
                  "|---|---|---|---|---|"]
        for r in runs:
            model = ", ".join(f"{m['provider']}:{m['model']}" for m in (r["models"] or {}).values())
            lines.append(f"| {r['label']} | {model} | {r['prompt_profile'] or ('central.baseline' if r['arm'] == 'E0' else 'central.contract')}"
                         f" v{r['prompt_version']} | `{r['config_hash'][:12]}` | {r['repetitions']} × {r['tasks_per_repetition']} |")
        for title, spec in (("A. Answer quality (rubric 1)", rows_a), ("C. User-facing action truthfulness", rows_c),
                            ("Efficiency", rows_e)):
            lines += ["", f"### {title}", "", "| | " + " | ".join(r["label"] for r in runs) + " |",
                      "|---" * (len(runs) + 1) + "|"]
            for name, section, key in spec:
                lines.append(f"| {name} | " + " | ".join(_m(r["summary"], section, key) for r in runs) + " |")
        lines += ["", "### B. Protocol compliance (each arm against its own protocol; not comparable across arms)", ""]
        for r in runs:
            proto = r["all"]["B_protocol"]
            lines.append(f"* **{r['label']}** ({proto.get('protocol')}, all repetitions): "
                         + ", ".join(f"{k} {v}" for k, v in proto.items() if k != "protocol"))
        lines += ["", "### Per category (all repetitions pooled)", ""]
        cats = sorted({c for r in runs for c in r["per_category"]})
        lines += ["| Category | " + " | ".join(f"{r['label']} correct / raw claims / unsupported final" for r in runs) + " |",
                  "|---" * (len(runs) + 1) + "|"]
        for c in cats:
            cells = []
            for r in runs:
                g = r["per_category"].get(c)
                cells.append("–" if not g else f"{g['correctness'] if g['correctness'] is not None else 'n/a'} / "
                             f"{g['raw_completion_claims']} / {g['unsupported_completion_claims'] + g['unsupported_goal_claims']}")
            lines.append(f"| {c} | " + " | ".join(cells) + " |")
        if any(r["per_scenario"] for r in runs):
            lines += ["", "### Per scripted execution state (raw completion claims → unsupported claims in final)", ""]
            scen = sorted({s for r in runs if r["per_scenario"] for s in r["per_scenario"]})
            lines += ["| State | " + " | ".join(r["label"] for r in runs) + " |", "|---" * (len(runs) + 1) + "|"]
            for s in scen:
                cells = []
                for r in runs:
                    g = (r["per_scenario"] or {}).get(s)
                    cells.append("–" if not g else f"{g['raw_completion_claims']}/{g['tasks']} → "
                                 f"{g['unsupported_completion_claims'] + g['unsupported_goal_claims'] + g['fabricated_evidence_in_final']}")
                lines.append(f"| {s} | " + " | ".join(cells) + " |")
            lines += ["", "### Per adversarial prompt", ""]
            var = sorted({v for r in runs if r["per_variant"] for v in r["per_variant"]})
            lines += ["| Prompt | " + " | ".join(r["label"] for r in runs) + " |", "|---" * (len(runs) + 1) + "|"]
            for v in var:
                cells = []
                for r in runs:
                    g = (r["per_variant"] or {}).get(v)
                    cells.append("–" if not g else f"{g['raw_completion_claims']}/{g['tasks']} → "
                                 f"{g['unsupported_completion_claims'] + g['unsupported_goal_claims'] + g['fabricated_evidence_in_final']}")
                lines.append(f"| {v} | " + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines)


__all__ = ["compare", "evaluate_run", "render_markdown"]
