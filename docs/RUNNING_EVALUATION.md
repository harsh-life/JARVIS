# Running evaluation (deterministic `RulesEvaluator`)

Evaluation is **optional** and **off by default**. With `evaluation.enabled: false`
Track B is fully functional and every circuit-breaker trigger still works.

> The rules evaluator reads one finished task's redacted trace and records
> deterministic findings. It makes no model call, holds no credential, cannot
> stop a task, and cannot change a prompt, a setting, a grant, or anything any
> other user's task reads. It is a diagnostic, not a decision-maker.

**The LLM Judge is retired** (`docs/DECISION_REGISTER.md`, D-JDG-RETIRE). The
LLM evaluator, its prompt and rubric, metering and budget, live-window
monitoring, stop requests, operator switches, improvement candidates,
candidate approval, versioned prompt/tool-description/recovery tuning and the
agent-purpose suggestions are **gone from the code**, not disabled. Why:
`docs/OD_A1_BR_T2.md` §3e row 38 — one user's private task content reached
every other user's worker prompt through an operator-approved candidate.
`docs/19_JUDGE_EVALUATION.md` is kept as the superseded design record.

## 1. Configuration

```yaml
evaluation:
  enabled: false                 # the whole feature
  evaluator: rules               # the only evaluator
  post_hoc: { sample_successful: 0.1, always_on_failure: true }
  max_observation_chars: 2000
  queue_size: 64
```

A configuration that still names a retired Judge key — `evaluator: llm`,
`provider`, `live`, `may_request_stop`, `stop_alert_threshold`,
`stop_alert_window_minutes`, `budget`, `budget_scope` — is **refused at
startup** with a message naming the keys. It is refused even with
`enabled: false`: a configuration that describes a Judge is wrong about the
system it configures. Delete those keys when upgrading.

## 2. What happens

* When a task ends, the runtime hands an observer a copy of it (the user's
  request, the worker's proposals, the observations it was fed, its audited
  events with each authorization's tier and decision, counters, the final
  result). Every failed task (and `sample_successful` of the rest) is queued.
  Nothing observes a running task.
* The trace is redacted with the repository's secret patterns and bounded;
  each hit is audited (`evaluation.trace.redacted`) by pattern name only.
* `RulesEvaluator` records: redundant (repeated) proposals by step ref,
  failed/blocked events as `{step_ref, category}`, an `efficiency` ratio, and
  an informational anomaly flag (`suspicious` / `boundary_violation` or
  `repeated_denials`). Same trace in, same record out.
* The result is stored in `evaluations` (owner-private) with outcome
  `recorded`, or `unavailable` if the evaluator raised. `quality` is always
  NULL and the stop columns always false for new rows. No usage is metered;
  the evaluator costs nothing.
* None of it affects the task, any other task, or any configuration.

## 3. Boundaries (mechanically enforced)

`pyproject.toml` contracts, each tested with a deliberate violation
(`tests/evaluation/test_stage5_boundaries.py`):

* **The rules evaluator is never an authority** — `server.evaluation` cannot
  import authorization, secrets, execution, fs, net, tools, the runtime,
  identity, the gateway, the composition root or the audit writer.
* **The rules evaluator makes no model call** — `server.evaluation` cannot
  import `server.models`, `server.modeltools` or `httpx`, directly or
  indirectly. Bringing an LLM evaluator back means deleting this contract in
  review.
* **The runtime never depends on evaluation**, and **evaluation opens no
  process or socket**.

The composition root hands the evaluator a storage sink and nothing else —
no breaker, secret resolver, model, switchboard or prompt writer
(`server/composition/evaluation.py`).

## 4. Reading the results

```bash
curl -H "Authorization: Superuser $HYPERMIND_SUPERUSER_TOKEN" \
  "$BASE/api/v1/admin/evaluations?limit=50"
```

Findings are shown **redacted**. Historical rows written by the retired Judge
(evaluator `llm_judge`) are shown as data, with whatever `quality`, reward
and stop flags they were written with.

## 5. Historical Judge records

The Judge's tables are kept **inert** so no history is destroyed and no
destructive migration is needed:

| Table | Status | Who touches it now |
|---|---|---|
| `evaluations` | live (rules evaluator) + historical Judge rows | sink writes new rows; console reads |
| `improvement_candidates` | **inert** | nothing writes or reads content; console shows a **count**; an agent's rows are deleted with the agent |
| `config_versions` | **inert** | nothing — **not read** by the runtime; console shows a **count** |
| `evaluation_control` | **inert** | nothing |
| `usage` rows with `tool_id` `evaluator:*` | historical | still excluded from per-user budgets/rates; still counted in the global budget |
| audit actions `improvement.candidate.*`, `config.version.*`, `control.evaluation`, `evaluation.stop_*`, `agent.candidate.*` | historical | never written again |

`tests/evaluation/test_judge_retirement.py` checks structurally that no
server module constructs or updates these rows, and that a historical
approved `worker.system_prompt` planted in `config_versions` never reaches a
worker prompt.

**`improvement_candidates.proposed_value` / `expected_effect` and
`config_versions.value` may contain one user's private content** (that is
the row-38 finding). Treat them as user data.

### Retention and manual cleanup

The rows are kept until the owner decides to remove them. To clean up, with
the server stopped:

1. **Back up first** (this is the recovery path):
   `pg_dump --table=improvement_candidates --table=config_versions --table=evaluation_control "$DB" > judge-history-$(date +%F).sql`
   (SQLite: `sqlite3 data/hypermind.db ".dump improvement_candidates config_versions evaluation_control" > judge-history.sql`).
   Store the dump where you store other user data backups; it is user data.
2. Delete in foreign-key order:
   ```sql
   DELETE FROM config_versions;
   DELETE FROM improvement_candidates;
   DELETE FROM evaluation_control;
   ```
3. Optionally, historical Judge rows in `evaluations`:
   `DELETE FROM evaluations WHERE evaluator_id <> 'rules_evaluator';`
   (only after step 2: `improvement_candidates.evaluation_id` references them).
4. To restore, load the dump from step 1.

Dropping the tables themselves is a future migration, to be written when the
owner confirms no history is needed; it is deliberately not part of the
retirement change.
