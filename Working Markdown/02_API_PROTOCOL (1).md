# 02_API_PROTOCOL.md
## Hypermind Track B — API & Protocol Specification

**Package:** subsystem doc 02 of 17 · **Depth:** deep · **Status:** implementation contract
**Authority:** subordinate to `00_CANONICAL_PRD`. Every endpoint enforces the PRD's auth chain (SESSION-004), phone-never-self-authorizes rule (PHONE-003), capability model (§16), and failure semantics (§29A). Data shapes reference `01_DATA_MODEL_SCHEMA.md` by entity name; they are not redefined here.
**Consumed by:** `03` (auth flows realize the auth endpoints), `05` (agent runtime is invoked through the agent endpoints), `07`/`08` (tool/device operations flow through here), `13` (every call emits a UsageEvent), `17` (each endpoint's tests).

**Label legend:** `[LOCKED]` fixed · `[IMPL]` engineer chooses within the constraint · `[FUTURE]` deferred · `[OPEN — OWNER]` unresolved.

---

## 0. Scope & non-goals

This document defines the **server-side HTTP API** the Android client calls, plus the internal invocation contracts for agent→tool, agent→model, and agent→IntelligenceProvider. It defines *shape and rules*, not implementation. The transport is HTTPS over the Cloudflare Tunnel (PRD SRV-002); the tunnel is transport only, never the authorization layer (PRD §18) — **every endpoint independently performs full authN + authZ** even though it arrives via the tunnel.

Non-goals: OIDC redirect mechanics (that's `03`), the agent loop internals (that's `05`), the actual DB queries (that's `01`/`04`). This doc says *what endpoints exist and what contract they honor*.

---

## 1. Cross-cutting protocol rules (apply to every endpoint)

### 1.1 Authentication `[LOCKED]` (PRD SESSION-001/004, PHONE-003)
- Every non-public endpoint requires a **short-lived access token** in `Authorization: Bearer <access_token>`.
- The server **derives identity from the token**, never from any `user_id`/`graph_id`/`session_id` in the request body (PHONE-003). A body that contains those fields has them treated as *claims to validate*, never as authorization.
- Token refresh uses the **long-lived device credential** (SESSION-001), via a dedicated endpoint (§3), not a full re-login.
- Public endpoints (no token): only the OIDC start/callback and health (`§2`, `§12`).

### 1.2 Authorization `[LOCKED]` (PRD SESSION-004, RAUTH-002)
Every resource-touching endpoint runs the deterministic chain before doing work:
```
valid token → device (not revoked) → user → session (not expired)
→ graph membership (if graph-scoped) → resource visibility (RAUTH-002)
→ capability (if an action) → absolute-floor check (PERM-006)
```
Failure at any step → the appropriate error (§1.6), an `AuditEvent` (`01` §11.1), and **no side effect**.

### 1.3 request_id `[LOCKED]`
- Every request carries/receives an `X-Request-Id` (client-supplied UUID accepted; server generates if absent).
- It correlates the request across `AuditEvent`, `PermissionDecision`, and `UsageEvent` (`01` §11) — the single thread for tracing one request end to end.

### 1.4 Idempotency `[IMPL, constrained]`
- All **state-changing** endpoints (POST/PUT/DELETE that create/modify) accept an `Idempotency-Key` header; a repeat with the same key returns the original result without re-executing. Required at minimum for: device registration, graph creation, membership grant, scheduled-job creation, agent task submission. `[IMPL]` storage of idempotency keys, but the guarantee is not optional for those endpoints (prevents duplicate charges/actions on client retry after FAIL-001).

### 1.5 Versioning `[LOCKED]`
- All endpoints are namespaced `/api/v1/...`. Breaking changes require `/api/v2`. Clients send `X-Client-Version`; the server may warn (not block) on an unsupported client in the pilot.

### 1.6 Error envelope `[LOCKED]`
Uniform error body, so the client renders **explicit failure states** (PRD §29A FAIL-CORE-001 — never silent, never fake success):
```json
{
  "error": {
    "code": "string (stable machine code, see §1.7)",
    "message": "human-readable, safe to show — NEVER contains secrets or stack traces (SECRET-004)",
    "request_id": "uuid",
    "retryable": true,
    "details": {}
  }
}
```
- `retryable` tells the client whether a retry could succeed (transient) vs. not (authz denial, validation).
- **No secret material, no internal stack trace, no other user's data** ever appears in an error (SECRET-004, DASH-005 spirit). Security failures return a generic code (§1.7) — they do not leak *why* in a way that aids probing.

### 1.7 Canonical error codes `[LOCKED]`
| HTTP | code | Meaning | retryable |
|---|---|---|---|
| 401 | `unauthenticated` | Missing/invalid/expired token | no (refresh first) |
| 401 | `token_expired` | Access token expired — refresh via device credential | no (→ refresh) |
| 403 | `unauthorized` | AuthZ chain failed (membership/visibility/capability). **Deliberately generic** — does not distinguish "not a member" from "resource is private" (anti-probing) | no |
| 403 | `confirmation_required` | Action needs human confirmation (PERM-004) — carries a `confirmation_token` in `details` | no (→ confirm) |
| 403 | `prohibited` | Absolute-floor action (PERM-006) — never allowed | no |
| 404 | `not_found` | Resource absent **or** not visible to caller (merged with 403-private, anti-enumeration) | no |
| 409 | `conflict` | Idempotency/version/state conflict | no |
| 422 | `validation_failed` | Body fails the `01` schema | no |
| 429 | `rate_limited` | Rate/budget limit hit (RATE-001) — carries `retry_after` | yes (after delay) |
| 503 | `dependency_unavailable` | A backend (model/tool/Mem0/Vault/scheduler/secret/intelligence) is down (§29A) — carries which class | yes |
| 500 | `internal_error` | Unexpected — generic, no detail leaked | maybe |

**Rule `[LOCKED]`:** a security-relevant denial NEVER reveals whether a resource exists but is private vs. doesn't exist — both return `not_found` (prevents cross-user enumeration, SEC-Q/R).

### 1.8 Pagination `[IMPL, constrained]`
List endpoints use cursor pagination: `?limit=<n>&cursor=<opaque>` → `{ items: [], next_cursor: "opaque|null" }`. `limit` is capped server-side (RATE-001 spirit — no unbounded fetch).

### 1.9 Streaming `[IMPL, constrained]`
Agent responses MAY stream (SSE or chunked). A streamed response still: carries the `request_id`; can terminate in an error envelope mid-stream (client must handle a stream that ends in failure — FAIL-005); and emits its `UsageEvent` on completion (or partial on failure). Non-streaming is an acceptable MVP default; streaming is an enhancement.

### 1.10 Metering `[LOCKED]` (PRD USAGE-001)
Every endpoint that triggers a model call or tool execution emits a `UsageEvent` (`01` §11.2) before returning. This is not optional — limits are enforced against this ledger (USAGE-002).

---

## 2. Standard request lifecycle (the shape every action endpoint follows)

```
1. Terminate TLS at tunnel → FastAPI (transport only, not authz — §18)
2. Attach/generate X-Request-Id
3. Authenticate: validate access token → resolve device/user/session   [§1.1]
   └ fail → 401, AuditEvent, stop
4. Rate/budget precheck (RATE-001)                                       [§1.10]
   └ fail → 429, stop
5. Validate body against 01 schema                                       [§1.6]
   └ fail → 422, stop
6. Authorize: membership → visibility → capability → absolute-floor      [§1.2]
   └ deny → 403/404, PermissionDecision + AuditEvent, stop
   └ require_confirmation → 403 confirmation_required, stop until confirmed
7. Execute (may invoke agent/tool/model/store)
   └ dependency down → 503 dependency_unavailable (§29A), stop
8. Emit UsageEvent (if model/tool used) + AuditEvent(result)
9. Persist relevant state (GRAPH-004)
10. Return result (or stream), with X-Request-Id
```

`[LOCKED]` Steps 3, 6, 8 are non-skippable for any resource-touching endpoint. An endpoint that mutates without an AuditEvent is a defect.

---

## 3. Endpoints — Auth / Identity / Session (`03` realizes these)

| Method · Path | Auth | Purpose | Request → Response | Key errors |
|---|---|---|---|---|
| `GET /api/v1/auth/oidc/start` | public | Begin Google OIDC (AUTH-001); returns redirect w/ state+nonce+PKCE (AUTH-005) | `{redirect_url}` | 503 |
| `GET /api/v1/auth/oidc/callback` | public | OIDC callback; validate issuer/audience/state/nonce/PKCE; map `sub`→User (AUTH-004/005) | code/state → `{needs_device_registration: bool, bootstrap_token}` | 401 `unauthenticated` (bad state/nonce) |
| `POST /api/v1/devices` | bootstrap_token | Register a device (DEVICE-001); issues long-lived device credential (stored client-side securely, PHONE-004) | `{platform}` → `{device_id, device_credential}` (credential returned **once**, never re-fetchable, never logged — SECRET-004) | 409 (idempotency), 422 |
| `POST /api/v1/sessions/token` | device credential | Exchange/refresh → short-lived access token (SESSION-001) without full re-login | `{device_credential}` → `{access_token, expires_at}` | 401 (revoked device → `unauthorized`), 503 |
| `POST /api/v1/sessions/logout` | Bearer | End current session | — → `204` | 401 |
| `DELETE /api/v1/devices/{device_id}` | Bearer (owner) | Revoke a device (lost-phone, SESSION-002); invalidates its credential remotely | — → `204` | 403, 404 |
| `DELETE /api/v1/account` | Bearer + fresh-auth | Account deletion → cascades graphs/memories/files/tasks/devices/tokens/creds/caches (LIFE-003) | — → `202` (async purge) | 403 |

`[LOCKED]` The device credential value is returned exactly once at registration, never retrievable again, never in any log/audit/usage record. A lost credential is handled by revoke+re-register, not by re-fetch.

---

## 4. Endpoints — Graph & Membership (`04` enforces authorization)

| Method · Path | Auth | Purpose | Request → Response | Key errors |
|---|---|---|---|---|
| `POST /api/v1/graphs` | Bearer | Create a graph (GRAPH-007); creator = owner | `{name, type:"private"\|"shared"}` → `Graph` | 422, 429 |
| `GET /api/v1/graphs` | Bearer | List graphs the user is a member of (ENT-002) | — → `{items:[Graph], next_cursor}` | — |
| `POST /api/v1/graphs/{graph_id}/access-requests` | Bearer | Request access to a shared graph (GRAPH-008) | `{message?}` → `{request_id, status:"pending"}` | 404 |
| `POST /api/v1/graphs/{graph_id}/members` | Bearer (owner/authorized) | Approve a member (GRAPH-008); creates `GraphMembership` | `{user_id, role:"member"}` → `GraphMembership` | 403, 409 |
| `DELETE /api/v1/graphs/{graph_id}/members/{user_id}` | Bearer (owner) | Revoke membership | — → `204` | 403, 404 |
| `POST /api/v1/sessions/active-graph` | Bearer | Switch the session's active graph (GRAPH-006) | `{graph_id}` → `{session_id, active_graph_id}` | 403 (not a member), 404 |

`[LOCKED]` Every graph endpoint re-checks membership server-side (PHONE-003) — a client asserting `graph_id` it isn't a member of gets `404` (anti-enumeration), never the graph.

---

## 5. Endpoints — Agent Runtime (`05` implements the loop)

| Method · Path | Auth | Purpose | Request → Response | Key errors |
|---|---|---|---|---|
| `POST /api/v1/agent/tasks` | Bearer + active graph | Submit a user request to the primary agent (GRAPH-004). May stream (§1.9). Idempotency-Key required. | `{input, stream?:bool}` → `AgentResult` or SSE stream | 403 `confirmation_required` (if the plan hits a consequential action), 429 (loop/budget), 503 (model down FAIL-005) |
| `POST /api/v1/agent/tasks/{task_id}/confirm` | Bearer | Provide human confirmation for a pending consequential/irreversible action (PERM-004) | `{confirmation_token, approve:bool}` → resumes or aborts | 403 `prohibited` (if somehow an absolute-floor action — should never reach here), 409 |
| `POST /api/v1/agent/tasks/{task_id}/cancel` | Bearer | Cancel a running task (runaway/user abort) | — → `204` | 404 |
| `GET /api/v1/agent/tasks/{task_id}` | Bearer (owner) | Poll task status/result | — → `AgentResult{status}` | 403, 404 |

`[LOCKED]` The agent endpoint returns a **proposal-executed-under-authorization** result, never raw model execution authority (P1). If the agent's plan includes a consequential action, the API returns `confirmation_required` and does **not** perform it until `/confirm` (PERM-004). An absolute-floor action (PERM-006) is never surfaced as confirmable — it returns `prohibited`.

---

## 6. Endpoints — Tools, Models, Capabilities (`06`/`07`/`08`)

Tools are invoked **by the agent, internally**, not directly by the client — the client submits a task (§5) and the agent proposes tool calls that the deterministic layer authorizes. The client-facing tool surface is limited to configuration and capability management:

| Method · Path | Auth | Purpose | Request → Response |
|---|---|---|---|
| `GET /api/v1/config/tools` | Bearer | List tools available/enabled for the resolved config (`ToolConfiguration`) | → `{items:[ToolContract-summary]}` |
| `PUT /api/v1/config/agent` | Bearer (owner-of-scope) | Set primary model / model-tools / enabled tools for a user- or graph-scoped `AgentConfiguration` (MODEL-001) | `AgentConfiguration` → `AgentConfiguration` |
| `GET /api/v1/capabilities` | Bearer | List the caller's active `CapabilityGrant`s | → `{items:[CapabilityGrant]}` |
| `POST /api/v1/capabilities` | Bearer | Grant a capability (user consent, PERM-002) — e.g. per-app device capability from the phone's permission UI (§13) | `{capability, scope_type, scope_id, resource_scope, expires_at?}` → `CapabilityGrant` |
| `DELETE /api/v1/capabilities/{grant_id}` | Bearer | Revoke a capability | — → `204` |

`[LOCKED]` A `POST /capabilities` for an absolute-floor capability (PERM-006) returns `prohibited` and creates no grant (`01` §7.1 validation). Internal agent→tool and agent→model invocation contracts (not HTTP; in-process) are defined in `06`/`07`; each still passes through the authorization chain and emits AuditEvent+UsageEvent.

**Internal invocation contracts (not client-facing, defined here for completeness, detailed in `06`/`07`):**
- `agent → tool`: `invoke(tool_id, input)` → deterministic authorize (capability + boundaries) → execute in sandbox → `ToolResult`; emits UsageEvent(kind=tool_call).
- `agent → model-tool`: `invoke(model_tool_id, prompt)` → resolve `ModelConfiguration` (secret by handle) → provider call w/ timeout/retry → normalized result; emits UsageEvent(kind=model_call).
- `agent → IntelligenceProvider`: only if `enabled` (§11); else the capability simply isn't present (INTEL-003).

---

## 7. Endpoints — Memory (`11` enforces visibility)

| Method · Path | Auth | Purpose | Request → Response |
|---|---|---|---|
| `GET /api/v1/memory` | Bearer + active graph | Retrieve the caller's relevant memories (hydration is agent-internal; this is user-facing recall/visibility, MEM-003) | `?query=&limit=` → `{items:[Mem0Fact]}` **visibility-filtered (RAUTH-003)** |
| `POST /api/v1/memory` | Bearer | Explicitly add a memory (default `visibility:private`) | `{fact_type, content, graph_id}` → `Mem0Fact` |
| `PATCH /api/v1/memory/{fact_id}` | Bearer (owner only) | Correct a memory, or change `visibility` private↔graph (owner-only, audited — RAUTH V2) | `{content?, visibility?}` → `Mem0Fact` |
| `DELETE /api/v1/memory/{fact_id}` | Bearer (owner only) | Delete a memory (MEM-003) | — → `204` |

`[LOCKED]` `GET /memory` returns only facts where `visibility==graph` OR `owner_user_id==caller` (RAUTH-003) — being in the graph is not enough to see another user's private facts. A `fact_type` outside the enum on POST → `422` (EMO-002). Changing visibility to `graph` emits an AuditEvent (RAUTH V2).

---

## 8. Endpoints — Knowledge Vault (`11`)

| Method · Path | Auth | Purpose | Request → Response |
|---|---|---|---|
| `GET /api/v1/vault/query` | Bearer | RAG query over static shared knowledge (VAULT-002) | `?domain=&question=&top_k=` → `VaultQueryResponse` |
| `POST /api/v1/vault/documents` | Bearer (curator, VAULT-004) | Add/index vault content (who-may-curate is `[IMPL]`/owner-defined) | `{source_file, domain, content}` → `{doc_id}` |

`[LOCKED]` Vault queries never touch Mem0's collection (VAULT-003, distinct client). Vault content carries no visibility triplet (shared-curated by construction) and grants no credentials/authorization (VAULT-004).

---

## 9. Endpoints — Scheduler (`SCHED`)

| Method · Path | Auth | Purpose | Request → Response |
|---|---|---|---|
| `POST /api/v1/jobs` | Bearer | Create a task-linked reminder (SCHED-001) | `{task_reason (required, non-empty), schedule, graph_id?}` → `ScheduledJob` |
| `GET /api/v1/jobs` | Bearer | List caller's jobs (visibility-filtered) | → `{items:[ScheduledJob]}` |
| `DELETE /api/v1/jobs/{job_id}` | Bearer (owner) | Cancel a job | — → `204` |

`[LOCKED]` Empty `task_reason` → `422` (SCHED-001, no unprompted proactivity). Job creation counts against the per-user scheduler limit → `429` on breach (RATE-001, FAIL-010).

---

## 10. Endpoints — Voice (`27` / `VoiceEvent`)

| Method · Path | Auth | Purpose | Request → Response |
|---|---|---|---|
| `POST /api/v1/voice/transcribe` | Bearer | STT: audio → transcript; audio **not retained by default** (LIFE-002) | audio → `VoiceEvent{transcript, audio_retained:false}` |
| `POST /api/v1/voice/synthesize` | Bearer | TTS: text → audio | `{text}` → audio |

`[LOCKED]` Any `SpeakerContext` produced is context only — `is_authorization_signal` is structurally false (VOICE-002, INV-14). The transcribe endpoint never persists raw audio unless the user explicitly enabled retention (LIFE-002).

---

## 11. IntelligenceProvider endpoint (disabled in MVP)

| Method · Path | Auth | Purpose | Response |
|---|---|---|---|
| `GET /api/v1/intelligence/status` | Bearer | Report whether an intelligence provider is enabled | `{enabled: false}` (MVP default) |

`[LOCKED]` MVP returns `enabled:false` and Track B functions fully (INTEL-003, PRD §39 #24). No `query`/`execute` intelligence endpoints are exposed while disabled — the capability is absent, not present-but-erroring (P3). When later enabled, the agent reaches the provider through the internal contract (§6), gated by `health()`; if enabled-but-down → `503 dependency_unavailable` (FAIL-011), core agent continues.

---

## 12. Endpoints — Dashboard & Health (`28` security applies)

| Method · Path | Auth | Purpose |
|---|---|---|
| `GET /api/v1/health` | public | Liveness (no data, no auth) → `{status}` |
| `GET /api/v1/admin/*` | **superuser** (DASH-003/004) | Observability surfaces (devices/users/graphs/sessions/audit/usage/health) |

`[LOCKED]` `/admin/*` requires a **superuser principal**, distinct from any normal user session (DASH-004, SUPER-001) — an ordinary Bearer token cannot reach it. Admin responses are **secret-free** (DASH-005) and **PII-redacted by default** (DASH-006); viewing unredacted user content is a separate privileged, audited action. `/health` is the only data-free public endpoint besides OIDC start/callback.

---

## 13. Failure-state mapping (client contract, ties §29A)

The client renders each 503 `dependency_unavailable` with the specific `details.dependency` so the user sees an honest state, never fake success (FAIL-CORE-001):

| `details.dependency` | Client shows | PRD |
|---|---|---|
| `model` | "Couldn't get a model response" | FAIL-005 |
| `tool` | the specific tool failure | FAIL-006 |
| `mem0` | "long-term memory temporarily unavailable" (proceeds on session context) | FAIL-008 |
| `vault` | "answering without curated knowledge" | FAIL-009 |
| `scheduler` | "couldn't set/manage reminder" | FAIL-010 |
| `secret` | operation blocked (fail-closed, FAIL-CORE-003) | FAIL-012 |
| `intelligence` | (only if enabled) "specialized capability unavailable"; core continues | FAIL-011 |

`[LOCKED]` A **security-control** dependency failure (secret, policy) fails closed → the operation is denied, not degraded to permissive (FAIL-CORE-003). A **non-security** dependency failure degrades gracefully with an explicit message.

---

## 14. New open items surfaced

| ID | Question | Owner |
|---|---|---|
| OD-API-1 | Streaming transport choice (SSE vs chunked vs websocket) for agent responses — `[IMPL]`, but pick before client work | engineer, non-blocking |
| OD-API-2 | Exact idempotency-key retention window | engineer, non-blocking |

No new *architectural* open decisions — this doc realizes PRD contracts; both items are `[IMPL]` details.

---

## 15. Acceptance hooks (for `17`)

- **API-T1** every non-public endpoint rejects a request with no/invalid/expired token (`401`).
- **API-T2** a body asserting a `user_id`/`graph_id` the token doesn't own is ignored; identity is token-derived (PHONE-003).
- **API-T3** a request for another user's private resource returns `404`, not the resource, and not a distinguishable "exists but forbidden" (anti-enumeration).
- **API-T4** a consequential action returns `confirmation_required` and does not execute until `/confirm` (PERM-004).
- **API-T5** an absolute-floor action / capability grant returns `prohibited` and creates no side effect (PERM-006).
- **API-T6** every model/tool-invoking call emits exactly one UsageEvent (USAGE-001).
- **API-T7** no error body contains a secret, stack trace, or another user's data (SECRET-004).
- **API-T8** `/admin/*` is unreachable with an ordinary user token (DASH-004).
- **API-T9** a retried state-changing call with the same `Idempotency-Key` does not double-execute.
- **API-T10** each dependency-down case returns `503` with the correct `dependency` and the client shows an explicit failure state (§29A).

---

*End of 02_API_PROTOCOL. Next: `03_AUTH_IDENTITY_SESSION.md` — the OIDC flow, device-credential and token lifecycle that these endpoints depend on. Changes here propagate to 03, 05, 06, 07, 13, 17.*
