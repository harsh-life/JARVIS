# 32 — Automation through n8n: an execution target, never an authority

> **[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]** — owner decision **OD-AUTO-1**
> (§7). Built off by default (`automation.n8n.enabled: false`). Nothing in the
> runtime depends on it.

## 1. The rule

```
user request → model proposes `automation.run_workflow` → Track B authorizes
(capability grant, workflow scope, risk tier, human confirmation) → the
execution layer calls ONE operator-pinned n8n webhook → the result returns to
the runtime as an untrusted observation → the model presents it
```

n8n executes. It never decides whether a user may run a workflow, it never
sees who the user is, and nothing it returns can grant, confirm or widen
anything. The model never supplies a URL, a webhook path, a method, a header
or free-form payload: only a pinned `workflow_id` and that workflow's typed
inputs.

## 2. Where it lives

| Piece | File | Layer |
|---|---|---|
| Capability `automation.workflow`, operation `run_workflow` → `consequential`, scope key `workflow_id` | `server/capabilities/registry.py` | capabilities (closed registry) |
| Operator configuration `automation.n8n` | `server/config/schema.py` | config |
| `N8nWorkflowExecutor` (validation, dedupe, retry, size caps, transport) | `server/execution/automation.py` | execution |
| Tool contract `automation.run_workflow` + adapter | `server/tools/platforms.py` | tools |
| Wiring; webhook-token reference resolved by handle | `server/composition/execution_tools.py` | composition |

No import contract changed; `lint-imports` keeps 38/38. `server.execution`
still never imports `server.secrets`: the composition root hands the executor
an opaque async callable (`key_provider_for(secret_ref, SecretRequester.server())`).

## 3. Why an execution target and not a `net.request` destination

`server/net/policy.py` blocks loopback for every tool, unconditionally ("a tool
cannot reach server-local services"). A self-hosted n8n on the same host is a
server-local service, so routing it through `net.request` would mean weakening
that invariant. Instead n8n is treated like the Android device: a fixed
execution target the execution layer dispatches an already-authorized request
to, at a loopback origin pinned by the operator (config refuses any
non-loopback `base_url`).

## 4. What the executor enforces (defence behind the engine)

* `workflow_id` must be one of `automation.n8n.workflows[].id`; unknown ids,
  URLs and paths are refused before any connection (`invalid_arguments`).
* `inputs` must be exactly that workflow's typed fields: unknown fields,
  missing required fields, wrong types (a boolean is never an integer) and
  over-long strings are refused before any connection.
* a grant narrowed with `resource_scope: {workflow_id: …}` is re-checked
  (`sandbox_violation` on mismatch).
* request ≤ `max_request_bytes`; response ≤ `max_response_bytes`; the
  response must be one JSON object; redirects are never followed; 401/403/404
  from n8n are `automation_unavailable` (contract broken), other ≥ 400 are
  `workflow_failed` (the workflow ran and failed).
* **Idempotency**: key = SHA-256(user, task, workflow, canonical inputs), sent
  as `X-Jarvis-Idempotency-Key`. A repeat of a call that may have reached n8n
  (success, workflow failure, timeout, refusal) returns the recorded outcome
  without calling n8n; a concurrent repeat waits for the first. A failure
  before anything was sent (credential unavailable, connection never opened,
  request too large) is not remembered, so a later attempt may run. The
  confirmation token is single-use on top of this.
* **Retry**: exactly one retry, only when the connection was never
  established. A timeout or any HTTP answer is never retried (a webhook POST is
  not idempotent).
* n8n receives `{workflow_id, inputs, jarvis: {task_id}}`. No user id, email,
  device, token or memory.

## 5. Operating it (development)

```yaml
automation:
  n8n:
    enabled: true
    base_url: "http://127.0.0.1:5678"          # loopback only
    secret_ref: "env:JARVIS_N8N_WEBHOOK_TOKEN"  # or secretstore:<handle>
    workflows:
      - id: jarvis.echo
        webhook_path: jarvis-echo
        description: "Echo and transform a short message (TEST ONLY)."
        inputs:
          message: {type: string, max_length: 200}
          count:   {type: integer, required: false}
```

n8n side: a Webhook node (POST, production URL, **Header Auth** credential
checking `X-Jarvis-Webhook-Token`), `Respond to Webhook` returning one JSON
object. Run n8n with its port published on `127.0.0.1` only, a persistent
volume, its own `N8N_ENCRYPTION_KEY`, diagnostics/templates/version checks off.
The editor is never exposed through the tunnel.

The user grants the capability (optionally narrowed to one workflow); every
run is still confirmed on the card built from the exact pending action.
Unattended Agent-Factory runs cannot use it (`consequential` > the
`low_write` ceiling).

## 6. Tests

* `tests/execution/test_automation_n8n.py` — executor, capability, registry, config (39).
* `tests/integration/test_automation_n8n_e2e.py` — through the production
  composition root: confirm-before-run, no grant → nothing reaches n8n, scope
  narrowing, unpinned/malformed refusal, failure observation, no replay (6).
  Set `JARVIS_N8N_LIVE_TOKEN_FILE` to run the same cases against a real n8n.

## 7. Owner direction (2026-11-26) and open decisions

Direction given by the owner on 2026-11-26, recorded here; the build above
already satisfies the first three for the validation workflows:

* n8n **may eventually** act on real external systems, but every workflow
  needs an explicit capability, its own scoped credential and Track B
  authorization; n8n is never an authority.
* Human confirmation is **mandatory for every external write** (today every
  run is `consequential`, so every run is confirmed).
* **High-irreversibility** workflows require **fresh authentication** — the
  existing step-up by device re-attestation used for `high_irreversible`
  actions (docs/23). Not built for workflows yet: it needs per-workflow tiers
  (OD-AUTO-2) so such a workflow resolves to `high_irreversible`.
* The validation workflows stay self-contained and TEST ONLY.

Still open:


* **OD-AUTO-1** — ratify n8n as an execution target with this contract, or not.
* **OD-AUTO-2** — per-workflow tiers declared by the operator (`consequential`
  or `high_irreversible` with step-up), which needs a registry change: tiers
  are per operation today. Until then every run is `consequential`.
* **OD-AUTO-3** — the webhook token is a server-owned operator credential sent
  to a loopback service. The tool registry refuses contracts that declare
  `may_send_credentials` (user credentials leaving through a tool); this
  contract does not declare it because no user credential is involved. Confirm
  that reading.
* **OD-AUTO-4** — per-workflow scoped credentials: how each workflow's
  external credential is held (n8n credential store vs SecretStore handle per
  workflow) and who may bind it. No external-system workflow until decided.
* Durable dedupe: the idempotency record is in process memory (bounded LRU).
  It does not survive a restart; the single-use confirmation still prevents a
  replayed approval. A table-backed record is future work.
