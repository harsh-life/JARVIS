# 18_SUPERVISORY_RUNTIME_RECOVERY_CONTROL.md
## JARVIS / Hypermind Track B — Supervisory Runtime, Recovery & Emergency Control

**Package:** next-build subsystem contract · **Depth:** deep · **Written:** 2026-09-24
**Status:** implementation contract for the next build. Formalizes owner decisions **RECOVERY-1**, the circuit-breaker half of the **Judge decision**, and the task-mode half of **OD-F1** (all owner-ratified 2026-09-24). Mechanisms marked `[PROPOSED]` are this document's choices, pending ratification.
**Authority:** below `00_CANONICAL_PRD.md` and `docs/DECISION_REGISTER.md`. **Extends** `05_AGENT_RUNTIME.md`; does not replace or edit it. Where this document widens a `05` behaviour (fallback, failure handling), it says so explicitly.
**Code this plugs into:** `server/agent/runtime.py` (`_model_step`, the fallback loop at `for provider in (models.primary, models.fallback)`, cancellation per OD-RT-4), `server/agent/state.py` (`TaskState`, `TaskStateRegistry`), `server/agent/bounds.py`, `server/composition/models.py` (`ResolvedModels`, OD-RT-3 precedence), `shared/schemas/agent.py` (`AgentTaskStatus`, `AgentFailureCode`), `server/agent/events.py`.
**Consumed by:** `19` (Judge feeds the breaker), `20` (break-glass is observable here), `28` (dashboard shows recovery and breaker state), `NEXT_BUILD_INDEX.md`.

**Labels:** `[LOCKED]` restates existing locked material with its source · `[OWNER-RATIFIED]` owner decision of 2026-09-24 · `[PROPOSED]` this document's mechanism · `[IMPL]` engineer's choice within the constraint · `[FUTURE]` · `[OPEN — OWNER]`.

---

## 0. The rules this document exists to enforce

> **1. JARVIS is the supervisor.** It is the deterministic control plane that hosts workers, detects their failure, switches them, and stops them. It is not a model and it never authors answer content itself.
> **2. Recovery is never a privileged path.** Every recovered step runs under the same principal, task, graph, capability activations, resource scope, risk tier, confirmation and execution rules as the step it replaces.
> **3. Stopping is deterministic and has many independent inputs.** No single component — Judge included — is the kill authority, and no component's absence prevents a stop.

---

## 1. Vocabulary

| Term | Meaning |
|---|---|
| **Worker** | The component that *proposes* the next step. Today every worker is an LLM behind a `ModelProvider` (`06`). §8 defines the slot. |
| **Supervisor** | The deterministic runtime (`05` + this document): parsing, authorization calls, dispatch, bounds, recovery, stopping. "JARVIS" in the owner's decisions. |
| **Worker switch** | The supervisor replacing the active worker mid-task with another eligible worker. |
| **Circuit breaker** | The deterministic component that terminates execution when any trigger in §5 fires. |
| **Safe state** | Task terminal, in-flight process groups killed, task grants revoked, pending confirmation tokens invalidated, temp roots released. |

`[OWNER-RATIFIED]` (RECOVERY-1) the owner's phrase "JARVIS may take over answering" is realized as: JARVIS routes the task to another worker, or ends it with an explicit, deterministic status report. **JARVIS never fabricates an answer** (`[LOCKED]` FAIL-CORE-002, `05` §5).

---

## 2. Normal flow (unchanged from `05`)

```
User → task (02 /agent/tasks) → Supervisor
     → Worker proposes → parse (strict schema) → 04 authorize (as the principal)
     → allow | require_confirmation | deny | prohibited
     → execute via tool/adapter (07/09/10/08) → observation → next step
```

Nothing in this document adds a second path into execution. The single tool-execute call site in `runtime.py` remains the only one (`[LOCKED]`, confirmed by the 2026-09 acceptance audit).

---

## 3. Task modes (OD-F1 autonomy model)

`[OWNER-RATIFIED]` (OD-F1): observe/read automatic; suggest must not execute; draft must not execute by itself; an explicit user instruction authorizes low-risk chaining inside the task; consequential needs confirmation; high-impact needs confirmation plus step-up; prohibited is impossible.

The tier half of OD-F1 is already implemented (`docs/CAPABILITY_MATRIX.md` §2, §4). This section adds the missing half: **what kind of task it is**.

`[PROPOSED]` every task carries an immutable `mode`, set at submission by the caller, never by the worker:

| Mode | Who creates it | Mode ceiling (highest tier any operation may reach) | Output |
|---|---|---|---|
| `execute` | the user's explicit instruction | none beyond the tier table (`low_write` automatic; `consequential`/`high_irreversible` confirm) | results of real execution |
| `draft` | the user asks for a draft | `low_read` | draft content in the final response; nothing sent, written, or scheduled |
| `suggest` | a configured suggestion source (e.g. a `22` reminder firing) | `low_read` | suggestions only |
| `observe` | a configured read-only check | `low_read` | observations only |

Rules:
- `[PROPOSED]` the ceiling is enforced by the supervisor **before** `04` is asked: an operation whose table tier exceeds the mode ceiling is refused as an observation ("not permitted in draft mode"), never confirmed. It is not a new tier and does not touch the tier table.
- `[PROPOSED]` turning a draft or suggestion into action requires a **new `execute` task created by the user**, which may take the draft as input. A worker cannot promote its own task's mode; there is no proposal field for mode.
- Default mode for `POST /agent/tasks` is `execute`, matching today's behaviour.

---

## 4. Failure detection and recovery (RECOVERY-1)

### 4.1 Detected conditions

| Condition | Detection (deterministic) | Today (`05`) | Next build |
|---|---|---|---|
| Provider unavailable / per-call timeout | `ModelUnavailable` / `asyncio.TimeoutError` | fallback to `models.fallback` if configured, else fail | unchanged, generalized to the worker chain (§4.2) |
| Malformed proposal | parser rejects after `max_parse_retries` | task fails `unparseable_proposal` | `[PROPOSED]` switch worker, then fail if the chain is exhausted |
| Stall: no progress | `[PROPOSED]` `stall_window` consecutive iterations with no successful tool execution and no final answer | runs until a bound | switch worker, then fail `stalled` |
| Stall: loop | `[PROPOSED]` the same `(tool, operation, canonical-args-hash)` proposed `loop_repeat_limit` times | runs until a bound | switch worker, then fail `stalled` |
| Worker refusal / cannot resolve | final answer carrying a structured `unresolved: true` flag in the proposal schema `[PROPOSED]` | treated as a normal finish | if `escalate_on_unresolved` is configured, switch to the next worker; else finish honestly |
| Ambiguous request | the worker asks the user a question | normal finish (the question is the answer) | unchanged — asking the user is correct behaviour, not a failure |

`[LOCKED]` a model refusal is never coerced into a fake action (`05` §6).

### 4.2 The worker chain

`[PROPOSED]` `ResolvedModels` generalizes from `(primary, fallback)` to an ordered **worker chain**:

```
chain = [ task's resolved primary (OD-RT-3: user → graph → server default),
          operator fallback(s) (config: agent.fallback, agent.recovery.chain[]) ]
```

Eligibility rules for every chain entry:
- `[OWNER-RATIFIED]` same principal, task, graph, activations, scope, tiers, confirmation, execution rules.
- `[PROPOSED]` **data-flow rule:** a worker receives the task's transcript. A chain entry is eligible only if it is either the task's own resolved primary or an **operator-configured** fallback. A worker a user configured for themselves is never used for another user's task. This keeps "who sees my data" to the set the user and operator already chose.
- `[LOCKED]` every attempt is bounded, budget-prechecked and metered exactly as `_model_step` does today (`05` §3, `13`). A paid fallback that would breach budget is refused, not made.
- `[PROPOSED]` new bound `max_worker_switches` (default 2) in `agent.bounds`; its breach fails `worker_chain_exhausted`. Switches count against `max_model_calls` like any call; recovery can never grind past a ceiling (`[LOCKED]` RT-T10).

### 4.3 What a switch preserves

| Preserved across a switch | Why |
|---|---|
| principal, session, device, graph, task id | recovery is not a new identity |
| active capability activations | they belong to the task, not the worker |
| counters (iterations, calls, budget used, wall clock) | bounds are per task |
| the compacted transcript (volatile, in-process) | continuity; compaction stays deterministic (OD-RT-2) |
| a pending confirmation | the token is bound to principal/session/task/capability/operation/resource/args-hash, **not** to the worker. On `/confirm`, the supervisor executes exactly the confirmed action; the new worker cannot re-propose or alter it |

| Not preserved | Why |
|---|---|
| anything across a server restart | the transcript is volatile by design (MEM-001); a restart fails the task closed (`confirmation_state_lost`), as today |
| the failed worker's malformed output | it is dropped, not shown to the next worker as authority |

### 4.4 Chain exhausted

`[LOCKED]` explicit failure, never a fabricated answer. The supervisor produces a deterministic status report to the user: what completed, what did not, which step was pending, and the failure code.

---

## 5. Circuit breaker (deterministic emergency control)

### 5.1 Triggers — independent inputs

| Trigger | Source | Scope |
|---|---|---|
| User cancel | `POST /agent/tasks/{id}/cancel` (exists, OD-RT-4) | task |
| Bound breach | runtime bounds (exist) | task |
| Principal revoked / user suspended | per-step principal check (exists, OD-ID-1) | task / user |
| `[PROPOSED]` repeated denials | `denial_limit` authorization denials in one task (grinding defence) | task |
| `[PROPOSED]` repeated boundary violations | `violation_limit` sandbox/egress/confinement violations in one task | task |
| `[PROPOSED]` repeated confirmation rejections | `rejection_limit` rejected confirmations in one task | task |
| `[PROPOSED]` operator stop | superuser control endpoint (§5.4) | task / user / device / global |
| `[PROPOSED]` anomaly signal | an EvaluationProvider (`19`) calling `trip()` | task |

`[OWNER-RATIFIED]` Judge is **one** trigger among many. The breaker works identically with Judge disabled; every other trigger is deterministic code that exists regardless (`[OWNER-RATIFIED]` "Judge must never become the sole kill authority").

### 5.2 The one-way rule

`[PROPOSED]` (mirrors `26`'s asymmetry) a trip signal can only **stop**. No input to the breaker can resume a task, clear a trip, grant a capability, lower a tier, or skip a confirmation. A stopped task is terminal. A latched global trip is cleared only by the operator (§5.4), never by a signal source.

### 5.3 Enforcement sequence

```
trigger → breaker.trip(scope, reason, source)            (deterministic)
  1. mark task(s) terminal: status=failed, code=emergency_stop (or cancelled for user cancel)
  2. set the cancellation event → running tool call cancelled, process group killed (OD-RT-4)
  3. invalidate pending confirmation tokens for the task(s)
  4. revoke task-scoped grants; release task temp roots (exist at task end)
  5. for device ops in flight: send cancel to the device (23 §4); late results discarded
  6. audit: breaker.tripped {scope, reason, source, task_ids}
  7. notify the user: plain reason, what was stopped, what (if anything) completed
  8. hand off to safe-state recovery (§6)
```

`[IMPL]` the breaker is a small class owned by the runtime package and exposed to other components only through a narrow port: `trip(scope, target_id, reason, source) -> None`. It exposes no method that resumes, clears (except the operator path), or authorizes.

### 5.4 Operator controls

`[PROPOSED]` superuser-only control endpoints, **separate from the dashboard module** (see `28` §1 for why):

| Endpoint | Effect |
|---|---|
| `POST /api/v1/admin/control/stop` `{scope, target_id, reason}` | trip at task / user / device scope |
| `POST /api/v1/admin/control/global-stop` `{reason}` | cancel all running tasks and refuse new submissions (`503 dependency_unavailable`, class `supervisor`) until cleared |
| `POST /api/v1/admin/control/global-clear` | clear the global latch |

Requires the superuser principal (`server/security/superuser.py`, `[LOCKED]` SUPER-001); an ordinary user token cannot reach them (`[LOCKED]` DASH-004 pattern). Every call is audited with the reason.

---

## 6. Safe-state recovery after a stop

`[OWNER-RATIFIED]` flow: *Judge detects → breaker terminates → user warned → JARVIS recovery/fallback.*

`[PROPOSED]` interpretation, stated explicitly because it constrains the owner's wording: after an **emergency** stop (anomaly, operator, repeated violations), recovery means **returning the system to a safe state and informing the user**, not silently continuing the same work with a different worker. Continuing automatically would route around the stop.

- The stopped task is not resumed.
- Consequential steps that were pending are never replayed.
- JARVIS offers the user a fresh `execute` task (optionally pre-filled with the original instruction). Starting it is a new explicit user instruction — the only thing that authorizes new execution under OD-F1.
- For **non-emergency** failures (§4: provider down, malformed, stall), recovery is the in-task worker switch, which does continue the task.

`[OPEN — OWNER]` OD-SUP-1: confirm this split (emergency → safe state + user restart; operational failure → in-task switch).

---

## 7. Observability and audit

`[PROPOSED]` new audit events (`server/agent/events.py`): `agent.worker.switched`, `agent.recovery.exhausted`, `agent.stall.detected`, `breaker.tripped`, `breaker.global.latched`, `breaker.global.cleared`. Each carries task id, from/to worker ids, reason code — never transcript content.

`[PROPOSED]` new `AgentFailureCode` values: `stalled`, `worker_chain_exhausted`, `emergency_stop`. `[PROPOSED]` `agent_tasks` gains `mode`, `worker_switches`, `stopped_by` (source class). These are additive schema changes (a migration), recorded as follow-ups in `NEXT_BUILD_INDEX.md` because `01`/`02` cannot be edited here.

The user sees the same facts in plain language through the task API; the operator sees them in `28`.

---

## 8. The worker slot — future Darwin / Blackbox boundary

`[OWNER-RATIFIED]` Darwin/Blackbox is **deferred** and not part of the current runtime. The current LLM does understanding, planning, reasoning, tool selection and chaining.

`[PROPOSED]` the only boundary needed now is the worker slot the supervisor already has:

```
Worker (Protocol)
  propose(messages: compacted transcript, tools: visible tool summaries) -> raw proposal text
  health() -> bool
  id / cost spec (for bounds and metering)
```

- Today: `LLMWorker` wraps a `ModelProvider`. This is a refactor of `_model_step`, not new behaviour.
- A future Darwin/Blackbox reasoning or chaining engine plugs in as **another Worker implementation**, or as a strategy that composes workers. It receives the same inputs and its output goes through the **same parser, bounds, metering, `04` authorization, tiers, confirmation and confinement**. It gets no new port: no authorization handle, no tool handle, no SecretStore handle, no capability registry.
- `[LOCKED]` the existing import contract "Runtime never depends on Intelligence/Decision providers" stays. A Darwin worker is injected by the composition root, never imported by `server/agent`.
- A Darwin system may *also* sit behind the IntelligenceProvider socket (PRD §25) for domain intelligence. That is a different role and does not change this slot.

No separate Darwin document is created: this section is the whole required boundary.

---

## 9. Configuration additions (`[PROPOSED]`, shape follows `15`)

```yaml
agent:
  fallback: {...}                 # exists
  recovery:
    chain: []                     # extra operator-configured workers, in order
    max_worker_switches: 2
    escalate_on_unresolved: false
    stall_window: 3
    loop_repeat_limit: 3
  breaker:
    denial_limit: 5
    violation_limit: 3
    rejection_limit: 3
```

All values are bounds, not authority. Removing the section yields today's behaviour.

---

## 10. Open items

| ID | Question | Status |
|---|---|---|
| OD-SUP-1 | Emergency stop → safe state + user restart; operational failure → in-task switch (§6) | `[OPEN — OWNER]`, recommended as written |
| OD-SUP-2 | Numeric defaults in §9 | `[IMPL]`, existence locked by this document |
| OD-SUP-3 | Whether `escalate_on_unresolved` may escalate to a *paid* worker | `[OPEN — OWNER]`; default no |

---

## 11. Acceptance hooks (for `17`; `[PROPOSED]` IDs)

- **SUP-T1** a provider outage on the primary switches to the next eligible worker; the task continues under the same principal/graph/activations.
- **SUP-T2** a malformed-proposal failure after parse retries switches worker; exhausting the chain fails `worker_chain_exhausted`, never a fabricated answer.
- **SUP-T3** a loop (same tool+args hash repeated) is detected and handled per §4.1.
- **SUP-T4** a worker switch cannot exceed `max_model_calls`, budget, or wall clock (RT-T10 analogue).
- **SUP-T5** a pending confirmation survives a worker switch and executes exactly the confirmed action.
- **SUP-T6** a user-configured worker is never used for another user's task (data-flow rule).
- **SUP-T7** `draft`/`suggest`/`observe` tasks cannot execute any operation above `low_read`; the worker cannot change mode.
- **SUP-T8** every breaker trigger in §5.1 stops the task with Judge disabled.
- **SUP-T9** a trip cannot be reversed by any signal source; a latched global stop refuses new tasks until the operator clears it.
- **SUP-T10** after an emergency stop no pending consequential action is replayed and the task is not auto-resumed.
- **SUP-T11** operator control endpoints reject an ordinary user token.
- **SUP-T12** every switch, stall and trip emits its audit event with no transcript content.

---

*End of 18. Next: `19_JUDGE_EVALUATION.md`.*
