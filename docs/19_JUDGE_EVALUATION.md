# 19_JUDGE_EVALUATION.md
## JARVIS / Hypermind Track B — Judge, EvaluationProvider & Human-Approved Improvement

**Package:** next-build subsystem contract · **Depth:** deep · **Written:** 2026-09-24
**Status:** formalizes the owner's **Judge / Evaluation decision** (owner-ratified 2026-09-24). The Judge is **optional**; Track B must be fully functional with it disabled.
**Authority:** below `00_CANONICAL_PRD.md` and `docs/DECISION_REGISTER.md`. Relates to `26_DECISION_PROVIDER.md` (a different abstraction — §2), `18` (circuit breaker), `13` (metering), `11` (visibility of stored records), `28` (display).
**Code this plugs into:** `server/agent/events.py` and the audit/usage ledgers (`server/security/audit.py`, `server/security/usage.py`) as the trace source; the breaker port from `18` §5.3; `server/models/` for the Judge's own model calls. New package `[PROPOSED]`: `server/evaluation/`.

**Labels:** `[LOCKED]` · `[OWNER-RATIFIED]` · `[PROPOSED]` · `[IMPL]` · `[FUTURE]` · `[OPEN — OWNER]`.

---

## 0. The rule this document exists to enforce

> **The Judge observes and scores. It never authorizes, never executes, and never kills on its own authority. Its only effect on a running task is a stop request that deterministic code enforces.**

`[OWNER-RATIFIED]` Judge is NOT: authorization, capability authority, security policy, trust root, confirmation bypass, or privileged execution authority. Judge is separate from Darwin.

---

## 1. Role

The Judge is a large-model supervisory evaluator. It answers four questions about a task:

1. **Quality** — did the result satisfy the user's request?
2. **Efficiency** — were steps redundant, wasteful, or unnecessary?
3. **Failure** — where and why did it go wrong?
4. **Anomaly** — is the behaviour extreme or dangerous enough to stop now?

Questions 1–3 are post-hoc and advisory. Question 4 may run while the task is live and is the only one that can affect a running task — and only through the breaker (§6).

---

## 2. Why a separate EvaluationProvider, not DecisionProvider

`[OWNER-RATIFIED]` do not force Judge into DecisionProvider; assess.

`[PROPOSED]` **verdict: separate abstraction.** The two differ on every axis that matters:

| | DecisionProvider (`26`) | EvaluationProvider (this doc) |
|---|---|---|
| Call site | inside runtime control flow, before a routing choice | outside the loop, subscribed to the task's trace |
| Input | a small, bounded decision request | the whole task trace |
| Output | a typed advisory signal the runtime interprets | scores, findings, reward records, optional stop request |
| Latency budget | tight (it is in the loop) | loose (mostly post-hoc) |
| Status | proposed, unratified (OD-DP-9) | owner-ratified role |

Shared principle, kept identical: **signals may only increase caution.** A DecisionProvider "clean" cannot lower a control; a Judge "fine" cannot lower a control; a Judge "stop" can only stop.

---

## 3. Interface

`[PROPOSED]` vendor-neutral:

```
EvaluationProvider (Protocol)
  id, version
  health() -> bool
  evaluate(trace: TaskTrace, kind: post_hoc | live_window) -> Evaluation
```

`[IMPL]` the default implementation is an LLM judge built on an existing `ModelProvider` adapter (no parallel model stack). A rules-only evaluator is also a valid implementation.

**Import boundary** `[PROPOSED]` (import-linter contract):
- `server/agent` must not import `server/evaluation` (the runtime never depends on the Judge — same shape as the existing INTEL-003 contract).
- `server/evaluation` must not import `server.capabilities`, `server.graph` (authorization), `server.secrets`, `server.execution`, `server.fs`, `server.net`, `server.tools`.
- The only runtime-facing handle the evaluation package receives is the breaker's `trip()` port, injected by the composition root.

---

## 4. Inputs — the TaskTrace

`[PROPOSED]` a `TaskTrace` is assembled from records that already exist:

| Field | Source | Notes |
|---|---|---|
| original user request, mode | task record | |
| proposals and parsed steps | runtime transcript (volatile) | captured while the task is in memory |
| authorization decisions, tiers, confirmations | audit events | |
| tool calls, outcomes, durations | audit + usage events | |
| observations | transcript, truncated to `max_observation_chars` | |
| worker switches, stalls, trips | `18` audit events | |
| final result or failure code | task record | |

Rules:
- `[LOCKED]` no secret value can be in a trace: agents hold handles only, and resolved values never enter observations (`12`, SS-T1). `[PROPOSED]` a secret-pattern detector (the same patterns used for the repository scan) also runs over the trace before it leaves the process; a match is redacted and audited.
- `[PROPOSED]` **visibility:** a trace contains only what the task's own principal could see — it is their task. No trace is ever combined with another user's trace in one Judge call.
- `[PROPOSED]` **reasoning artifacts:** the Judge sees only artifacts the worker produced as output. It does not claim access to hidden model internals.
- `[PROPOSED]` **data flow:** the Judge provider must be operator-configured and disclosed to users, like any fallback worker (`18` §4.2). A deployment with a cloud Judge sends task traces to that provider; that is a configuration choice, never a hidden dependency.

---

## 5. Outputs

`[PROPOSED]` `Evaluation` (typed, schema-validated; malformed output is rejected, never coerced):

```
Evaluation {
  task_id, evaluator_id, evaluator_version, kind
  quality: score 0..1 | null
  efficiency: score 0..1 | null, redundant_steps: [step_ref]
  failures: [{step_ref, category, note}]
  anomaly: none | suspicious | stop_requested, anomaly_reason
  reward: {credit: float, attributed_to: [step_ref]} | null
  improvement_candidates: [ImprovementCandidate]
}
```

- Scores are **records**, not controls. Nothing in the runtime reads `quality`, `efficiency` or `reward` to make a decision.
- `[PROPOSED]` evaluations are stored with the same ownership and visibility as the task (owner-private by default, `11`'s triplet). The operator sees them redacted in `28`.

---

## 6. Anomaly and emergency stop

`[OWNER-RATIFIED]` *Judge detects → deterministic circuit breaker → execution terminated → user warned → JARVIS recovery/fallback.* Judge must never be the sole kill authority.

`[PROPOSED]` live monitoring:
- A `live_window` evaluation runs over the recent trace window when configured (e.g. after each tool execution or every N steps). It runs **asynchronously**; the task does not wait for it.
- On `anomaly = stop_requested`, the evaluation package calls `breaker.trip(scope=task, reason=anomaly_reason, source=evaluator_id)`. The breaker (`18` §5) enforces the stop. The Judge performs none of the enforcement steps itself.
- `suspicious` does not stop anything. `[PROPOSED]` it is recorded and may raise caution through existing deterministic means only (e.g. surfaced to the user and operator); it can never lower one.
- A stop is honoured without second-guessing because stopping is the safe direction. The cost of a wrong stop is availability, not safety. `[PROPOSED]` if one evaluator trips more than `stop_alert_threshold` tasks in a window, the operator is alerted; the operator may disable the Judge. Judge stops are not silently ignored.

**The Judge can be wrong in both directions, and neither weakens security:**
- A missed anomaly (false "fine") leaves every deterministic control in place — the Judge never removed any.
- A false stop costs one task.

---

## 7. Failure behaviour

| Judge condition | Behaviour |
|---|---|
| disabled | Track B fully functional (breaker still has every other trigger) |
| unavailable / timeout | task continues; evaluation recorded as `unavailable`; no security effect |
| malformed output | rejected; recorded; no effect |
| over budget | evaluation skipped and recorded; the **task's** budget is never charged for Judge calls (§8) |

Continuing without the Judge is correct because the Judge is not a security boundary. Every security property holds without it.

---

## 8. Metering and cost

- `[LOCKED]` every model call is metered (USAGE-001). Judge calls emit `UsageEvent{kind: model_call}` attributed to the evaluator.
- `[PROPOSED]` Judge spend has its **own** budget (`evaluation.budget`) so evaluating a task cannot exhaust that task's or user's budget. Whether Judge spend counts against the per-user budget, the global budget, or both is OD-JDG-2.
- `[PROPOSED]` post-hoc evaluation may sample (e.g. evaluate every failed task and a configured fraction of successful ones) to control cost. Live monitoring is off by default.

---

## 9. Self-improvement and human oversight

Contained here because the Judge is where improvement signals originate; no separate document is needed.

```
execution → evaluation → reward/credit → improvement candidate → HUMAN APPROVAL → versioned config change
```

`[PROPOSED]` `ImprovementCandidate {target, proposed_change, evidence: [task_id], expected_effect}`.

**What a candidate may target** (after human approval): worker system-prompt text, tool descriptions shown to the worker, recovery/stall thresholds, Judge rubric wording, suggestion templates.

**What no candidate may ever target** `[PROPOSED, locked if ratified]`:
- the capability registry, operation mapping, tier table, floor names
- authorization rules, graph roles, visibility predicate
- confinement modes, sandbox/egress policy, break-glass rules (`20`)
- SecretStore behaviour or any secret
- the breaker's triggers or its one-way rule
- the Judge's own permissions or its ability to approve candidates

Rules:
- `[PROPOSED]` candidates go to a review queue visible in `28`. Nothing is applied automatically. Approval is a superuser action, audited, producing a versioned config change that can be rolled back.
- `[PROPOSED]` reward records are stored, not consumed. There is no training loop in the current build (`[LOCKED]` PRD §21: fine-tuning/LoRA is future and deferred).
- `[PROPOSED]` cross-user analysis of traces for improvement (e.g. "this tool description confuses many users") uses redacted aggregates only; raw traces from different users are never combined into one prompt.

Security policy and authorization stay entirely outside learned or self-improving behaviour.

---

## 10. Configuration (`[PROPOSED]`)

```yaml
evaluation:
  enabled: false
  provider: null                  # an entry shaped like agent.primary (06 ModelEntryConfig)
  post_hoc: { sample_successful: 0.1, always_on_failure: true }
  live: { enabled: false, every_n_steps: 1 }
  may_request_stop: false         # operator must opt in before any evaluator can call trip()
  stop_alert_threshold: 5
  budget: 0.0
```

`may_request_stop: false` by default: the Judge's stop ability is itself an operator opt-in.

---

## 11. Open items

| ID | Question | Status |
|---|---|---|
| OD-JDG-1 | Ratify EvaluationProvider as separate from DecisionProvider (§2) | `[OPEN — OWNER]`, recommended |
| OD-JDG-2 | Judge budget scope (own / per-user / global) | `[OPEN — OWNER]`, rec own + global |
| OD-JDG-3 | Pilot Judge provider (which model, cloud or local) | `[OPEN — OWNER]` |
| OD-JDG-4 | `usage.kind` extension (`evaluation_call`) vs `model_call` with attribution | `[OPEN — OWNER]`; needs a `01` §1.2 edit if extended |

---

## 12. Acceptance hooks (`[PROPOSED]` IDs)

- **JDG-T1** with `evaluation.enabled: false`, all existing acceptance tests pass and every breaker trigger except `anomaly` still works.
- **JDG-T2** `server/agent` cannot import `server/evaluation`; `server/evaluation` cannot import capabilities, graph authorization, secrets, execution, fs, net, or tools (CI).
- **JDG-T3** a Judge `stop_requested` results in the breaker's enforcement sequence; the Judge module performs none of it.
- **JDG-T4** no Judge output can resume a task, grant a capability, lower a tier, or satisfy a confirmation.
- **JDG-T5** Judge unavailable / malformed / over budget → task unaffected; outcome recorded.
- **JDG-T6** a trace never contains a secret value; a planted secret pattern is redacted before any Judge call.
- **JDG-T7** no Judge call combines two users' traces.
- **JDG-T8** every Judge call is metered and never charged to the evaluated task's budget.
- **JDG-T9** an improvement candidate targeting any §9 forbidden target is rejected at creation; no candidate applies without a superuser approval.

---

*End of 19. Next: `20_CONFINEMENT_BREAK_GLASS.md`.*
