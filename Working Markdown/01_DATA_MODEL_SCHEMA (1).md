# 01_DATA_MODEL_SCHEMA.md
## Hypermind Track B — Data Model & Schema Specification

**Package:** subsystem doc 01 of 17 · **Depth:** deep · **Status:** implementation contract
**Authority:** subordinate to `00_CANONICAL_PRD` (`Track_B_PRD_Refined.md`). Where a field or rule here elaborates a PRD contract, the PRD requirement ID is cited; this document may not contradict the PRD (source-of-truth rule). Every schema in PRD §26 is defined here at implementation grade.
**Consumed by:** every other subsystem doc (`02`–`17`) references these schemas. Build this first.

**Label legend** (same as PRD): `[LOCKED]` fixed · `[IMPL]` engineer chooses within the constraint · `[FUTURE]` deferred · `[OPEN — OWNER]` unresolved.

---

## 0. How to read this document

Each entity is given as: **purpose · storage scope · fields table · keys/indexes · relationships · validation rules · visibility/authorization notes**. The fields table columns are: **Field · Type · Req · Constraint · Notes**. Types are storage-neutral (`uuid`, `string`, `int`, `float`, `bool`, `timestamp` (ISO-8601 UTC), `enum(...)`, `json`, `ref(Entity.field)`); the concrete DB/ORM is `[IMPL]` (see §14 storage-scoping and OD-A1 interaction).

Two cross-cutting field families appear on many entities and are defined once (§1) then referenced:
- **Provenance-lite** (`created_at`, and where relevant `created_by`) — audit/trace basics.
- **Visibility triplet** (`visibility`, `owner_user_id`, `source_user_id`) — the RAUTH-003 private-vs-graph-shared model. Any user-generated resource that can live in a shared graph carries it.

---

## 1. Cross-cutting field definitions (defined once, referenced everywhere)

### 1.1 Visibility triplet `[LOCKED]` (PRD RAUTH-003/004/005)

| Field | Type | Req | Constraint | Notes |
|---|---|---|---|---|
| `visibility` | enum(`private`,`graph`) | Yes | default `private` | `private` = readable only by `owner_user_id`; `graph` = readable by members of `graph_id`. Default is always `private` (RAUTH-005). |
| `owner_user_id` | ref(User.user_id) | Yes | — | The principal who controls visibility. Only the owner may change `visibility` or delete. |
| `source_user_id` | ref(User.user_id) | Yes | — | Who produced the content (provenance). Usually equals `owner_user_id`; kept separate for audit and for content generated on another user's behalf. |

**Rule V1 `[LOCKED]`:** presence of a `graph_id` on a resource **never** implies readability by graph members. Readability requires `visibility == graph` OR requester `== owner_user_id` (RAUTH-002). Any entity carrying `graph_id` + user content MUST carry the visibility triplet; `graph_id` alone is not an authorization field.

**Rule V2 `[LOCKED]`:** changing `visibility` from `private` → `graph` is an explicit, owner-only, audited action (emits an `AuditEvent`, §11). It is never a side effect of any other operation.

### 1.2 Enum registry `[LOCKED]`

Central list so no enum drifts across docs. An enum value not in this registry is invalid.

| Enum | Values |
|---|---|
| `visibility` | `private`, `graph` |
| `user.status` | `active`, `suspended`, `deleted` |
| `device.platform` | `android` (MVP; others `[FUTURE]`) |
| `graph.type` | `private`, `shared` |
| `membership.role` | `owner`, `member` (MVP; richer roles OD-E1 `[FUTURE]`) |
| `fact_type` | `preference`, `past_request`, `stated_goal` |
| `agentconfig.scope_type` | `user`, `graph` |
| `capability.scope_type` | `user`, `graph`, `device`, `session`, `task` |
| `permission.decision` | `allow`, `deny`, `require_confirmation` |
| `risk_category` | `low_read`, `low_write`, `consequential`, `high_irreversible` |
| `secret.class` | `model_api_key`, `oauth_token`, `device_credential`, `master_key`, `other` |
| `secret.owner_scope_type` | `user`, `graph`, `server` |
| `audit.actor` | `user`, `agent`, `tool`, `system`, `superuser` |
| `audit.result` | `success`, `failure`, `blocked` |
| `usage.kind` | `model_call`, `tool_call` |
| `model.provider` | `ollama`, `openai`, `anthropic`, `gemini`, `deepseek`, `groq`, `openai_compatible`, `custom` |
| `job.status` | `active`, `cancelled`, `fired` |
| `tool.risk_category` | same as `risk_category` |

---

## 2. Identity entities

### 2.1 User `[LOCKED]` (PRD ENT-001, AUTH-004/005)

**Purpose:** the authenticated principal everything attributes to. **Storage scope:** server-global (one row per human).

| Field | Type | Req | Constraint | Notes |
|---|---|---|---|---|
| `user_id` | uuid | Yes | PK | Hypermind-owned internal id — never the Google id. |
| `oidc_subject` | string | Yes | unique | Stable Google `sub` (AUTH-005). The join key from OIDC → user. **Never** email (email is mutable). |
| `oidc_issuer` | string | Yes | — | e.g. Google issuer URL; supports future multi-provider (AUTH-001). |
| `display_name` | string | No | — | Non-authoritative label for UI/dashboard. |
| `status` | enum(user.status) | Yes | default `active` | `deleted` triggers lifecycle purge (PRD LIFE-003). |
| `created_at` | timestamp | Yes | — | |

**Keys/indexes:** PK `user_id`; unique index `(oidc_issuer, oidc_subject)` — the pair, not subject alone, so two providers can't collide.
**Relationships:** 1→N `Device`, `GraphMembership`, `AgentConfiguration`; referenced as `owner_user_id`/`source_user_id` by all visibility-bearing resources.
**Validation:** `oidc_subject` immutable after creation. `status = deleted` rows are retained only as tombstones during purge, then removed per LIFE-003.

### 2.2 Device `[LOCKED]` (PRD DEVICE-001, SESSION-002)

**Purpose:** a registered client bound to a user, with its own revocable credential. **Storage scope:** server-global.

| Field | Type | Req | Constraint | Notes |
|---|---|---|---|---|
| `device_id` | uuid | Yes | PK | |
| `user_id` | ref(User.user_id) | Yes | FK | Owning user. |
| `platform` | enum(device.platform) | Yes | — | `android` for MVP. |
| `credential_ref` | ref(SecretReference.secret_ref) | Yes | — | Handle to the long-lived device credential in the SecretStore — **never the credential value** (SECRET-002). |
| `registered_at` | timestamp | Yes | — | |
| `last_seen` | timestamp | No | — | Updated on activity; supports revocation UX. |
| `revoked` | bool | Yes | default `false` | Set true on lost-device revocation (PRD §39 #4). |
| `revoked_at` | timestamp | No | — | |

**Keys/indexes:** PK `device_id`; index `user_id`.
**Relationships:** N→1 `User`; 1→N `Session`.
**Validation:** a request presenting a `credential_ref` whose Device is `revoked` is rejected at auth (see `03`). `credential_ref` value never stored inline.

### 2.3 Session `[LOCKED]` (PRD SESSION-001)

**Purpose:** a bounded interaction with a short-lived token. **Storage scope:** server-global (may be ephemeral/expiring store).

| Field | Type | Req | Constraint | Notes |
|---|---|---|---|---|
| `session_id` | uuid | Yes | PK | |
| `device_id` | ref(Device.device_id) | Yes | FK | |
| `user_id` | ref(User.user_id) | Yes | FK | Denormalized for fast authZ; must match `Device.user_id`. |
| `active_graph_id` | ref(Graph.graph_id) | No | — | The graph the session is currently operating in (graph switching, §11 GRAPH-006). Null before a graph is selected. |
| `issued_at` | timestamp | Yes | — | |
| `expires_at` | timestamp | Yes | — | Short-lived (SESSION-001); refresh via device credential, not re-login. |
| `scope` | json (list of string) | No | — | Optional narrowed scope for the session. |

**Keys/indexes:** PK `session_id`; index `device_id`, `user_id`.
**Validation:** `user_id` MUST equal the owning `Device.user_id` (integrity check, not client-asserted — PRD PHONE-003). Expired sessions are non-authoritative; `active_graph_id` must reference a graph the user is a member of at use time (re-checked live, not trusted from the row).

---

## 3. Context entities

### 3.1 Graph `[LOCKED]` (PRD GRAPH-001..009, §11A)

**Purpose:** the first-class authorization boundary + context substrate. **Storage scope:** server-global.

| Field | Type | Req | Constraint | Notes |
|---|---|---|---|---|
| `graph_id` | uuid | Yes | PK | |
| `name` | string | Yes | — | |
| `owner_user_id` | ref(User.user_id) | Yes | FK | The creator/owner (can approve members, GRAPH-008). |
| `type` | enum(graph.type) | Yes | — | `private` (single-user) or `shared`. |
| `created_at` | timestamp | Yes | — | |

**Keys/indexes:** PK `graph_id`; index `owner_user_id`.
**Relationships:** 1→N `GraphMembership`; scopes `Mem0Fact`, `FileResource`, `ScheduledJob`, `AgentConfiguration` (graph-scoped ones).
**Validation:** a `private` graph has exactly one membership (its owner); a `shared` graph has ≥1. Deleting a graph cascades per LIFE-003 (memories/files/tasks/memberships scoped to it).

### 3.2 GraphMembership `[LOCKED]` (PRD GRAPH-007, RAUTH-001)

**Purpose:** the membership + role dimension of authorization. **Storage scope:** server-global.

| Field | Type | Req | Constraint | Notes |
|---|---|---|---|---|
| `membership_id` | uuid | Yes | PK | |
| `graph_id` | ref(Graph.graph_id) | Yes | FK | |
| `user_id` | ref(User.user_id) | Yes | FK | |
| `role` | enum(membership.role) | Yes | — | `owner` or `member` (MVP). |
| `granted_by` | ref(User.user_id) | Yes | — | Who approved (GRAPH-008). |
| `granted_at` | timestamp | Yes | — | |
| `revoked_at` | timestamp | No | — | Null = active. |

**Keys/indexes:** PK `membership_id`; **unique index `(graph_id, user_id)` where `revoked_at IS NULL`** (a user has at most one active membership per graph); index `user_id` (list a user's graphs).
**Relationships:** N→1 `Graph`, `User`.
**Validation `[LOCKED]`:** membership is the *first* of five authorization dimensions (RAUTH-001) — being present here is **necessary but not sufficient** to read any specific resource (that also needs the visibility check, V1). Membership existence never implies resource visibility.

---

## 4. Runtime-configuration entity

### 4.1 AgentConfiguration `[LOCKED]` (PRD AGENT-001, MODEL-001, §6)

**Purpose:** which primary model + model-tools + tools + capabilities apply, resolvable per user or per graph. **Storage scope:** server-global, scoped by `(scope_type, scope_id)`.

| Field | Type | Req | Constraint | Notes |
|---|---|---|---|---|
| `config_id` | uuid | Yes | PK | |
| `scope_type` | enum(agentconfig.scope_type) | Yes | — | `user` or `graph`. |
| `scope_id` | uuid | Yes | — | user_id or graph_id per `scope_type`. |
| `primary_model` | json (ModelConfiguration, §9.1) | Yes | — | The one primary agent model. `secret_ref` handle only. |
| `model_tools` | json (list of string) | No | — | ids of enabled LLM-as-tool entries (§9.2). |
| `enabled_tools` | json (list of string) | No | — | ids of enabled generic tools (ToolConfiguration, §9.3). |
| `granted_capabilities` | json (list of string) | No | — | capability strings active in this config (see §7 CapabilityGrant for the authoritative grant records; this is the resolved set). |
| `updated_at` | timestamp | Yes | — | |

**Keys/indexes:** PK `config_id`; unique index `(scope_type, scope_id)` — one active config per scope.
**Resolution rule `[IMPL, constrained]`:** when both a user-scope and graph-scope config exist for a request, the resolution precedence (graph overrides user, or user overrides graph) is `[IMPL]` but MUST be deterministic and documented in `05_AGENT_RUNTIME.md`; it is not decided by the model.
**Validation:** `primary_model` must reference a supported `model.provider`; secrets by handle only.

---

## 5. Memory & knowledge entities

### 5.1 Mem0Fact `[LOCKED]` (PRD MEM-001..006, RAUTH-003, EMO-002, LORA-002)

**Purpose:** persistent, task-relevant, visibility-aware long-term memory. **Storage scope:** Mem0 vector store + SQLite; ChromaDB collection `hypermind_memories` (distinct from vault, VAULT-003).

| Field | Type | Req | Constraint | Notes |
|---|---|---|---|---|
| `fact_id` | uuid | Yes | PK | |
| `owner_user_id` | ref(User.user_id) | Yes | visibility triplet | Controls visibility. |
| `source_user_id` | ref(User.user_id) | Yes | visibility triplet | Provenance. |
| `graph_id` | ref(Graph.graph_id) | Yes | FK | The graph context it was formed in. |
| `visibility` | enum(visibility) | Yes | default `private` | RAUTH-003: `private` even inside a shared graph unless owner shares it. |
| `fact_type` | enum(fact_type) | Yes | — | **Only** preference/past_request/stated_goal — the EMO-002 enforcement point. |
| `content` | string | Yes | **must not** contain relationship/emotional content (EMO-002) | Task-relevant only. |
| `timestamp` | timestamp | Yes | — | |
| `source_session_id` | ref(Session.session_id) | No | — | Where it originated. |
| `embedding_ref` | string | No | — | Vector-store handle `[IMPL]`. |

**Keys/indexes:** PK `fact_id`; index `(graph_id, owner_user_id)`; vector index on embedding.
**Validation `[LOCKED]`:** (a) `fact_type` enum is the structural guard against emotional content; a fact that would need a type outside the enum is rejected, not coerced. (b) Retrieval MUST apply the visibility filter (V1): a query by User B in graph X returns only facts where `visibility == graph` OR `owner_user_id == B`. (c) LORA-002: schema is LoRA-training-ready as-is; no rework needed later.

### 5.2 VaultDocument / VaultQuery `[LOCKED]` (PRD VAULT-001..005)

**Purpose:** static/shared curated knowledge, separate from Mem0. **Storage scope:** Git-backed markdown + ChromaDB collection `hypermind_vault` (distinct client + collection, VAULT-003).

VaultDocument (indexing side):

| Field | Type | Req | Constraint | Notes |
|---|---|---|---|---|
| `doc_id` | uuid | Yes | PK | |
| `source_file` | string | Yes | — | e.g. `vault/domain/topic.md`. |
| `domain` | string | Yes | — | Query filter key. |
| `chunk` | string | Yes | — | Indexed markdown chunk. |
| `embedding_ref` | string | No | — | |
| `curated_by` | ref(User.user_id) | No | — | Who may modify is `[IMPL]`/owner-defined (VAULT-004). |

VaultQueryResponse (read side, PRD §26): `{ results: [{ chunk, source_file, relevance_score }] }`.
**Validation `[LOCKED]`:** the vault collection name and client object are **distinct** from Mem0's in every file (VAULT-003) — a PR pointing both at one collection is merge-blocking. Vault content is not user memory and carries no visibility triplet (it is shared-curated by construction), and it grants no credentials/authorization (VAULT-004). MVP content excludes finance/health/education (VAULT-005).

---

## 6. Resource entities (visibility-aware)

### 6.1 FileResource `[LOCKED]` (PRD FS-001, RAUTH-003)

**Purpose:** a file inside a graph/user/task sandbox. **Storage scope:** sandboxed filesystem (see `09_FILESYSTEM_SANDBOX.md`); metadata row server-global.

| Field | Type | Req | Constraint | Notes |
|---|---|---|---|---|
| `file_id` | uuid | Yes | PK | |
| `owner_user_id` | ref(User.user_id) | Yes | visibility triplet | |
| `source_user_id` | ref(User.user_id) | Yes | visibility triplet | |
| `graph_id` | ref(Graph.graph_id) | No | — | Null for user-private (non-graph) files. |
| `visibility` | enum(visibility) | Yes | default `private` | |
| `sandbox_root` | string | Yes | — | The permitted root this file lives under (`09`). |
| `relative_path` | string | Yes | normalized, traversal-safe (`09`) | Never an absolute host path; never escapes `sandbox_root`. |
| `size_bytes` | int | Yes | ≤ configured max (`09`) | |
| `created_at` | timestamp | Yes | — | |

**Keys/indexes:** PK `file_id`; unique index `(sandbox_root, relative_path)`; index `(graph_id, owner_user_id)`.
**Validation `[LOCKED]`:** `relative_path` MUST pass path-normalization + traversal + symlink checks (`09` owns the algorithm); visibility filter V1 applies to reads. A file's physical location is derived from `sandbox_root + relative_path`, never from client-supplied absolute paths.

### 6.2 ScheduledJob `[LOCKED]` (PRD SCHED-001, RAUTH-003)

**Purpose:** a task-linked reminder (only). **Storage scope:** scheduler store (APScheduler) + metadata row.

| Field | Type | Req | Constraint | Notes |
|---|---|---|---|---|
| `job_id` | uuid | Yes | PK | |
| `owner_user_id` | ref(User.user_id) | Yes | visibility triplet | |
| `source_user_id` | ref(User.user_id) | Yes | visibility triplet | |
| `graph_id` | ref(Graph.graph_id) | No | — | |
| `visibility` | enum(visibility) | Yes | default `private` | |
| `task_reason` | string | Yes | **required, non-empty** (SCHED-001) | The user-given reason; a job with no user reason must not exist. |
| `schedule` | string | Yes | cron or ISO datetime | |
| `status` | enum(job.status) | Yes | default `active` | |
| `created_at` | timestamp | Yes | — | |

**Keys/indexes:** PK `job_id`; index `(owner_user_id, status)`.
**Validation `[LOCKED]`:** empty `task_reason` is rejected (SCHED-001 — no unprompted proactivity). Jobs count against the per-user scheduler rate limit (RATE-001) — creation over quota fails explicitly (FAIL-010).

---

## 7. Authorization & capability entities

### 7.1 CapabilityGrant `[LOCKED]` (PRD PERM-001/002, RAUTH-001 dimension 5)

**Purpose:** a granted capability — the authoritative record behind AgentConfiguration's resolved set. **Storage scope:** server-global.

| Field | Type | Req | Constraint | Notes |
|---|---|---|---|---|
| `grant_id` | uuid | Yes | PK | |
| `principal_id` | uuid | Yes | — | The user/device/session/graph/task the grant applies to (per `scope_type`). |
| `scope_type` | enum(capability.scope_type) | Yes | — | user/graph/device/session/task. |
| `capability` | string | Yes | e.g. `app.interact`, `file.read` | Maps to concrete operations per `07`/`08` (AND-006 — not universal CRUD). |
| `resource_scope` | json | No | — | Narrowing (e.g. which app, which sandbox root). |
| `granted_by` | ref(User.user_id) | Yes | — | The user who granted it (user consent, PERM-002). |
| `created_at` | timestamp | Yes | — | |
| `expires_at` | timestamp | No | — | Null = until revoked. |
| `revoked_at` | timestamp | No | — | Null = active. |

**Keys/indexes:** PK `grant_id`; index `(principal_id, scope_type, capability)` where active.
**Validation `[LOCKED]`:** a grant authorizes exactly the concrete operations its `capability` maps to (`07`/`08`), never broader by name. Absolute-floor capabilities (PERM-006) can never be granted — they are prohibited by absence, so no `CapabilityGrant` row for them can be created; attempting to create one is a hard error, not a stored-but-denied grant.

### 7.2 PermissionDecision `[LOCKED]` (PRD PERM-004/005 — the audited output of an authZ check)

**Purpose:** the recorded result of evaluating whether an operation is allowed / needs confirmation / denied. **Storage scope:** audit store (append-only).

| Field | Type | Req | Constraint | Notes |
|---|---|---|---|---|
| `decision_id` | uuid | Yes | PK | |
| `request_id` | uuid | Yes | — | Correlates to the originating request + AuditEvent. |
| `principal_id` | uuid | Yes | — | |
| `capability` | string | Yes | — | |
| `resource_ref` | string | Yes | — | What was being accessed. |
| `decision` | enum(permission.decision) | Yes | — | allow/deny/require_confirmation. |
| `risk_category` | enum(risk_category) | Yes | — | Drives confirmation strength (PERM-004). |
| `reason` | string | Yes | — | Machine-readable reason. |
| `timestamp` | timestamp | Yes | — | |

**Keys/indexes:** PK `decision_id`; index `request_id`, `principal_id`.
**Validation `[LOCKED]`:** the decision is produced by deterministic policy (PERM-005), never by model judgment. Append-only; never edited.

---

## 8. Secret entity (handle only)

### 8.1 SecretReference `[LOCKED]` (PRD SECRET-001/002, SUPER-001)

**Purpose:** a handle to secret material — **the value never appears in any schema, log, dashboard, or client** (SECRET-004, DASH-005, USAGE-003). **Storage scope:** metadata row server-global; the *value* lives only in the SecretStore (`12`).

| Field | Type | Req | Constraint | Notes |
|---|---|---|---|---|
| `secret_ref` | string | Yes | PK (handle) | The opaque handle used everywhere a secret is referenced. |
| `owner_scope_type` | enum(secret.owner_scope_type) | Yes | — | user/graph/server. |
| `owner_scope_id` | uuid | No | — | Null for `server` scope. |
| `class` | enum(secret.class) | Yes | — | model_api_key/oauth_token/device_credential/master_key/other. |
| `created_at` | timestamp | Yes | — | |
| `rotated_at` | timestamp | No | — | |
| `revoked_at` | timestamp | No | — | |

**Keys/indexes:** PK `secret_ref`; index `(owner_scope_type, owner_scope_id)`.
**Validation `[LOCKED]`:** (a) no field ever holds the secret value — only the handle + metadata. (b) `class = master_key` references are **never** resolvable by the agent or user tools (SUPER-001) — resolution is superuser/bootstrap-only (`12`). (c) The agent may reference a secret only by `secret_ref`; the deterministic layer resolves it at the boundary (SECRET-002). Graph sharing never shares a `SecretReference` (GRAPH-009).

---

## 9. Configuration entities

### 9.1 ModelConfiguration `[LOCKED]` (PRD MODEL-001..005)

| Field | Type | Req | Constraint | Notes |
|---|---|---|---|---|
| `provider` | enum(model.provider) | Yes | — | |
| `model` | string | Yes | — | Provider-specific model id. |
| `endpoint` | string | No | — | For self-hosted/openai_compatible. |
| `secret_ref` | ref(SecretReference.secret_ref) | No | — | Handle for API key; null for keyless local (ollama). |
| `timeout_seconds` | int | Yes | > 0 | |
| `retry` | json | No | — | Retry policy (`06`). |
| `generation_policy` | json | No | — | temperature/max_tokens/etc. |

**Validation:** `secret_ref` only — never an inline key (SECRET-004). Embedded in AgentConfiguration.primary_model and in model-tool entries.

### 9.2 (LLM-as-tool) — a model-tool is a ToolConfiguration (§9.3) whose underlying tool is a model invoker; its model settings are a ModelConfiguration. No separate schema; discovery/normalization contract lives in `06_MODEL_PROVIDER_LLM_TOOL.md` (MODELTOOL-001..004).

### 9.3 ToolConfiguration `[LOCKED]` (PRD TOOL-001..004)

**Purpose:** the enabled instance of a tool (native, MCP-adapter, or model-tool). Its *contract* is the ToolContract (§10); this is its *enabled configuration*.

| Field | Type | Req | Constraint | Notes |
|---|---|---|---|---|
| `tool_id` | string | Yes | PK (per config scope) | Must match a registered ToolContract.tool_id. |
| `enabled` | bool | Yes | default `false` | A tool runs only if enabled AND capability-granted AND boundaries satisfied (TOOL-003). |
| `config` | json | No | — | Tool-specific settings. |
| `secret_ref` | ref(SecretReference.secret_ref) | No | — | Handle if the tool needs a secret. |
| `overrides` | json | No | — | e.g. `{rate_limits, timeout_seconds}` overriding contract defaults. |

**Validation `[LOCKED]`:** an MCP-adapter tool is a ToolConfiguration like any other — protocol conformance grants no trust (TOOL-004); it still needs an enabled config, a ToolContract, a granted capability, and satisfied boundaries.

---

## 10. ToolContract `[LOCKED]` (PRD TOOL-002 — the machine-readable contract)

**Purpose:** the declared contract every tool must publish before it is callable. **Storage scope:** tool registry (config, `[IMPL]` git-backed recommended).

| Field | Type | Req | Constraint | Notes |
|---|---|---|---|---|
| `tool_id` | string | Yes | unique | |
| `version` | string | Yes | — | |
| `description` | string | Yes | — | |
| `input_schema` | json (JSON Schema) | Yes | — | |
| `output_schema` | json (JSON Schema) | Yes | — | |
| `required_capability` | string | Yes | — | Which capability gates it (§7). |
| `resource_scope` | json | No | — | |
| `network` | json | Yes | — | `{required, destinations[], internet, private_net, may_send_credentials}` (NET-002). |
| `filesystem` | json | Yes | — | `{roots[], read, write}` (`09`). |
| `risk_category` | enum(risk_category) | Yes | — | |
| `timeout_seconds` | int | Yes | > 0 | |
| `retry_policy` | json | No | — | |
| `rate_limits` | json | No | — | |
| `confirmation_required` | bool | Yes | — | Per PERM-004. |
| `failure_behavior` | string | Yes | — | |
| `audit` | string | Yes | — | Audit requirements. |

**Validation `[LOCKED]`:** a tool with no ToolContract cannot be registered or run (TOOL-003). `network`/`filesystem` declarations are enforced by the deterministic boundary layers (`09`,`10`), not trusted from the tool.

---

## 11. Observability & accounting entities

### 11.1 AuditEvent `[LOCKED]` (PRD §30 threat model, §44 — audit is load-bearing)

**Purpose:** the canonical security/audit record; the threat model depends on it. **Storage scope:** append-only audit store; secret-free (SECRET-004).

| Field | Type | Req | Constraint | Notes |
|---|---|---|---|---|
| `event_id` | uuid | Yes | PK | |
| `request_id` | uuid | Yes | — | Correlates a whole request across components. |
| `user_id` | ref(User.user_id) | No | — | Null for system events. |
| `device_id` | ref(Device.device_id) | No | — | |
| `session_id` | ref(Session.session_id) | No | — | |
| `graph_id` | ref(Graph.graph_id) | No | — | |
| `actor` | enum(audit.actor) | Yes | — | user/agent/tool/system/superuser. |
| `action` | string | Yes | — | |
| `resource` | string | Yes | — | |
| `decision` | enum(permission.decision) + `n/a` | Yes | — | |
| `result` | enum(audit.result) | Yes | — | success/failure/blocked. |
| `timestamp` | timestamp | Yes | — | |

**Keys/indexes:** PK `event_id`; index `request_id`, `(user_id, timestamp)`, `(graph_id, timestamp)`.
**Validation `[LOCKED]`:** append-only, never edited/deleted except by lifecycle purge (LIFE-001); never contains secret material; the agent cannot disable or write false audit entries (PERM-006 — disabling audit is an absolute-floor prohibition).

### 11.2 UsageEvent `[LOCKED]` (PRD USAGE-001..003 — the metering substrate)

**Purpose:** per-call metering that budget/rate limits are enforced against. **Storage scope:** usage ledger; secret-free.

| Field | Type | Req | Constraint | Notes |
|---|---|---|---|---|
| `usage_id` | uuid | Yes | PK | |
| `request_id` | uuid | Yes | — | |
| `user_id` | ref(User.user_id) | Yes | — | Attribution. |
| `device_id` | ref(Device.device_id) | No | — | |
| `session_id` | ref(Session.session_id) | No | — | |
| `graph_id` | ref(Graph.graph_id) | No | — | |
| `kind` | enum(usage.kind) | Yes | — | model_call/tool_call. |
| `provider` | string | No | — | For model calls. |
| `model` | string | No | — | |
| `tool_id` | string | No | — | For tool calls. |
| `tokens_or_units` | int | Yes | ≥ 0 | |
| `estimated_cost` | float | Yes | ≥ 0 | For paid-provider budget (USAGE-002). |
| `timestamp` | timestamp | Yes | — | |

**Keys/indexes:** PK `usage_id`; index `(user_id, timestamp)`, `(graph_id, timestamp)`.
**Validation `[LOCKED]`:** every model_call and tool_call emits exactly one UsageEvent (USAGE-001); budget totals are derived from this ledger, not estimated (USAGE-002); never contains secret material (USAGE-003).

---

## 12. Voice entities

### 12.1 VoiceEvent / SpeakerContext `[LOCKED]` (PRD VOICE-001..004, EMO-005)

VoiceEvent:

| Field | Type | Req | Constraint | Notes |
|---|---|---|---|---|
| `voice_event_id` | uuid | Yes | PK | |
| `session_id` | ref(Session.session_id) | Yes | — | |
| `transcript` | string | Yes | — | STT output. |
| `audio_retained` | bool | Yes | default `false` | LIFE-002: transcribe→process→delete unless user opts in. |
| `timestamp` | timestamp | Yes | — | |

SpeakerContext:

| Field | Type | Req | Constraint | Notes |
|---|---|---|---|---|
| `speaker_id` | string | Yes | — | Provider label — not a Hypermind user_id. |
| `confidence` | float | Yes | 0..1 | |
| `utterance` | string | Yes | — | |
| `timestamp` | timestamp | Yes | — | |
| `is_authorization_signal` | bool | Yes | **structurally always `false`** | VOICE-002/EMO-005: speaker identity is context only, never auth. |

**Validation `[LOCKED]`:** `audio_retained` defaults false; `is_authorization_signal` can never be true — any code path setting it true is a defect (INV-14). `speaker_id` MUST NOT be mapped to a `user_id` for authorization purposes; if surfaced to the agent it is context only.

---

## 13. Entity-relationship overview

```
User 1─────N Device 1─────N Session
  │                              │ active_graph_id (nullable)
  │ owner/member                 ▼
  ├──────N GraphMembership N─────1 Graph 1───N ┌ Mem0Fact      (visibility triplet)
  │                                            ├ FileResource  (visibility triplet)
  │ owner_user_id / source_user_id ────────────┼ ScheduledJob  (visibility triplet)
  │                                            └ AgentConfiguration (scope=graph)
  ├──────N AgentConfiguration (scope=user)
  ├──────N CapabilityGrant (principal_id)
  └──────N SecretReference (owner_scope=user)   [value lives only in SecretStore]

Per request:  AuditEvent + PermissionDecision + UsageEvent   (correlated by request_id)
Config:       ModelConfiguration ⊂ AgentConfiguration ;  ToolConfiguration ↔ ToolContract
Voice:        VoiceEvent, SpeakerContext (session-scoped; speaker ≠ auth)
Knowledge:    VaultDocument (shared-curated, no visibility triplet — shared by construction)
```

**Referential integrity rules `[LOCKED]`:**
- Every visibility-triplet resource (Mem0Fact, FileResource, ScheduledJob) MUST have valid `owner_user_id` + `source_user_id` and, if graph-scoped, a `graph_id` for a graph the owner is a member of.
- `Session.user_id` MUST equal `Device.user_id` (no cross-user session on a device).
- `GraphMembership (graph_id, user_id)` unique per active membership.
- No entity stores a secret value; only `secret_ref` handles.

---

## 14. Storage scoping rules (interacts with OD-A1)

- **STORE-001 `[LOCKED]`** Three logically distinct stores (matching PRD OD-04/08/11-equivalents and the Track B backends): (a) the relational/identity store (users/devices/sessions/graphs/memberships/configs/grants/secret-refs/audit/usage); (b) Mem0 (`hypermind_memories` Chroma collection + SQLite); (c) Knowledge Vault (`hypermind_vault` Chroma collection, git-backed). They are never the same collection/client (VAULT-003).
- **STORE-002 `[LOCKED]`** Per-user/per-graph separation of Mem0 content is enforced by the visibility filter at query time (V1), and is a testable acceptance requirement (PRD §39 #22, MEM-005) — not an assumed property.
- **STORE-003 `[OPEN — OWNER, OD-A1]`** Whether these stores provide *physical* cross-user isolation under application-level RCE on a single laptop is the open security question (PRD §31, INV-20). The data model supports *logical* isolation (visibility filters + scoping); the physical-isolation decision (separate processes/DBs per user vs shared) is OD-A1 and belongs to `14_SECURITY_BLAST_RADIUS.md`. **This document does not claim physical isolation.**
- **STORE-004 `[IMPL]`** Concrete DB choice (SQLite/Postgres/etc.) is an implementation decision, constrained by: must support the unique indexes above; must support the visibility-filter query pattern efficiently; must not require exposing secret values (SecretStore stays separate, `12`).

---

## 15. Validation & acceptance hooks (for `17_TEST_ACCEPTANCE_VALIDATION.md`)

The tests these schemas must pass (defined fully in `17`):
- **DM-T1** every enum value used anywhere is in the §1.2 registry.
- **DM-T2** a Mem0Fact / FileResource / ScheduledJob defaults to `visibility: private`; a graph member cannot read another user's `private` resource in the same graph (RAUTH-003; PRD §39 #22).
- **DM-T3** a `fact_type` outside the enum is rejected (EMO-002).
- **DM-T4** an empty `task_reason` ScheduledJob is rejected (SCHED-001).
- **DM-T5** no schema instance, log, dashboard view, or usage record contains a secret value (SECRET-004; PRD §39 #19).
- **DM-T6** a Session whose `user_id ≠ Device.user_id` is rejected.
- **DM-T7** a duplicate active `GraphMembership (graph_id, user_id)` is rejected.
- **DM-T8** `is_authorization_signal` cannot be set true (INV-14).
- **DM-T9** creating a CapabilityGrant for an absolute-floor capability (PERM-006) is a hard error.

---

## 16. Open items owned elsewhere

- **OD-A1** (physical cross-user isolation) → `14`. This doc provides logical isolation only.
- **OD-D1** (SecretStore key custody) → `12`. This doc references secrets by handle only.
- **OD-E1** (richer graph roles) → future; `membership.role` enum extends without schema break.
- Resolution precedence for user-vs-graph AgentConfiguration → `05` (must be deterministic).

---

*End of 01_DATA_MODEL_SCHEMA. Next in sequence: `02_API_PROTOCOL.md`. This document is the schema authority the rest of the package builds on; changes here propagate to 02, 04, 07, 11, 13, 14, 17 (per the PRD cross-propagation rule).*
