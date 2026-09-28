# Running the `runtime` branch

This extends `docs/RUNNING_SECURITY_CORE.md` (install, KEK, bootstrap, login are
unchanged). What is new is the **agent runtime** (`05`): tasks, model providers
(`06`), the tool registry and model-tools (`07`), usage/rate/budget enforcement
(`13`), and the memory-hydration boundary (`11`).

Read `docs/CAPABILITY_MATRIX.md` (what the agent may do, where, and at what tier)
and `docs/DECISION_REGISTER.md` (the owner's decisions and the values this branch
proposed) before configuring it.

## 1. Start the server

The entrypoint moved to the composition root, which assembles the Security Core
and the runtime together:

```bash
uvicorn server.composition.main:app --host 127.0.0.1 --port 8000
```

The default primary model is a local Ollama model (`06` §2):

```bash
ollama pull qwen2.5:3b-instruct
```

## 2. Configure the runtime

All optional; the defaults are the `[PROPOSED]` values in the decision register.

```yaml
agent:
  provider: "ollama"
  model: "qwen2.5:3b-instruct"
  # A paid provider must declare pricing, or the config fails to load (13 §3):
  # provider: "deepseek"
  # model: "deepseek-chat"
  # secret_ref: "secretstore:<handle>"      # or "env:DEEPSEEK_API_KEY" — never the key
  # pricing: { input_per_1k_tokens: 0.00027, output_per_1k_tokens: 0.0011 }
  # fallback: { provider: "ollama", model: "qwen2.5:1.5b-instruct" }
  bounds:                     # 05 §3 — every ceiling exists; these are the numbers
    max_iterations: 12
    max_model_calls: 16
    max_tool_calls: 24
    max_model_tool_nesting_depth: 1
    wall_clock_timeout_seconds: 120
    per_task_budget: 0.0      # paid calls are refused until you raise this
    memory_top_k: 5

models_as_tools:              # LLM-as-a-tool (06 §3), gated by `model.invoke`
  - id: "coder"
    provider: "ollama"
    model: "qwen2.5-coder:3b"
    description: "writes and reviews code"
    enabled: true

security:
  rate_limits:
    per_user_requests_per_minute: 60      # metered model + tool calls
    per_device_requests_per_minute: 60
    global_requests_per_minute: 600
    per_session_concurrent_tasks: 1
    per_user_concurrent_tasks: 2
    global_concurrent_tasks: 8
  budgets:
    per_user_daily_cost_limit: 0.0         # 0.0 → no paid spend at all
    global_daily_cost_limit: 0.0
```

## 3. A task, end to end

```
POST /api/v1/agent/tasks                 Idempotency-Key: <uuid>   { "input": "..." }
  → 200 AgentResult                       finished
  → 403 confirmation_required             paused; details carry task_id, token, and the
                                          exact action (tool, operation, resource, args, tier)
  → 429 rate_limited / 503 dependency_unavailable   explicit failure with failure_code

POST /api/v1/agent/tasks/{id}/confirm    { "confirmation_token": "...", "approve": true }
  → continues the task; a high_irreversible action also needs a step-up-fresh
    session (re-issue your access token first, or you get 401 step_up_required)

GET  /api/v1/agent/tasks/{id}            any of the owner's devices; 404 for anyone else
POST /api/v1/agent/tasks/{id}/cancel
GET  /api/v1/config/tools                what the agent can use, per platform, per tier
GET  /api/v1/intelligence/status         {"enabled": false} — the MVP default
```

A task starts with **no** capabilities active. The agent asks for what it needs;
a capability you already granted (`POST /api/v1/capabilities`) activates
immediately for that task only, and one you have not granted pauses for your
approval and becomes a task-scoped grant that is revoked when the task ends.

## 4. What is intentionally missing

- **Filesystem, network, `system.restricted`, and Android tool adapters exist
  now** — the execution branch built them; see `docs/RUNNING_EXECUTION.md`.
  They are no longer a gap in the runtime.
- **Mem0.** The hydration boundary is built; the store is `11`'s. Without one,
  every task notes "long-term memory temporarily unavailable" (FAIL-008) and runs
  on session context.
- **Streaming.** Responses are non-streaming (02 §1.9's MVP default).
- **`PUT /config/agent`.** Per-user/per-graph `AgentConfiguration` rows are read
  (OD-RT-3 precedence), but there is no endpoint to write them yet.

## 4a. Deployment limits of the current foundation (known, documented)

These are properties of the pilot architecture, measured during the
integration-hardening pass through U6, not bugs to work around:

- **One server process owns the store.** The task registry, the circuit
  breaker, the in-memory side of the global latch, and break-glass records all
  live in the process. Run one uvicorn worker. (The persisted latch still fails
  closed across processes; nothing else is shared.)
- **The pilot's runtime store is PostgreSQL (H-1).** SQLite stays supported
  for development and tests, but it has one writer, and PRD #32 (fairness
  under ~10-device load) is measured and met only on PostgreSQL
  (`docs/RELEASE_VALIDATION.md` §I, `docs/DECISION_REGISTER.md` §2I H-1).
  Setup: `docs/RUNNING_FOUNDATION.md` §4.
- **No store transaction is held across a long wait.** A request still has
  one session (02 §1.2: a refusal commits its audit and nothing else), but the
  runtime commits what a task has written so far — its row, its
  authorization decisions, its audit, its metered usage — before each model
  call, tool run and memory search, and the memory write path commits before
  the provider's write (`TaskEnvironment.release_store`). So one user's
  running task holds no lock and no pooled connection while it waits, and
  another user's request is served at once. Consequences, deliberately:
  what a task did before an unexpected failure stays recorded (its row,
  audit and spend) instead of being rolled back, and the restart
  reconciliation below closes a row left `running`; a confirmation token
  spent on an approved action is spent durably before the action runs, so a
  retry can never run it twice.
- **In-process guards cover what concurrency now exposes.** A retry with the
  same `Idempotency-Key` that arrives while the original still runs is
  refused `409 conflict` (`details.idempotency = "in_progress"`) and replays
  the stored result once the original has answered. Usage admission counts the
  calls already admitted but not yet committed to the ledger, so two users
  cannot both pass a budget or rate limit with room for one. Both guards live
  in the one server process, which is another reason to run exactly one.
- **A transient store conflict is a retryable `503`.** SQLite's busy lock, a
  PostgreSQL deadlock, serialization failure, lock timeout or connection
  pressure → `503 dependency_unavailable` (`details.dependency = "storage"`),
  with that request's transaction rolled back — never a `500`.
- **A restart fails what it interrupted, closed.** The transcript and any
  paused action are volatile (MEM-001). At startup, before the first request,
  every task left non-terminal is closed — `confirmation_state_lost` if it was
  paused, `internal_error` if it was running — with its task grants revoked,
  its tokens spent, its temp root removed, and an `agent.task.abandoned` audit
  row. Break-glass records do not survive a restart at all.

## 5. Tests and checks

```bash
python3 -m pytest tests/ -q                       # the whole suite
python3 -m pytest tests/runtime/ -q               # the runtime suite
lint-imports --config pyproject.toml              # 14 boundary contracts
python3 -m pytest tests/security_core/test_od_a1_br_t2.py -q -s   # BR-T2 measurement
```

All of these run in CI on every pull request, with no secrets.
