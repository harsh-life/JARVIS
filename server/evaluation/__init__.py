"""evaluation — the Judge (19_JUDGE_EVALUATION.md).

The Judge observes and scores. It never authorizes, never executes, and never
kills on its own authority; its only effect on a running task is a stop
*request* that the deterministic circuit breaker enforces (19 §0, JDG-T3).

  * `trace`      — the one-task, one-user, redacted, bounded TaskTrace (19 §4)
  * `provider`   — the EvaluationProvider interface, `LLMJudge` (on an existing
                   ModelProvider) and `RulesJudge` (19 §3); strict parsing (§5)
  * `metering`   — every Judge model call metered against its own budget (§8)
  * `candidates` — the closed registry of what an improvement may target (§9)
  * `service`    — one evaluation, start to finish, with §7's failure outcomes
  * `runner`     — the asynchronous queue; the task never waits for the Judge
  * `ports`      — what this package needs, satisfied by the composition root

Boundaries (pyproject contracts "The Judge is never an authority", "The runtime
never depends on the Judge", "The Judge opens no process or socket", and
"Only the gateway reaches superuser authority"):

  * the runtime never imports this package — it notifies an observer port the
    composition root satisfies;
  * this package cannot reach authorization (`server.capabilities`,
    `server.graph`), identity, secrets, execution, the file sandbox, the egress
    client, tools, the runtime, the gateway, the operator control path,
    break-glass records, or the improvement approval path. It cannot approve
    its own candidates: approval is a superuser action in the composition root.

Track B is fully functional with `evaluation.enabled: false` (JDG-T1).
"""
