# 33 — Laya: a production-facing orchestration interface (plan)

> **[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]** — owner decision **OD-LAYA-1**.
> A plan, not a build. Nothing in `server/` changes until it is ratified.
> Owner direction (2026-11-26): keep Laya as the intelligent orchestrator; do
> not move the experiment harness wholesale into production; give Laya a
> production interface with strict schemas and deterministic validation; keep
> the local Qwen provider working; no paid provider calls.

## 1. What Laya is, and is not

Laya **proposes**: decomposition, work packages, dependencies, specialist
assignments, sequencing, and recovery recommendations. Track B **decides and
executes**. A plan is untrusted model output, like a single `tool_call`.

```
user request → Central (worker) proposes `delegate_plan`
            → Laya (planner model) returns a LayaPlan          [untrusted]
            → PlanValidator (deterministic, server/agent)      [authority boundary]
            → each node becomes an ordinary runtime step:
              tool/model calls still go through activation → engine → tier →
              confirmation → execution, exactly as today
            → Central synthesizes from validated ResultBundles
```

Laya never holds a grant, a key, a session or a device; it cannot call a
tool, n8n, memory or the vault; it cannot widen a task's capabilities,
budget, bounds or deadline. A plan node can only *ask* for work the task was
already allowed to do.

## 2. What is reused from `experiments/` and what is not

The harness proved the contract shape on local Qwen (validation evidence
2026-11-26: a 3-node plan citing unsupplied evidence was rejected before any
dispatch; a valid 1-node plan was dispatched). Reuse the **contracts and the
validator's rules**, re-implemented in production packages under the import
contracts — **not** the harness, its runner, scoring, prompts or result files.

| Harness piece (`experiments/`) | Production home | Change |
|---|---|---|
| `contracts.Plan/PlanNode/WorkPackage/ResultBundle` + typed bodies | `shared/schemas/laya.py` | add `node_kind`, capability hints, version field; frozen, `extra="forbid"` |
| `orchestration/validator.validate_plan` | `server/agent/laya_validator.py` (pure) | add capability/tool-registry checks, task-bound checks, depth/fan-out from `agent.bounds` |
| `orchestration/backends.ModelPlanner` | `server/agents/laya/planner.py` | calls the task's model through the existing metered path; no new provider code |
| `orchestration/dispatch` | `server/agent/runtime.py` (a new step kind) | nodes run as runtime steps, never a parallel executor |
| runner, scoring, prompts, results, annotation | stay in `experiments/` | never imported by `server/` (existing contract) |

## 3. Strict schemas (draft)

```python
class LayaNode(StrictModel):            # frozen, extra="forbid"
    node_id: constr(pattern=r"^[a-z][a-z0-9_]{0,15}$")
    kind: Literal["specialist", "tool_intent"]
    specialization: SpecializationId | None      # required iff kind == specialist
    instruction: constr(max_length=1000)
    depends_on: tuple[NodeId, ...] = ()          # ≤ 4
    evidence_refs: tuple[EvidenceId, ...] = ()   # subset of what the task supplied
    capability_hint: CapabilityName | None       # tool_intent only; a hint, never a grant

class LayaPlan(StrictModel):
    schema_version: Literal["laya.plan/1"]
    task_id: UUID                                 # must equal the running task
    nodes: tuple[LayaNode, ...]                   # 1..max_fan_out
    rationale: constr(max_length=1000) | None
```

Parsing is deterministic (one JSON object, no repair beyond the runtime's
existing bounded parse retries). Unknown fields, extra keys or any
authority-shaped field (`approved`, `grant`, `budget`, `priority`, `tier`,
`confirmed`, `url`, `endpoint`) make the plan unparseable.

## 4. Deterministic validation (the authority boundary)

`validate_plan(plan, limits) -> list[ValidatedNode] | PlanRejected(issues)`,
pure, no I/O. Every rule returns a stable reason code:

* `wrong_task`, `empty_plan`, `fan_out_exceeded`, `duplicate_node`,
  `unknown_dependency`, `cycle`, `depth_exceeded`;
* `specialization_not_enabled` — only operator-enabled specializations;
* `evidence_not_supplied` — refs ⊆ the task's supplied evidence ids;
* `capability_not_registered` / `capability_floor` — a `capability_hint`
  must exist in the closed registry and never be a floor capability; a hint
  for a capability the task has not activated is allowed but is executed
  only through the normal activation + confirmation path;
* `budget_exceeded` — the plan's projected model calls fit
  `max_model_calls`, `per_task_budget` and the user's live budget;
* `mode_violation` — `draft`/`suggest`/`observe` tasks accept only
  specialist nodes, never `tool_intent`;
* `unattended_violation` — under a `DelegatedPrincipal`, no node may hint a
  capability above the unattended ceiling.

A rejected plan is an observation to Central (reason codes only). Nothing is
dispatched from a rejected plan, in part or in whole.

## 5. Recovery

Laya may propose `retry_node`, `skip_node` or `replan` after a node fails.
The supervisor (docs/18) decides, under the existing breaker and
`max_worker_switches`; a replan is validated from scratch against the
remaining budget and the already-dispatched node set. No proposal can resume
a task the breaker stopped.

## 6. Configuration and providers

```yaml
laya:
  enabled: false                 # off by default
  planner: agent.primary         # a model entry already configured (local Qwen today)
  max_fan_out: 3
  max_depth: 3
  per_node_timeout_seconds: 240  # local 3B on an 8 GB host needs this
  specializations: [research, assessment, planning]
```

The planner uses the same `ModelProvider` and metering as every model call
(local Ollama `qwen2.5:3b-instruct` today, cost 0). A hosted provider is a
config change plus `pricing` and a non-zero budget the owner approves; none
is configured.

## 7. Acceptance tests (before ratification)

1. Every reason code in §4 has a unit test, including authority-shaped fields.
2. A rejected plan dispatches nothing (ledger: zero model/tool calls after the
   planner's own call).
3. A `tool_intent` node for an unactivated consequential capability pauses for
   confirmation exactly as a direct `tool_call` would; declining runs nothing.
4. Unattended runs: any hint above the ceiling is refused before the engine.
5. Laya disabled → Track B unchanged (all existing suites green).
6. Live smoke on local Qwen, recorded, never asserted on model text.
7. `lint-imports`: `server.agent` still imports no secrets, capabilities or
   graph; the planner lives in `server.agents`, the validator in
   `server.agent`, and `experiments/` is still never imported.

## 8. Open decisions

* **OD-LAYA-1** — ratify this interface (or amend).
* **OD-LAYA-2** — whether `tool_intent` nodes are allowed at all in the first
  build, or Laya stays specialist-only (text work) until a later milestone.
* **OD-LAYA-3** — whether specialists may use a different model from Central.
