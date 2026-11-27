"""`RulesEvaluator` — deterministic, informational task diagnostics.

It reads one task's redacted, bounded `TaskTrace` and records what the trace
shows: repeated (redundant) proposals, execution failures and blocked events,
and an informational anomaly flag when the task hit a boundary violation or
was denied repeatedly. That is all it does.

What it cannot do, structurally (pyproject: "The rules evaluator is never an
authority", "The rules evaluator makes no model call"):

* no model call — `server.models` is not importable from this package, and
  there is no provider, rubric, prompt, budget or meter;
* no credential — it is handed no secret resolver and cannot import
  `server.secrets`;
* no effect — it returns a frozen `RulesEvaluation`; it holds no breaker,
  runtime, configuration, grant or prompt handle, so it cannot stop, approve,
  authorize, execute, or change anything for this or any other user.

Formerly `RulesJudge`; renamed when the LLM Judge was retired
(docs/DECISION_REGISTER.md, D-JDG-RETIRE). The rules themselves are unchanged.
"""

from __future__ import annotations

from server.evaluation.trace import TaskTrace
from shared.schemas.evaluation import (
    MAX_FAILURES,
    MAX_REDUNDANT_STEPS,
    Anomaly,
    EvaluationKind,
    FailureFinding,
    RulesEvaluation,
)


class RulesEvaluator:
    """Pure function over one trace. Same trace in, same record out."""

    id = "rules_evaluator"
    version = "1"

    def evaluate(self, trace: TaskTrace) -> RulesEvaluation:
        if not isinstance(trace, TaskTrace):
            raise TypeError("the rules evaluator reads exactly one task's trace")
        seen: set[str] = set()
        redundant: list[str] = []
        failures: list[FailureFinding] = []
        proposals = 0
        for step in trace.steps:
            if step.kind == "proposal":
                proposals += 1
                if step.text in seen:
                    redundant.append(step.ref)
                seen.add(step.text)
            elif step.kind == "event" and step.result in ("failure", "blocked"):
                category = (step.name or "event").replace(".", "_")[:32].lower()
                if category[:1].isalpha():
                    failures.append(FailureFinding(step_ref=step.ref, category=category))
        counters = trace.counters
        anomaly, reason = Anomaly.NONE, None
        if counters.get("boundary_violations", 0) > 0:
            anomaly, reason = Anomaly.SUSPICIOUS, "boundary_violation"
        elif counters.get("denials", 0) >= 2:
            anomaly, reason = Anomaly.SUSPICIOUS, "repeated_denials"
        efficiency = None if proposals == 0 else round(1.0 - len(redundant) / proposals, 3)
        return RulesEvaluation(
            task_id=trace.task_id, evaluator_id=self.id, evaluator_version=self.version,
            kind=EvaluationKind.POST_HOC, efficiency=efficiency,
            redundant_steps=tuple(redundant[:MAX_REDUNDANT_STEPS]), failures=tuple(failures[:MAX_FAILURES]),
            anomaly=anomaly, anomaly_reason=reason,
        )


__all__ = ["RulesEvaluator"]
