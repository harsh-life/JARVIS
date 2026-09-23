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

- **Filesystem, network, and Android tool adapters.** `file.*`, `device.*`,
  `app.*` and `system.restricted` exist as capabilities but have no adapter, so the
  runtime rejects them as `unsupported_platform`. The tool registry refuses any
  tool that declares a filesystem or network need until `09`/`10` exist to enforce
  it. That is the boundary working, not a gap in it.
- **Mem0.** The hydration boundary is built; the store is `11`'s. Without one,
  every task notes "long-term memory temporarily unavailable" (FAIL-008) and runs
  on session context.
- **Streaming.** Responses are non-streaming (02 §1.9's MVP default).
- **`PUT /config/agent`.** Per-user/per-graph `AgentConfiguration` rows are read
  (OD-RT-3 precedence), but there is no endpoint to write them yet.

## 5. Tests and checks

```bash
python3 -m pytest tests/ -q                       # the whole suite
python3 -m pytest tests/runtime/ -q               # the runtime suite
lint-imports --config pyproject.toml              # 10 boundary contracts
python3 -m pytest tests/security_core/test_od_a1_br_t2.py -q -s   # BR-T2 measurement
```

All of these run in CI on every pull request, with no secrets.
