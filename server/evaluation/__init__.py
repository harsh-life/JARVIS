"""evaluation — deterministic, informational task diagnostics (`RulesEvaluator`).

  * `trace`   — the one-task, one-user, redacted, bounded TaskTrace
  * `rules`   — `RulesEvaluator`: redundant proposals, failures, an
                informational anomaly flag; no model, no cost, no authority
  * `service` — one evaluation of one finished task, start to finish
  * `runner`  — the bounded queue that keeps the record's write off the task's
                own path (the task never waits for it)
  * `ports`   — the one thing this package needs (a sink), satisfied by the
                composition root

Boundaries (pyproject contracts "The rules evaluator is never an authority",
"The rules evaluator makes no model call", "The runtime never depends on
evaluation", "Evaluation opens no process or socket"):

  * the runtime never imports this package — it notifies an observer port the
    composition root satisfies, and never reads anything back;
  * this package cannot reach authorization, identity, secrets, execution,
    the file sandbox, the egress client, tools, the runtime, the model layer,
    the gateway, the operator control path or break-glass records.

The LLM Judge, its prompt/rubric, metering, stop requests, improvement
candidates and the global prompt-tuning path that used to live here were
retired (docs/DECISION_REGISTER.md, D-JDG-RETIRE; docs/OD_A1_BR_T2.md §3e).
They are not dormant: the code is gone, and configuration that names them is
refused at startup. Track B is fully functional with `evaluation.enabled: false`.
"""
