# Running the Judge (docs/19)

The Judge is **optional** and **off by default**. With `evaluation.enabled: false`
Track B is fully functional and every circuit-breaker trigger except the
evaluator's still works (JDG-T1).

> The Judge observes and scores. It never authorizes, never executes, and never
> kills on its own authority. Its only effect on a running task is a stop
> request that the deterministic breaker enforces. (19 §0)

## 1. Configuration

```yaml
evaluation:
  enabled: false                 # the whole feature
  evaluator: llm                 # llm | rules  (rules: deterministic, no model, no cost)
  provider: null                 # required for `llm`; shaped like `agent` (06)
  #  provider: ollama            # local: traces stay on this host
  #  model: qwen2.5:7b-instruct
  #  timeout_seconds: 60
  post_hoc: { sample_successful: 0.1, always_on_failure: true }
  live: { enabled: false, every_n_steps: 1, window_steps: 20 }
  may_request_stop: false        # the operator must opt in before any evaluator can call trip()
  stop_alert_threshold: 5
  stop_alert_window_minutes: 60
  budget: 0.0                    # the Judge's own daily budget; 0.0 refuses any paid Judge call
  budget_scope: own_and_global   # OD-JDG-2 (open): also counts toward the global daily budget
  max_observation_chars: 2000
  queue_size: 64
```

* A **non-local provider receives redacted task traces**. That is a disclosed
  operator choice (19 §4); the server logs a warning at startup when you make it.
  Its `secret_ref` is a handle (`env:` or `secretstore:`), resolved per call,
  and only a SecretStore handle of class `model_api_key` is ever handed to it.
* Judge spend is metered as `model_call` usage attributed to
  `evaluator:<id>` (OD-JDG-4). It never counts against the evaluated task's
  budget, nor against the user's own daily budget or rate limits.

## 2. What happens

* **Post-hoc**: when a task ends, the runtime hands an observer a copy of it
  (the user's request, the worker's proposals, the observations it was fed,
  its audited events with each authorization's tier and decision, counters,
  the final result). Every failed task (and `sample_successful` of the rest)
  is evaluated in the background.
* **Live window** (`live.enabled`): after every `every_n_steps` tool
  executions, the last `window_steps` steps are evaluated asynchronously. The
  task never waits.
* Before any Judge call the trace is redacted with the repository's secret
  patterns; each hit is audited (`evaluation.trace.redacted`) by pattern name.
  One call sees one task of one user, and never who the user is.
* The result is stored in `evaluations` (owner-private) with one of four
  outcomes: `recorded`, `unavailable`, `malformed` (the output is rejected,
  never repaired), `over_budget` (skipped before any call). None affects the
  task.

## 3. Stop requests

A live evaluation that answers `anomaly: stop_requested` is handed to the
circuit breaker (`trip()`, source `evaluator`) **only** if `may_request_stop`
is true and the operator switch allows it. The breaker then runs its usual
sequence (18 §5.3): the in-flight call is abandoned, the task ends
`emergency_stop`, its confirmation tokens are spent, its grants revoked, any
break-glass record ended, and the user is told an automated evaluator flagged
the task. The stopped task is never resumed. Otherwise the request is recorded
with why it was not honoured.

More than `stop_alert_threshold` honoured stops in the window writes one
`evaluation.stop_alert` audit row (and a warning log). Switch the Judge off if
it is misbehaving:

```bash
curl -X POST "$BASE/api/v1/admin/control/evaluation/switches" \
  -H "Authorization: Superuser $HYPERMIND_SUPERUSER_TOKEN" \
  -d '{"judge_enabled": false, "reason": "too_many_stops"}'
```

A switch can turn the Judge or its stop requests **off**, and back on only as
far as the configuration allows.

## 4. Improvement candidates (19 §9)

The Judge may suggest a change to one of: `worker.system_prompt`,
`worker.tool_description:<tool_id>`, `recovery.stall_window`,
`recovery.loop_repeat_limit`, `recovery.max_worker_switches`,
`evaluation.rubric`, `suggestion.template`. Anything else — capabilities,
operations, tiers, authorization, graph roles, visibility, confinement,
sandbox/egress, break-glass, secrets, the breaker, the Judge's own permissions
— is refused at creation and audited (`improvement.candidate.refused`).

Nothing applies automatically. Review the queue in the console
(`GET /api/v1/admin/evaluations`; read a candidate's text through the audited
`GET /api/v1/admin/privileged/tasks/{task_id}?reason=…`), then:

```bash
# approve → a new config version (re-validated at approval)
curl -X POST "$BASE/api/v1/admin/control/evaluation/candidates/$ID/approve" \
  -H "Authorization: Superuser $HYPERMIND_SUPERUSER_TOKEN" -d '{"reason": "reviewed"}'
# reject
curl -X POST "$BASE/api/v1/admin/control/evaluation/candidates/$ID/reject" \
  -H "Authorization: Superuser $HYPERMIND_SUPERUSER_TOKEN" -d '{"reason": "not_useful"}'
# roll back a target's current version (a new version restoring the previous value)
curl -X POST "$BASE/api/v1/admin/control/evaluation/config-versions/$VERSION/rollback" \
  -H "Authorization: Superuser $HYPERMIND_SUPERUSER_TOKEN" -d '{"reason": "revert"}'
```

Applied worker text is shown to the worker as operator guidance that **grants
nothing**: every proposal is still parsed, authorized and confirmed exactly as
before. There is no training loop in this build.

## 5. Verifying

```bash
python -m pytest tests/evaluation -q     # contract, runtime wiring, review queue, boundaries
lint-imports --config pyproject.toml     # "The Judge is never an authority", "The runtime never depends on the Judge", …
```
