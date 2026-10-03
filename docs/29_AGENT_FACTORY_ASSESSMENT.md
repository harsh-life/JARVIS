# Agent Factory — Implementation Specification
## JARVIS / Hypermind Track B — document 29 (rewritten from the Agent Factory assessment)

**Written:** 2026-09-30 · **Audited against:** `harsh-life/JARVIS` `origin/main` @ `ddbb038` (PR #30; PostgreSQL store, Stage 5 Judge and console, scheduler, memory, voice, Android device hub all merged).
**Document status:** **`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]`**. The *technical design* is implementation-ready where marked. Nothing here is `[LOCKED]` unless it restates existing canonical text with its source.
**Ratified 2026-10-02 (owner instruction; `docs/DECISION_REGISTER.md` §2K):** OD-AF-2, 3, 4, 5, 7 and 8 at the §32 recommended values; PRD §22 carries the §15.8 amendment. OD-AF-1, 9 and 10 stay open, and everything else here stays a proposal.
**Ratified 2026-10-02 (owner; register §2L):** OD-AF-6 — the first external runtime is Browser Use under P2 — and OD-TOOL-3 — no MCP for it. The infrastructure mechanisms it needs, OD-AF-11…15, were ratified on 2026-10-02 at the register's recommendations (§2L).
**Authority:** below `Working Markdown/00_CANONICAL_PRD.md` and `docs/DECISION_REGISTER.md`. Consumes `04`, `05`, `07`, `13`, `18`–`24`, `27`, `28` and the code of record named in each section.

### Classification used throughout

| Label | Meaning |
|---|---|
| `[LOCKED]` | already canonical; restated with its source |
| `[IMPLEMENTATION-READY]` | exact design; can be coded now without changing canonical security. Provisional values follow the repository's established practice (register §2–§2G): *implemented recommendation*, pending the owner's signature, never "ratified by being implemented" |
| `[OWNER DECISION]` | must be ratified before the dependent work starts; the recommended v1 value is given separately |
| `[FUTURE]` | not an implementation target |

### Repository facts this specification is built on (verified in code)

| Fact | Where |
|---|---|
| Task modes `execute \| draft \| suggest \| observe`; `observe`/`draft`/`suggest` are capped at `low_read` **before** the engine is asked | `shared/schemas/agent.py::TaskMode`, `server/agent/modes.py::MODE_CEILING` |
| `Principal` requires non-null `user_id`, `device_id`, `session_id` | `shared/schemas/authorization.py::Principal` |
| Confirmation tokens bind `session_id` among other fields | `server/capabilities/confirmation.py` |
| `CapabilityScopeType ∈ {user, graph, device, session, task}` — no `agent` | `shared/schemas/enums.py` |
| Closed capability registry: `app.interact`, `device.read`, `device.ui_control`, `file.read`, `file.write`, `system.restricted`, `net.request`, `model.invoke`, `scheduler.create` | `server/capabilities/registry.py` |
| `net.request` has no grant-level scope keys; destinations come from operator `EgressPolicy` | `docs/CAPABILITY_MATRIX.md` §3.1 |
| The worker slot is `Worker = ModelProvider`; `ResolvedModels.chain` (OD-RT-3 precedence, then operator fallbacks) | `server/agent/ports.py` |
| Model factory implements `ollama` and OpenAI-compatible (`openai`, `deepseek`, `groq`, `openai_compatible`); `anthropic`, `gemini`, `custom` are refused | `server/models/factory.py` |
| The scheduler cannot import the runtime, tools, devices, authority, secrets, memory or Judge (import contract) | `pyproject.toml` contract "The scheduler never executes…" |
| No MCP client or server exists (release validation marks TL-T6 "N/A: no MCP") | `docs/RELEASE_VALIDATION.md` §C |
| No container or network-namespace isolation exists (`mount_isolated`, `netns_filtered` are recorded, not built) | `server/config/schema.py` |
| One server process; PostgreSQL is the supported store for PRD #32 | register §2I |
| Canonical argument hashing: `operation_key(...)` (sorted-key JSON, SHA-256) | `server/agent/recovery.py` |
| `usage_events` is a locked entity (`01`); Judge attribution avoided an enum change (OD-JDG-4 precedent) | `server/storage/models.py`, register §2G |
| Paid-call budgets default to `0.0` | register §2 (OD-02, OD-USE-1) |

---

## 1. Purpose

**The product behaviour this document makes explicit:**

```
USER DESCRIBES A REPEATED GOAL
 → JARVIS UNDERSTANDS IT                                  (worker LLM; data only)
 → JARVIS IDENTIFIES THE REQUIRED AGENT CLASS             (worker proposes; compiler verifies)
 → JARVIS SELECTS AN APPROVED MODEL AND RUNTIME           (deterministic selector)
 → DETERMINISTIC COMPILER BUILDS THE AUTHORIZATION ENVELOPE
 → USER APPROVES CREATION                                 (consequential confirmation)
 → JARVIS RUNS THE AGENT
 → ALL ACTIONS STILL PASS THROUGH JARVIS                  (04 on every call)
 → RESULT RETURNS TO THE OWNER                            (owner inbox only)
```

The user never needs to know which runtime was selected. Runtimes are replaceable infrastructure. JARVIS remains the authority. Darwin remains separate and future (§33).

### 1.1 The four things that are never conflated

| | Concept | What it is | Authority |
|---|---|---|---|
| A | **AgentDefinition** | persistent, owner-private, versioned specification of a reusable agent | none — a description |
| B | **Agentic model** | an LLM that proposes tool-using steps | none — proposals only |
| C | **Agent runtime** | the loop engine turning proposals into repeated steps (native JARVIS runtime first; external providers later) | none — an engine |
| D | **JARVIS authority** | identity (`03`), five-dimension authorization (`04`), capability registry, tiers, confirmation (`07`), floor, budgets (`13`), supervisor (`18`) | **the only authority** |

`[LOCKED]` invariant carried from the PRD's P1: *model output is never the security boundary*. Extended here as the central testable invariant:

> **NO MODEL OR AGENT RUNTIME CAN CREATE AUTHORITY.**

### 1.2 LLM-mediated agent provisioning

JARVIS uses one model (the *factory worker*: the task's resolved worker, OD-RT-3) to interpret the request. It then runs the created agent with a possibly different *agent model* chosen by the selector. Everything either model writes is **data**:
- prompts can shape behaviour;
- prompts never define permissions, tiers, graph ownership, identity, confirmation requirements, budgets, delegation, secrets, or endpoint authority.

---

## 2. User Experience

### 2.1 Canonical creation flow `[IMPLEMENTATION-READY]` (on-demand and reminder-tap agents)

User: *"Check my unread security advisories every morning, summarize anything critical, and put it in my JARVIS inbox."*

| Step | Actor | What happens | Code |
|---|---|---|---|
| 1 | user | sends the sentence as an ordinary `execute` task | `POST /api/v1/agent/tasks` |
| 2 | worker | activates `agent.define` (standing grant or confirmation, existing activation flow) and proposes `tool_call agent.define.compile` with an **AgentDraft** as arguments | `server/agent/runtime.py` |
| 3 | runtime | parses the proposal; the tool adapter validates the draft against the strict schema (`extra="forbid"`) | §9.2 |
| 4 | compiler | classifies → selects template → selects runtime and model → derives envelope → resolves trigger, memory, outputs, budget → returns an immutable **CompiledAgentSpec preview** with a `compile_id` | §9 |
| 5 | compiler | if classification is ambiguous or a required value is missing, returns `needs_clarification` with closed choices; the worker asks the user; nothing is guessed | §9.4 |
| 6 | worker | proposes `agent.define.create {compile_id}` | |
| 7 | engine | `agent.define.create` is `consequential` → task pauses; the confirmation card is **rendered by deterministic code from the compiled spec** (never from worker prose) | `23` §5.4 pattern |
| 8 | user | approves on an authenticated device | `POST /agent/tasks/{id}/confirm` |
| 9 | JARVIS | persists AgentDefinition v1 (status `active`), provisions the runtime-side agent (native: no-op), registers the trigger | §14 |
| 10 | JARVIS | reports in plain language what the agent can and cannot do, including *"it will remind you each morning; tap to run"* | §17 |

The user never picks a model, runtime, tool framework, capability, prompt template or scheduler mechanism. Each run afterwards is either:
- **on-demand** ("Run my advisory agent"), or
- **reminder-tap** (the scheduler reminds; the user taps; the run executes as the present user).

**Unattended** morning execution requires §15 and is `[OWNER DECISION]`-blocked.

### 2.2 What the user sees on the confirmation card (deterministic rendering)

```
Create agent "Security advisory digest" (v1)
  Does:        reads sources you allow, summarizes critical items
  Can use:     read the web from approved sites · read your JARVIS memory
  Cannot:      write files · send messages · control devices · create agents
  Runs:        when you ask, or when you tap the 7:00 reminder (Asia/Kolkata)
  Results go:  your JARVIS inbox only
  Budget:      ≤ $0.05 per run · ≤ $1.00 per month   (counts toward your budget)
  Engine:      JARVIS built-in runtime · model profile "general-agentic"
[Approve]  [Cancel]
```

The "Engine" line is shown for transparency. Changing it is never required.

---

## 3. Architecture

```
JARVIS Agent Factory
├── AgentDefinition store            (owner-private, versioned, immutable compiled specs)
├── AgentTemplate Registry           (repo-versioned YAML, operator-enabled subset)
├── AgentModelProfile Registry       (operator config over existing 06 model entries)
├── AgentRuntimeProfile Registry     (code-registered providers, operator-enabled)
├── AgentCompiler                    (deterministic; the only writer of CompiledAgentSpec)
├── AgentSelector                    (deterministic; explainable selection)
├── Agent Gateway
│   ├── Model Gateway                (model allowlist, budgets, metering, provider keys)
│   └── Tool Gateway                 (04 authorization, tiers, mode ceiling, envelope, confirmation)
├── Native Runtime Provider          (the existing JARVIS runtime, 05/18)
├── External Runtime Providers       [FUTURE / infrastructure-blocked]
│   ├── browser specialist           (Browser Use)
│   ├── coding specialist            (OpenHands)
│   ├── stateful specialist          (Letta)
│   └── other approved runtimes      (OpenAI Agents SDK / Google ADK / LangGraph / Pydantic AI / OpenClaw)
├── Scheduler / Delegation           (22 reminders now; StandingDelegation after the PRD amendment)
├── Memory                           (Mem0 via 21 hydration; agent notebook)
├── Judge                            (19; observes agent traces)
└── Endpoints                        (23/24; inbox, approvals, stop)
```

### 3.1 Package layout `[IMPLEMENTATION-READY]`

| Package | Contents | May import | Must not import |
|---|---|---|---|
| `shared/schemas/agent_factory.py` | every schema in §4–§9, §11–§15 | `shared.schemas.*` | `server.*` (existing contract) |
| `server/agents/registry/` | template, model-profile, runtime-profile loaders | `shared`, `server.config` | `server.secrets`, `server.capabilities.grants`, `server.capabilities.confirmation`, `server.agent`, `server.tools`, `server.execution`, `server.models` |
| `server/agents/compiler.py`, `selector.py`, `abilities.py` | pure deterministic logic | `shared`, `server.agents.registry` | same as above **plus** `sqlalchemy` (pure: tested without a DB) |
| `server/agents/service.py` | persistence, lifecycle, inbox | `shared`, `server.agents.*`, `server.storage` | `server.secrets`, `server.agent` runtime internals, `server.execution` |
| `server/agents/providers/native.py` | native provider: starts a task through the runtime port | `shared`, `server.agents.service` | `server.graph`, `server.capabilities` (authorization stays in the runtime/engine path), `server.secrets` |
| `server/agents/gateway/` | Model Gateway, Tool Gateway (Phase 3 in-process; Phase 6 network surface) | `shared`, `server.agents.service`, ports | `server.secrets` resolution (keys resolved only inside `server.models` adapters via the existing resolver) |
| `server/composition/agents.py` | wiring, the `agent.*` tool adapters | anything the composition root already may | — |

`[IMPLEMENTATION-READY]` new import-linter contracts:
1. **AF-C1:** `server.agents` never imports `server.secrets`.
2. **AF-C2:** `server.agents.compiler`, `server.agents.selector` and `server.agents.registry` import no DB, runtime, tool, model or execution module.
3. **AF-C3:** `server.agents.providers.*` never imports `server.graph` or `server.capabilities`.
4. **AF-C4:** `server.scheduler` still never imports `server.agents` (reminders carry an `agent_id` as data, §17).
5. **AF-C5:** `server.evaluation` never imports `server.agents.service` mutation paths (Judge is never an authority).
6. **AF-C6:** `server.graph` and `server.capabilities` never import `server.agents` (agent state and notebooks are never read by authorization code).

---

## 4. AgentDefinition

### 4.1 Schema `[IMPLEMENTATION-READY]`

`AgentDefinition` is the mutable head record. Every change produces a new immutable `CompiledAgentSpec` version.

| Field | Type | Set by | Mutable | Notes |
|---|---|---|---|---|
| `agent_id` | uuid | server | no | PK |
| `owner_user_id` | uuid | **principal** | no | never from a draft |
| `graph_id` | uuid | **principal's active graph, membership re-checked live** | no | an agent never moves graphs; a new graph = a new agent |
| `name` | str, 1–80 chars, printable | draft (sanitized) | via update | display only |
| `status` | `AgentStatus` (§14.1) | service | yes | |
| `current_version` | int ≥ 1 | service | yes (monotonic) | FK to `agent_spec_versions` |
| `visibility` | `private` (only value in v1) | server | no | `[LOCKED]` default-private (RAUTH-005). Graph-shared agents are `[FUTURE]` |
| `created_at`, `updated_at` | timestamptz | server | server | |
| `deleted_at` | timestamptz \| null | service | set once | tombstone kept for audit; spec versions purged per §22 |
| `created_from_task_id` | uuid \| null | server | no | provenance |
| `created_by_device_id` | uuid | principal | no | provenance |

`[LOCKED]` resource authorization: a new `ResourceType.AGENTDEFINITION` goes through `04` with owner/visibility dimensions exactly like `mem0fact`. Only the owner can read, update, run, pause or delete. Graph members see nothing (anti-enumeration `404`). This needs an owner-signed `01`/`04` enum addition (§28).

### 4.2 Relationship to other entities

```
AgentDefinition 1──N AgentSpecVersion (CompiledAgentSpec, immutable)
AgentDefinition 1──N AgentRun ──1 agent_task (existing agent_tasks row, native provider)
AgentDefinition 1──N AgentInboxItem
AgentDefinition 1──N AgentNotebookEntry
AgentDefinition 1──0..1 StandingDelegation (active)          [Phase 5]
AgentRun        1──N AgentRunUsage ──1 usage_events           (join; locked entity untouched)
```

---

## 5. AgentTemplate Registry

### 5.1 Responsibility

A template is the operator-approved *maximum* shape of a class of agent. The worker chooses among templates; it never invents one. A template is authority-*limiting*, never authority-*granting*: it caps an envelope, and the owner's live grants still apply on top.

### 5.2 Schema `[IMPLEMENTATION-READY]`

File: `server/agents/templates/<template_id>.yaml`, loaded at startup, schema-validated, fail-closed (an invalid template file stops startup; `15` §6 pattern).

| Field | Type | Meaning |
|---|---|---|
| `template_id` | slug `^[a-z][a-z0-9_]{2,40}$` | stable id |
| `version` | int ≥ 1 | bump on any change |
| `description` | str ≤ 400 | shown to the factory worker as the catalog entry |
| `task_tags` | list of `TaskTag` (closed enum, §5.4) | what the worker matches against |
| `abilities` | list of `AbilityName` (closed, §9.3) | the maximum abilities an agent of this class may request |
| `run_mode` | `TaskMode` | `observe` for read-only classes; `execute` only where writes are part of the class |
| `risk_ceiling` | `RiskCategory` | highest tier any operation of this agent may reach, applied *in addition to* the mode ceiling |
| `required_model_features` | list of `ModelFeature` (§6.2) | routing requirement |
| `preferred_runtime` | runtime_id | first choice |
| `fallback_runtimes` | list of runtime_id | ordered |
| `min_isolation` | `IsolationMode` (§7.2) | a runtime below this is never selected |
| `memory_policy` | `MemoryPolicy` (§16.2) | |
| `trigger_support` | list of `TriggerKind` (`on_demand`, `reminder`, `unattended`) | `unattended` usable only after §15 |
| `output_support` | list of `OutputKind` (`inbox` only in v1) | |
| `max_run_seconds` | int, 10–3600 | per-run wall clock (≤ `agent.bounds.wall_clock_timeout_seconds` for native) |
| `max_model_calls_per_run` | int, 1–64 | |
| `max_tool_calls_per_run` | int, 0–128 | |
| `default_budget_per_run` | decimal ≥ 0 | USD, capped by §10 |
| `default_budget_per_month` | decimal ≥ 0 | |
| `unattended_supported` | bool | template-level permission; still needs a StandingDelegation |
| `supported_endpoints` | list (`android`, `desktop`, `browser`) | where results and approvals render |
| `required_infrastructure` | list of `InfraRequirement` (`none`, `container`, `netns`, `mcp_server`, `browser_sandbox`) | selector filter |
| `provider_compatibility` | map runtime_id → notes | informational |
| `priority` | int | deterministic tie-break (higher first) |

### 5.3 Catalog lifecycle `[IMPLEMENTATION-READY]`

- **Shipped catalog:** templates live in the repository and are code-reviewed like capability registry entries. Adding one is a PR, not a runtime action. `server/agents/templates/README.md` documents the format and review checklist:
  - abilities ⊆ ability registry;
  - `risk_ceiling` justified;
  - `run_mode` justified;
  - tests added.
- **Enablement:** `agents.enabled_templates: [ids]`, default empty. Operators enable a subset.
- **Versioning:** a template version bump marks every `CompiledAgentSpec` built from an older version as `revalidation_required` (§14.3, AGENT-T25). It keeps running only if recompiling under the new version produces an envelope and runtime/model selection that are **equal or narrower**; otherwise the agent moves to `needs_reapproval`.
- The model **never** creates, edits or enables a template at runtime. No endpoint does either.

### 5.4 Closed vocabularies

`TaskTag` (v1): `research`, `monitoring`, `summarization`, `classification`, `extraction`, `document_processing`, `reporting`, `notification_prep`, `knowledge_maintenance`, `browser_automation`, `coding_maintenance`, `data_analysis`, `api_workflow`, `security_research`. Adding a tag is an enum PR.

### 5.5 v1 shipped templates `[IMPLEMENTATION-READY]` (enabled only by the operator)

| template_id | tags | abilities | run_mode | risk_ceiling | runtime | isolation | unattended |
|---|---|---|---|---|---|---|---|
| `research_digest` | research, summarization, reporting, security_research | `read_web_allowlisted`, `read_user_memory`, `read_vault` | observe | low_read | native | in_process | yes (template-level) |
| `web_monitor_basic` | monitoring, extraction | `read_web_allowlisted`, `read_agent_notebook`, `write_agent_notebook` | observe | low_read | native | in_process | yes |
| `knowledge_keeper` | knowledge_maintenance, summarization | `read_user_memory`, `read_vault`, `read_sandbox_files` | observe | low_read | native | in_process | yes |
| `file_organizer` | document_processing | `read_sandbox_files`, `write_sandbox_files` | execute | low_write | native | in_process | no |

`write_agent_notebook` is runtime-owned state (§16.3), not a capability. It is permitted in `observe` because it writes nothing outside the agent's own notebook. Its exact enforcement is in §16.3.

Browser, coding and stateful templates (`browser_monitor`, `repo_maintenance`, `stateful_assistant`) are specified in §29 and are `[FUTURE]` until their infrastructure exists.

---

## 6. Model Profile Registry

### 6.1 Responsibility

A model profile maps a named routing profile to an **existing** configured model entry (`06`). Profile features are **routing metadata, never authorization**.

### 6.2 Schema `[IMPLEMENTATION-READY]` (config `agents.model_profiles`)

| Field | Type | Meaning |
|---|---|---|
| `profile_id` | slug | e.g. `general-agentic` |
| `version` | int | |
| `model_ref` | reference to a `ModelEntryConfig` already valid under `06` (`provider` ∈ implemented providers; `secret_ref`; `pricing` required for non-local) | the only way a profile reaches a model |
| `features` | set of `ModelFeature`: `agentic_reasoning`, `tool_calling`, `structured_output`, `vision`, `browser_suitable`, `coding_suitable`, `long_context` | operator-declared |
| `context_window` | int | tokens |
| `latency_class` | `low \| medium \| high` | |
| `cost_class` | `local \| low \| medium \| high` | derived from `pricing` at load; a declared class that contradicts pricing fails load |
| `supported_runtimes` | list of runtime_id | |
| `required_apis` | list (`chat_completions`, `responses`) | |
| `guardrail_notes` | str | informational |
| `enabled` | bool | |

Rules:
- `[IMPLEMENTATION-READY]` a profile whose `model_ref.provider` is not implemented by `server/models/factory.py` fails startup. Today that means Ollama or OpenAI-compatible only.
- `[IMPLEMENTATION-READY]` the owner's model policy is the existing OD-RT-3 resolution. The set of profiles a given owner may use is: profiles whose `model_ref` equals the owner's resolved primary, or is in `agents.model_profiles_open_to_all` (operator list). A user's own configured model is never used for another user's agent (`18` §4.2 data-flow rule).
- User preference ("faster", "cheaper", "more thorough") is a closed `ModelPreference` enum in the draft. It changes ordering only, never the eligible set.

---

## 7. Runtime Provider Registry

### 7.1 Responsibility

Describes each runtime provider so the selector can match it, and exposes the provider interface (§7.3). Providers are registered in code; operators enable them in config.

### 7.2 AgentRuntimeProfile schema `[IMPLEMENTATION-READY]`

| Field | Type | Meaning |
|---|---|---|
| `runtime_id` | slug | `native`, `letta`, `openhands`, `browser_use`, `openai_agents`, `google_adk`, `langgraph`, `pydantic_ai`, `openclaw` |
| `runtime_type` | `native \| general_runtime \| framework_hosted \| specialized_runtime` | §30 categories |
| `version_pin` | exact version string | the adapter refuses any other version (the OD-MB-1 Mem0 precedent) |
| `supported_template_tags` | list of `TaskTag` | |
| `supported_model_features` | set | what the runtime can drive |
| `tool_interface` | `in_process_tool_catalog \| tool_gateway_mcp \| tool_gateway_http \| contained_workspace` | |
| `lifecycle_interface` | `task_runtime \| http_api \| sdk_in_worker_process \| ws_protocol` | |
| `persistence_model` | `volatile_task \| runtime_store \| workspace_files` | |
| `isolation_mode` | `IsolationMode`: `in_process < worker_process < container_netns` | |
| `network_requirements` | `none \| jarvis_gateway_only \| jarvis_gateway_plus_egress_proxy` | |
| `observability` | `full_trace \| gateway_trace_only` | |
| `cancellation` | `cooperative_event \| api_cancel \| container_kill` | |
| `export_supported`, `deprovision_supported` | bool | |
| `human_approval_mode` | `jarvis_gateway` (only accepted value) | framework-native approval flows are never relied on (§30.3) |
| `known_limitations` | list of str | |
| `required_infrastructure` | list of `InfraRequirement` | |
| `enabled` | bool | config `agents.runtimes.<id>.enabled`, default false for every non-native provider |

### 7.3 Provider interface `[IMPLEMENTATION-READY]`

```python
class AgentRuntimeProvider(Protocol):
    profile: AgentRuntimeProfile

    async def health(self) -> ProviderHealth: ...
    async def provision(self, spec: CompiledAgentSpec) -> RuntimeRef: ...          # idempotent on (agent_id, version)
    async def start_run(self, ctx: AgentRunContext) -> RunHandle: ...
    async def cancel_run(self, handle: RunHandle, reason: CancelReason) -> None: ... # must be safe to call repeatedly
    async def run_status(self, handle: RunHandle) -> AgentRunStatus: ...
    async def deprovision(self, ref: RuntimeRef) -> DeprovisionReceipt: ...          # deletes runtime-side state
    async def export_state(self, ref: RuntimeRef) -> bytes | None: ...              # None if unsupported
    async def list_runtime_agents(self) -> list[RuntimeRef]: ...                    # reconciliation
```

Invariants:
- A provider receives only a `CompiledAgentSpec` and an `AgentRunContext`. It never receives a `Principal`, a session token, a device credential, a SecretStore handle or a provider key.
- A provider never decides whether an operation is allowed. Every effect goes through the Tool Gateway, or for the native provider through the runtime's existing authorization path.
- `deprovision` must leave `list_runtime_agents()` without the ref; this is verified by AGENT-T19.

### 7.4 Native provider `[IMPLEMENTATION-READY]`

| Aspect | Specification |
|---|---|
| Interface | `start_run` creates an ordinary task through the runtime (`server/agent/runtime.py`) with: `mode = template.run_mode`; user input assembled by §9.7; an **envelope ceiling** (§10.3); agent attribution (§12.4); bounds `min(template, agent.bounds)` |
| Model loop | owned by the existing runtime; the worker chain = the selected profile's model first, then operator fallbacks that satisfy the template's `required_model_features` |
| Persistence | the task row (existing) + `agent_runs` + notebook; the transcript stays volatile (MEM-001) |
| Tools | the existing tool catalog, filtered by envelope |
| Cancellation | existing `/cancel` event and breaker (OD-RT-4, `18` §5) |
| Isolation | `in_process` (inherits OD-A1 unchanged) |
| provision / deprovision | provision is a no-op; deprovision deletes notebook entries and inbox items |
| Principal | v1: the **present user's** principal (on-demand or reminder-tap); Phase 5: the delegated principal |

---

## 8. Agent Selection

### 8.1 Responsibility

Deterministically choose `(template, runtime, model_profile)` after the worker's semantic proposal. Every branch is recorded in `selection_reason`.

### 8.2 Inputs and output `[IMPLEMENTATION-READY]`

```python
@dataclass(frozen=True)
class SelectionInput:
    template_hint: str | None                  # from draft
    task_tags: frozenset[TaskTag]              # from draft
    requested_abilities: frozenset[AbilityName]
    model_preference: ModelPreference | None
    trigger_kind: TriggerKind
    owner_primary_model_ref: str               # from OD-RT-3 resolution (server)
    enabled_templates: tuple[AgentTemplate, ...]
    enabled_runtimes: tuple[AgentRuntimeProfile, ...]
    enabled_model_profiles: tuple[AgentModelProfile, ...]
    available_infrastructure: frozenset[InfraRequirement]   # from config + startup probes
    budget_ceiling_per_run: Decimal            # §10.5

@dataclass(frozen=True)
class AgentSelection:
    template_id: str
    template_version: int
    runtime_id: str
    runtime_version_pin: str
    model_profile_id: str
    model_profile_version: int
    selection_reason: tuple[str, ...]          # ordered rule trace, human-readable
    constraints_applied: tuple[str, ...]       # filters that removed candidates, with ids
```

### 8.3 Algorithm `[IMPLEMENTATION-READY]`

1. **Template candidates** = enabled templates where:
   - `requested_abilities ⊆ template.abilities`;
   - `|task_tags ∩ template.task_tags| ≥ 1`;
   - `trigger_kind ∈ template.trigger_support`;
   - `template.required_infrastructure ⊆ available_infrastructure`.
2. If `template_hint` is set and names a candidate, that candidate wins. If the hint names a non-candidate, record why, and continue without it. **A hint never admits a template the filters rejected.**
3. Otherwise, order candidates by:
   1. largest `|task_tags ∩ template.task_tags|`;
   2. then smallest ability surface `|template.abilities|` (least privilege);
   3. then `priority`;
   4. then `template_id`.
4. **Ambiguity rule:** if the top two candidates tie on (1) and (2) and differ in abilities, `run_mode` or `risk_ceiling`, return `needs_clarification` listing both descriptions. Zero candidates → `needs_clarification` with the three closest templates, or `no_template` if none are enabled.
5. **Runtime candidates** = `[preferred_runtime] + fallback_runtimes` filtered by:
   - enabled;
   - template tag supported;
   - `isolation_mode ≥ template.min_isolation`;
   - `required_infrastructure ⊆ available`;
   - `health() == ok` at compile time.

   Take the first. None → fail closed with `no_runtime` (AGENT-T21).
6. **Model candidates** = enabled profiles where:
   - `features ⊇ template.required_model_features`;
   - `runtime_id ∈ supported_runtimes`;
   - permitted for this owner (§6.2);
   - `projected_cost_per_run ≤ budget_ceiling_per_run`, where the projection is `max_model_calls_per_run × pricing at the template's max context`.

   Order by `model_preference` (`faster` → latency_class; `cheaper` → cost_class; `thorough` → context_window desc, then features count), then `profile_id`. None → `no_model`.
7. Emit `AgentSelection` with the trace.

The selector is a pure function: same inputs → same output (AGENT-T20 property test).

---

## 9. Agent Compiler

### 9.1 Responsibility and pipeline

```
AgentDraft ─validate─► classify ─► select (§8) ─► derive abilities→capabilities (§9.3)
  ─► envelope ceiling (§10) ─► memory policy (§16) ─► trigger (§17) ─► outputs ─► budget (§10.5)
  ─► instructions assembly (§9.7) ─► CompiledAgentSpec (immutable, hashed)
```

The compiler is the **only** code that can produce a `CompiledAgentSpec`. It is pure (AF-C2), except that its caller supplies the owner's server-side context.

### 9.2 AgentDraft — what the worker may write `[IMPLEMENTATION-READY]`

`AgentDraft` is a strict Pydantic model (`extra="forbid"`, frozen).

| Field | Type | Bounds |
|---|---|---|
| `name` | str | 1–80 printable chars |
| `purpose` | str | 1–2000 chars; the user's goal in natural language |
| `desired_outcome` | str | 0–1000 chars |
| `task_tags` | list[`TaskTag`] | 1–5, closed enum |
| `template_hint` | str \| null | must match `^[a-z][a-z0-9_]{2,40}$` |
| `requested_abilities` | list[`AbilityName`] | 0–10, closed enum |
| `sources` | list[`SourceRef`] | 0–20; each `{kind: url\|memory_topic\|vault_domain\|sandbox_path, value: str≤500}` — data, validated per kind |
| `trigger_request` | `TriggerRequest` | `{kind: on_demand\|reminder\|unattended, schedule_text: str≤200 \| null, cron: str \| null, timezone: IANA str \| null}` |
| `output_request` | `OutputKind` | `inbox` only in v1 |
| `model_preference` | `ModelPreference` \| null | `faster \| cheaper \| thorough` |
| `budget_preference_per_run` | decimal \| null | only lowers the policy value, never raises it |
| `notes_for_user` | str | 0–500 chars; shown to the user, never used by the compiler |

**Forbidden in a draft** (AGENT-T2, §23). Because the model rejects extra fields, any of these makes the whole draft malformed:
- `user_id`, `owner_user_id`, `graph_id`, `agent_id`;
- `capabilities`, `grants`, `tier`, `risk`, `confirmation`, `requires_confirmation`;
- `budget_ceiling`, `secret_ref`, `secrets`, `delegation`, `delegation_id`;
- `endpoint`, `device_id`, `target_device`;
- `runtime`, `runtime_id`, `network`, `egress`, `destinations`;
- `model_ref`, `mode`, `version`, `spec_hash`.

A malformed draft is refused as an observation and audited `agent.draft.rejected` with the offending field names, never their values. This matches `server/agent/proposals.py`.

### 9.3 Abilities — the bridge from words to capabilities `[IMPLEMENTATION-READY]`

The worker names *abilities* (closed enum). The compiler maps each to exact capability operations through a code-reviewed table, `server/agents/abilities.py`:

| AbilityName | Capability → operations | Extra constraint |
|---|---|---|
| `read_web_allowlisted` | `net.request` → `get` | only destinations in the operator `EgressPolicy`. The agent cannot widen it; `sources.url` entries not already allowed become a clarification ("ask your operator to allow X"), never an egress change |
| `read_user_memory` | runtime memory hydration (not a capability; `21`) | owner-filtered, visibility-filtered |
| `read_vault` | vault hydration (not a capability) | |
| `read_sandbox_files` | `file.read` → `read_file`, `list_directory`, `stat` | `sandbox_root` label from `sources.sandbox_path` label only |
| `write_sandbox_files` | `file.write` → `write_file`, `create_file` | `delete_file`/`bulk_delete` excluded from every template in v1 |
| `read_agent_notebook`, `write_agent_notebook` | agent notebook (§16.3; not a capability) | own agent only |
| `create_reminder` | `scheduler.create` → `create_reminder` | reminders only; `task_reason` bound to the run input (SCH-B4) |
| `invoke_model_tool` | `model.invoke` → `invoke` | only model-tools the owner's config enables |

`[IMPLEMENTATION-READY]` **never mappable:** `agent.*`, `system.restricted`, `device.*`, `app.interact`, every floor name. A template containing an ability that maps to any of these fails load.

### 9.4 Clarification contract

```python
class CompileOutcome(BaseModel):          # returned to the worker as the tool observation
    kind: Literal["compiled", "needs_clarification", "rejected"]
    compile_id: UUID | None               # compiled only; expires in 15 minutes, owner+task bound
    spec_preview: CompiledAgentSpecView | None   # user-safe rendering of the spec
    questions: list[ClarificationQuestion] = []  # closed choices where possible
    reason_codes: list[str] = []          # e.g. "ambiguous_template", "no_runtime", "source_not_allowlisted"
```

Clarifications are answered in the normal conversation. The worker recompiles with a revised draft. The compiler holds no conversational state beyond `compile_id` previews.

### 9.5 CompiledAgentSpec `[IMPLEMENTATION-READY]` (immutable; table `agent_spec_versions`)

| Field | Type | Source |
|---|---|---|
| `agent_id` | uuid | server |
| `version` | int | server |
| `spec_hash` | sha256 hex over canonical JSON of every field below except `created_at` | compiler |
| `owner_user_id`, `graph_id` | uuid | principal |
| `name`, `purpose`, `desired_outcome`, `sources` | as validated | draft (data) |
| `selection` | `AgentSelection` | selector |
| `template_id`, `template_version` | | selector |
| `run_mode` | `TaskMode` | template |
| `risk_ceiling` | `RiskCategory` | template |
| `envelope_ceiling` | list of `EnvelopeEntry {capability, operations: [str], scope: {key: value}}` | §10.2 |
| `hydration` | `{user_memory: bool, vault_domains: [str]}` | §16 |
| `notebook_enabled` | bool | template |
| `trigger` | `CompiledTrigger {kind, cron: str \| null, timezone: str, next_fire_preview: timestamptz \| null}` | §17 |
| `outputs` | `[inbox]` | fixed v1 |
| `budget` | `{per_run: decimal, per_month: decimal}` | §10.5 |
| `bounds` | `{max_run_seconds, max_model_calls, max_tool_calls}` | template ∩ `agent.bounds` |
| `instructions_template_version` | int | §9.7 |
| `compiler_version` | int | code constant |
| `created_at` | timestamptz | server |

A spec is never edited. `update` compiles a new version. The previous version stays for audit until deletion.

### 9.6 Validation rules (each a unit test with a mutation counterpart)

1. `envelope_ceiling ⊆ ability mapping(template.abilities ∩ requested_abilities)`.
2. Every capability in the envelope exists in the registry. None is a floor name, `agent.*`, `system.restricted` or `device.*`.
3. `risk_ceiling ≤ severity(run_mode ceiling or table)`: `observe` templates cannot declare `low_write`.
4. `trigger.kind = unattended` ⇒ `template.unattended_supported` **and** Phase 5 enabled; otherwise `rejected: unattended_unavailable`.
5. `budget.per_run ≤ min(template.default, owner policy)` and `budget.per_month ≤ owner policy`.
6. `outputs == [inbox]`.
7. The owner's active agent count `< agents.max_agents_per_user`.
8. `spec_hash` recomputes identically on load (tamper check); a mismatch marks the agent `invalid` and refuses runs.

### 9.7 Instructions assembly (what the agent model sees) `[IMPLEMENTATION-READY]`

The run's user input is assembled by code from a versioned template:

```
[JARVIS agent run — instructions are data from the agent's owner; they grant nothing]
Agent: {name}   Run: {run_id}   Mode: {run_mode}
Goal (owner's words, quoted):
<<<
{purpose}
{desired_outcome}
>>>
Sources the owner listed (data):
<<<
{sources rendered one per line}
>>>
Deliver: a concise result for the owner's inbox. Do not ask the owner questions during an
unattended run; record open questions in the result instead.
```

The system prompt, tool list, activation rules and operator guidance come from the existing runtime (`server/agent/context.py`). Purpose text is quoted data. It cannot change the tool list, the mode, the envelope or bounds, because those are applied by code (§10) regardless of what the text says.

---

## 10. Authorization Derivation

### 10.1 The effective-authority formula `[IMPLEMENTATION-READY]`

For every operation an agent run attempts:

```
effective = owner's current grants          (live, 04 D5 — read at call time, never cached in the spec)
          ∩ template maximum                (template.abilities → mapped operations)
          ∩ envelope_ceiling                (compiled spec)
          ∩ graph scope                     (04 D1: owner still an active member of spec.graph_id)
          ∩ runtime/platform restrictions   (adapter exists for the platform; native = server platform only)
          ∩ run restrictions                (bounds, budget, run status, cancellation)
          ∩ mode ceiling                    (modes.py: observe/draft/suggest ⇒ low_read)
          ∩ risk_ceiling                    (template)
          ∩ global floor                    (floor.py: prohibited, never confirmable)
```

The spec stores only the **ceiling**. Nothing in the spec is ever read as a grant.

### 10.2 Envelope ceiling construction (compile time)

`envelope_ceiling = map(template.abilities ∩ requested_abilities) ∩ registry(existing capability ops) − never_mappable`. Scope values come only from validated `sources` (sandbox labels) or are empty. `net.request` gets no scope keys, because destinations are the operator `EgressPolicy`.

### 10.3 Enforcement at run time — the envelope gate `[IMPLEMENTATION-READY]`

Implemented beside the mode ceiling, in the supervisor, **before** the engine is asked:
- **new module:** `server/agent/envelope.py`;
- **runtime change:** the runtime calls it where it calls `within_ceiling`.

```python
def within_envelope(envelope: Envelope | None, capability: str, operation: str,
                    scope: Mapping[str, str] | None, tier: RiskCategory | None,
                    risk_ceiling: RiskCategory | None) -> bool:
    """None envelope = not an agent run (unchanged behaviour). Unknown tier ⇒ False."""
```

Rules:
- A `request_capabilities` proposal for a capability outside the envelope is refused as an observation, and never offered for confirmation (the same pattern as `modes.not_activated`).
- A tool call whose `(capability, operation)` is outside the envelope, or whose scope is not a narrowing of the envelope scope, is refused before `04`.
- Everything inside the envelope still goes through activation (standing grant or confirmation), then `04` (D1–D5, floor, tier) exactly as today. **The envelope can only remove, never add** (AGENT-T3, T15).
- The envelope object is built by the runtime from the stored spec version **at run start** and re-read at every step. Spec tampering is caught by `spec_hash` (§9.6 rule 8). A version change mid-run cancels the run with `spec_changed`.

### 10.4 What an agent can never do (each a named test)

| Forbidden | Enforced by |
|---|---|
| create agents / child agents | `agent.*` never mappable (§9.3); `agent.*` capabilities excluded from every envelope (AGENT-T5) |
| grant itself capabilities / modify envelope / raise tier | no capability-grant path in any envelope; spec immutable; tier from registry (AGENT-T4) |
| obtain secrets | handle-only (`12`); agent packages barred from `server.secrets` (AF-C1) |
| modify delegation, owner, budget | no tool writes those fields; only owner APIs do, with confirmation |
| access another graph | graph fixed in spec; D1 live check each step (AGENT-T9) |
| bypass confirmation | engine unchanged; no draft/spec field for confirmation (AGENT-T2) |
| call arbitrary destinations | `net.request` bound to operator `EgressPolicy`; external runtimes network-isolated (§21) |
| turn output into authority | output stored as inbox data only; never parsed for permissions (AGENT-T16) |

### 10.5 Budgets `[IMPLEMENTATION-READY]`

- **Per run:** `spec.budget.per_run` is enforced by the existing usage precheck with an extra per-run accumulator. A breach fails the run `budget_exceeded`.
- **Per agent per month:** the sum over `agent_run_usage` joined to `usage_events` for the calendar month (UTC). The next run is refused `agent_budget_exhausted` when the sum plus the projected cost exceeds it.
- **Owner's budget:** unchanged. Agent spend is the owner's spend (`security.budgets`, OD-USE-3).
- `[LOCKED]` defaults of `0.0` refuse paid calls. An agent whose only eligible model is paid cannot run until the operator raises budgets. The compiler reports `no_model: budget` rather than creating a dead agent.

---

## 11. Agent Gateway

### 11.1 Responsibility

The single choke point through which any agent run reaches models and tools.
- **Phase 3:** in-process, used by the native provider.
- **Phase 6:** additionally exposed over an authenticated network surface to external runtimes.

**One code path for both**, so native and external runs are tested identically.

### 11.2 AgentRunContext `[IMPLEMENTATION-READY]` (what a provider receives)

| Field | Type | Notes |
|---|---|---|
| `run_id` | uuid | |
| `agent_id`, `version`, `spec_hash` | | |
| `input_text` | str | §9.7 assembled |
| `deadline` | timestamptz | `started_at + bounds.max_run_seconds` |
| `model_endpoint` | url \| null | Phase 6: Model Gateway base URL; native: null (in-process) |
| `tool_endpoint` | url \| null | Phase 6: Tool Gateway URL; native: null |
| `run_token` | str \| null | Phase 6 only; opaque; never logged |
| `tool_manifest` | list of `ToolDescriptor {tool_id, operation, input_schema, description}` | derived from envelope ∩ enabled tools |
| `model_alias` | str | the only model name the runtime may request (`agent-model`) |

No field carries a principal, session, device, secret handle or provider key.

### 11.3 Run tokens `[IMPLEMENTATION-READY]` (Phase 3 data model; used on the wire in Phase 6)

Table `agent_run_tokens`:

| Column | Type | Notes |
|---|---|---|
| `token_id` | uuid | PK |
| `token_hash` | bytea (SHA-256 of 32 random bytes, base64url on the wire) | the plaintext is shown once to the provider at `start_run` |
| `run_id`, `agent_id` | uuid | binding |
| `spec_hash` | text | binding |
| `purpose` | `model \| tool` | separate tokens per gateway |
| `issued_at`, `expires_at` | timestamptz | `expires_at = run deadline + 30 s`, never later |
| `revoked_at` | timestamptz \| null | set by stop / cancel / breaker / delete / spec change / grant loss |

Rules:
- Validation = hash lookup + not expired + not revoked + run in state `running` + `spec_hash` equals the definition's current `spec_hash`.
- Revocation takes effect on the next request, with no cache (AGENT-T8, T23).
- **Replay protection** (tool gateway): every request carries `request_nonce` (128-bit) and `sent_at`. The gateway rejects `|now − sent_at| > 60 s` and any nonce already seen for the token (table `agent_gateway_nonces`, pruned after token expiry).
- **Idempotency:** `(token_id, request_nonce)` is unique; a retried nonce returns the stored response, never re-executes.

---

## 12. Model Gateway

### 12.1 Responsibility

Every model call of every agent run — native in-process in Phase 3, external over HTTP in Phase 6 — is resolved, budget-checked, metered and traced by JARVIS. External runtimes never hold a provider key.

### 12.2 Contract `[IMPLEMENTATION-READY]` (Phase 3 in-process; Phase 6 HTTP)

- **Phase 6 wire format:** OpenAI-compatible `POST {model_endpoint}/v1/chat/completions` with `Authorization: Bearer <run_token(model)>`.
  *Built in Phase 6 slice 6A (register §2J, AF-P6-1…7): a separate internal listener, off by default; the order, errors and bounds below as specified, plus `429 max_model_calls` for the run's model-call bound.*
- **Model name:** the request's `model` must equal `model_alias`; anything else is `403 model_not_allowed`.
- **Pipeline:**

```
authenticate token → resolve run → map alias → spec.selection.model_profile_id → ModelEntryConfig
→ bounds: model_calls < max_model_calls_per_run, now < deadline
→ usage precheck (owner budget, per-run, per-agent-month) → provider adapter (06; key via existing resolver,
  class model_api_key only, OD-SEC-2) → response schema validation (choices/message shape; oversize → truncated
  with a marker, 05 §6) → UsageEvent(kind=model_call) + agent_run_usage row → trace event → response
```

- **Streaming** is not supported in v1 (`stream: true` → `400`). This keeps metering exact.
- **Retries:** at most the existing worker-chain behaviour (`18` §4.2), restricted to profiles that satisfy the template's `required_model_features`. Each attempt is metered.
- **Cancellation:** run cancel → token revoked → in-flight provider call cancelled via the task cancellation event (native) or connection close (external); the attempt is metered as `units=0`.

### 12.3 Errors

| HTTP | code | When |
|---|---|---|
| 401 | `invalid_run_token` | unknown, expired or revoked token |
| 403 | `model_not_allowed` | alias mismatch |
| 409 | `run_not_running` | run paused, cancelled or finished |
| 429 | `budget_exceeded` \| `agent_budget_exhausted` \| `rate_limited` | usage checks |
| 503 | `dependency_unavailable` (class `model`) | provider chain exhausted |

### 12.4 Attribution without touching the locked `usage_events` entity

Table `agent_run_usage(run_id uuid, usage_id uuid unique, primary key(run_id, usage_id))` is written in the same transaction as the `UsageEvent`. Per-agent and per-run totals join through it. `usage_events` is unchanged (the OD-JDG-4 precedent). Extending `01` instead is `[OWNER DECISION]` OD-AF-9.

---

## 13. Tool Gateway

### 13.1 Responsibility

Every tool effect of an agent run is authorized by `04` as the run's principal, under the envelope, mode and risk ceilings, and recorded.

### 13.2 Request / response / error schemas `[IMPLEMENTATION-READY]`

```python
class ToolGatewayRequest(BaseModel):               # extra="forbid"
    request_nonce: str                              # 22+ chars base64url
    sent_at: datetime
    tool_id: str
    operation: str
    arguments: dict[str, Any]                       # validated against the tool's input schema
    resource_ref: str | None = None
    scope: dict[str, str] | None = None

class ToolGatewayResponse(BaseModel):
    status: Literal["ok", "denied", "confirmation_pending", "failed"]
    observation: str | None                         # untrusted data, truncated to max_observation_chars
    denial_reason: str | None                       # plain text, no secrets, no policy internals beyond the refusal
    confirmation_id: UUID | None                    # when pending; the run is paused
    args_hash: str                                  # operation_key(...) of the executed/refused call

class ToolGatewayError(BaseModel):
    code: Literal["invalid_run_token", "replay", "stale_request", "schema_invalid",
                  "run_not_running", "unknown_tool", "internal_error"]
    message: str
```

### 13.3 Pipeline `[IMPLEMENTATION-READY]`

```
1. authenticate token (purpose=tool)            → 401 invalid_run_token
2. replay/staleness check (nonce, sent_at)      → 409 replay / 400 stale_request
3. resolve run, agent, current spec version     → 409 run_not_running if not running
4. derive principal:
     Phase 2–4: the present user's Principal captured at run start (re-checked principal_active each call)
     Phase 5:   DelegatedPrincipal (§15.2)
5. schema-validate arguments against the tool's contract
6. mode ceiling (modes.within_ceiling) → envelope gate (§10.3)   → denied (observation)
7. activation: capability active for this run's task? else standing grant → activate; else consequential
   activation → confirmation_pending (Phase 2–4: owner confirms on device; Phase 5: see §15.6)
8. 04 authorize(ActionRequest) — D1–D5, floor, tier                → denied / confirmation_pending
9. execute through the existing tool catalog (07/09/10) with the task's cancellation event
10. record: AuditEvent (existing agent.tool.* events + agent_id/run_id), UsageEvent(tool_call), trace event
11. return observation (truncated, secret-pattern scrubbed before it leaves the gateway, 19 §4 detector)
```

- **Args hashing:** `operation_key(tool, operation, platform, arguments, resource_ref, scope)` (existing function). The same hash binds a confirmation to the exact call.
- **Audit fields** added to agent-run events: `agent_id`, `run_id`, `spec_version`, `args_hash`, `gateway` (`in_process` \| `http`). No argument values or observation text are stored in audit rows.
- The **native provider** executes steps 4–11 through the existing runtime path; steps 1–3 are implicit, because the run is in-process. The same `envelope.py` and audit fields are used, so behaviour is identical.

---

## 14. Agent Lifecycle

### 14.1 Definition states (`AgentStatus`) `[IMPLEMENTATION-READY]`

```
(draft)* → (compiled)* → awaiting_confirmation → active ⇄ paused
                                        │            │
                                        ▼            ▼
                              needs_reapproval    revoked ──► deleted
                                        │
                                        └──► active (after owner re-approval of a new version)
```

\* `draft` and `compiled` are **not persisted** as definitions. They exist as a `CompileOutcome` preview (15-minute `compile_id`). A definition row is created only at `awaiting_confirmation` (paused-task confirmation) and becomes `active` on approval. If the confirmation expires or is rejected, the row is deleted.

| Status | Can run | Trigger fires | Meaning |
|---|---|---|---|
| `awaiting_confirmation` | no | no | creation pending the owner's approval |
| `active` | yes | yes | |
| `paused` | no | no (reminders suppressed) | owner or system paused it |
| `needs_reapproval` | no | no | template/model/runtime changed or became unavailable in a way that alters the envelope or selection |
| `revoked` | no | no | a hard invalidation (graph membership lost, owner suspended, spec tamper); only deletion or recompilation from scratch follows |
| `deleted` | no | no | tombstone |

### 14.2 Run states (`AgentRunStatus`)

`queued → running → waiting → completed | failed | cancelled`. Here `waiting` covers the task's `awaiting_confirmation` and `waiting_for_platform`.

This is the prompt's merged lifecycle split into the two objects it actually describes:
- definition: `draft … active, paused, revoked, deleted`;
- run: `running, waiting, completed, failed`.

### 14.3 Event handling `[IMPLEMENTATION-READY]` — "no authority survives revocation"

| Event | Effect | When detected |
|---|---|---|
| Owner loses a grant | the next affected call fails `04` D5; the envelope does not compensate. If the loss empties a template's required capability, the definition goes `needs_reapproval` | live, per call; nightly recheck job |
| Owner leaves the graph / graph deleted | running runs cancelled `principal_revoked`; tokens revoked; definition `revoked` | per step (D1); graph lifecycle hook |
| Owner suspended | runs cancelled at the next step (existing OD-ID-1); definition `paused` | per step |
| Model policy change (profile disabled, OD-RT-3 change) | recompile under the new policy: equal or narrower → auto-applied as a new version with an audit row; otherwise `needs_reapproval` | config reload / startup |
| Template version change | same rule (AGENT-T25) | startup |
| Runtime disappears / disabled | runs fail `dependency_unavailable`; definition `needs_reapproval` unless the recompile selects a fallback runtime with an equal-or-narrower envelope | health probe / startup |
| Per-agent monthly budget exhausted | runs refused; definition stays `active`; owner notified once per month | at run start |
| Delegation expires (Phase 5) | unattended trigger stops; on-demand and reminder-tap still work | delegation check |
| Device revoked | runs started from that device's session cancelled at the next step (principal check); the definition is unaffected | per step |
| User deletes the agent | `consequential` confirmation → runs cancelled → tokens revoked → trigger removed → `provider.deprovision` → notebook and inbox deleted → spec versions purged → tombstone | synchronous |
| Server restart | reconciliation (§25.2) | startup |
| Runtime crash mid-run | run `failed`, `runtime_crashed`; no replay of pending actions | provider status / task failure |
| Judge requests stop (`live_window`, `may_request_stop`) | breaker `trip(task)` → emergency stop sequence (`18` §5.3) → run `cancelled`, `emergency_stop` | existing |
| Breaker / operator stop | same; additionally `POST /admin/control/agents/{id}/pause` pauses the definition | existing + new control |

---

## 15. Recurring / Delegated Agents

### 15.1 The three run kinds

| Kind | Principal | Allowed today | Mechanism |
|---|---|---|---|
| **On-demand** | present user (session + device) | **yes** | `POST /api/v1/agents/{id}/runs` or the worker's `agent.run.run_now` |
| **Reminder-tap** | present user | **yes** | the scheduler reminder carries `agent_id`; the tap opens a *Run agent* screen; the user presses Run (§17) |
| **Unattended recurring** | delegated principal | **no** — `[OWNER DECISION]` OD-AF-2 + PRD amendment | this section |

### 15.2 DelegatedPrincipal `[OWNER DECISION]` (design fixed; activation needs ratification)

```python
class DelegatedPrincipal(ORMBase):           # shared/schemas/authorization.py, frozen, extra="forbid"
    user_id: UUID                             # the owner
    agent_id: UUID
    delegation_id: UUID
    run_id: UUID
    graph_id: UUID
    # no device_id, no session_id — structurally
```

Required changes:
- `04`'s engine and `SecurityPort` accept `Principal | DelegatedPrincipal`.
- Device operations require `device_id`, so they are structurally refused for a delegated principal (OD-DEV-1 unchanged).
- `principal_active` for a delegated principal checks:
  - owner `active`;
  - graph membership active;
  - delegation `active` and unexpired;
  - `envelope_hash` equals the current spec's envelope hash;
  - run `running`.

### 15.3 StandingDelegation schema

| Field | Type | Notes |
|---|---|---|
| `delegation_id` | uuid | PK |
| `agent_id` | uuid | one active per agent |
| `owner_user_id`, `graph_id` | uuid | copied from the definition |
| `spec_version`, `envelope_hash` | int, sha256 | any spec change → `status=invalidated` |
| `allowed_trigger_cron`, `timezone` | str | exactly the compiled trigger |
| `max_runs_per_day` | int 1–24 | |
| `budget_per_run`, `budget_per_month` | decimal | ≤ the spec's |
| `created_at`, `expires_at` | timestamptz | `expires_at ≤ created_at + agents.delegation_max_days` |
| `created_with_step_up` | bool (must be true) | confirmation + step-up at `/confirm` |
| `created_by_device_id`, `created_by_session_id` | uuid | provenance |
| `status` | `active \| revoked \| expired \| invalidated` | |
| `revoked_at`, `revoked_reason` | | |

### 15.4 Lifecycle

- **Grant:** `agent.delegate.grant_standing` (tier `consequential` + step-up). The card shows the envelope, schedule, budgets and expiry.
- **Revoke:** `agent.delegate.revoke_standing` (`low_write`), always allowed; also revoked by pause, delete, graph loss or owner suspension.
- **Expiry:** status `expired`. The owner is notified 3 days before expiry and at expiry. Renewal is a new grant with step-up.

### 15.5 Unattended trigger path (no scheduler execution)

- A new `server/agents/triggers.py` job loop, not the scheduler, evaluates due unattended agents from `agent_definitions` joined to active delegations. AF-C4 keeps the scheduler execution-free.
- A due run starts only if all hold: `max_runs_per_day` not reached; budget available; delegation valid; global breaker not latched; agent `active`.
- **Misfires:** one run within `misfire_grace_minutes`; older occurrences are coalesced into one `missed` record, reported in the inbox. No catch-up storm.

### 15.6 Confirmation in unattended runs `[OWNER DECISION]` OD-AF-4

- **Recommended v1:** unattended envelopes are restricted to `low_read`/`low_write`. The compiler rejects `unattended` for any template whose `risk_ceiling > low_write`. A consequential request therefore cannot arise; if it did (a registry change), it is refused, never paused.
- **Alternative** (not v1): pause and notify, with a token bound to `(owner, agent_id, delegation_id, run_id, capability, operation, resource, args_hash)`, approved on an authenticated device with step-up.

### 15.7 Smallest safe v1 unattended envelope

`low_read`/`low_write` only · no device execution · no external recipients · no agent creation · no `system.restricted` · no `high_irreversible` · no `net.request post` · inbox output only · budgets explicit and non-zero.

### 15.8 Canonical changes required before Phase 5 (exact)

| Document | Change |
|---|---|
| PRD §22 (SCHED-001) | append: *"An agent with an active, step-up-granted StandingDelegation may execute unattended within its compiled envelope (≤ low_write), outputs to the owner's inbox only. The scheduler itself still never executes."* |
| `docs/22` §0 and OD-SCH-3 | OD-SCH-3 resolved by reference to doc 29 §15 |
| `03` §8 / `04` §1 | DelegatedPrincipal as a second principal form; device ops excluded |
| `01` §1.2 | `CapabilityScopeType += agent` (for delegation-scoped activation records); new entities (§22) |
| `07` / confirmation | (only if OD-AF-4 alternative) delegated confirmation binding |
| register | OD-AF-1…10 decisions |

---

## 16. Memory / State

### 16.1 Three distinct concepts `[LOCKED]` + `[IMPLEMENTATION-READY]`

| | Store | Lifetime | Who writes | Visibility |
|---|---|---|---|---|
| Session/task state | volatile runtime state | the run | runtime | the run |
| User persistent memory | Mem0 via `21` | until the user deletes | JARVIS extraction and gate only | owner / graph-shared per the triplet |
| Agent operational state | `agent_notebook_entries` (native); provider store (external) | until the agent is deleted | the agent's own runs | owner-private, per agent |

### 16.2 MemoryPolicy (template field)

`{user_memory_read: bool, vault_read: bool, vault_domains: [str], notebook: none | read | read_write, user_memory_write: never}`.

`user_memory_write` has one value in v1: agent output never becomes user memory. Agent runs are excluded from `memory.auto_extract` (`_form_memory` skips tasks with an agent attribution, AGENT-T17).

### 16.3 Agent notebook `[IMPLEMENTATION-READY]`

- **Table** `agent_notebook_entries(entry_id, agent_id, owner_user_id, key str≤120, value text≤8000, updated_at, updated_by_run_id)`. Unique `(agent_id, key)`. At most 200 entries per agent.
- **Access:** two runtime-owned pseudo-tools, `notebook.get` and `notebook.put`.
  - Exposed only when `spec.notebook_enabled`.
  - They are not capabilities, and they operate only on the run's own `agent_id`, bound by the runtime, never taken from arguments.
  - `put` is allowed in `observe` mode because its effect is confined to the agent's own state. It is recorded as an audit event.
- **Gating:** values pass the secret-pattern detector and the `21` §4 emotional-content gate before storage. A match rejects the write with an observation.
- **Deletion** with the agent; **export** via `GET /agents/{id}/export` (owner, `low_read`).

### 16.4 User memory access

Only through the existing hydration (`AuthorizedContextHydrator`): owner-filtered, visibility-filtered and re-checked by the engine. It is included at run start when `hydration.user_memory`. Agents have no direct Mem0 access.

---

## 17. Scheduler

### 17.1 Reminder-tap integration `[IMPLEMENTATION-READY]` (no scheduler contract change)

- The agent's `reminder` trigger creates an ordinary `ScheduledJob` through the scheduler's service API. It is created by `server/agents/service.py` via a composition-root port, not by the scheduler importing agents.
- **Job fields:**
  - `task_reason = "Run agent: {name}"` — the owner's approved name; SCH-001 needs a non-empty user-given reason, and the owner approved the name on the confirmation card;
  - `schedule` = the compiled cron with `CRON_TZ=`;
  - new nullable `agent_id` column on `scheduled_jobs` — data only; the scheduler never interprets it. This needs an `01` field addition, flagged in §28.
- **Reminder frame:** gains optional `agent_id` for clients declaring `features: ["agent_reminders"]` (the SCH-B11 pattern). The Android client shows *"Run {name}"*; pressing it calls `POST /agents/{id}/runs` with the present session.
- **Pause and delete:** pausing an agent cancels its reminder job; deleting it cancels the job. Both go through the scheduler's existing consequential `DELETE` flow, confirmed as part of the agent's own pause/delete confirmation.

### 17.2 Rule preserved `[LOCKED]`

*A firing reminder delivers a message; it never executes* (`22` §0). Unattended execution uses §15.5's separate trigger loop, only after the PRD amendment.

---

## 18. Judge

- **Trace source:** agent runs are tasks, so the existing JDG-B1 trace (volatile runtime events plus decisions, tiers, units and cost) applies unchanged, with `agent_id`/`run_id` added to trace events. Phase 6 external runs are traced from gateway events (`gateway_trace_only`).
- **The Judge may:** evaluate (`post_hoc`, `live_window`); request a stop through the breaker (existing JDG-B2 gating); propose improvement candidates.
- **The Judge may not:** create agents, grant authority, modify envelopes, approve itself, override confirmations, change budgets or choose policy (AF-C5 + existing contract "The Judge is never an authority").
- **Improvement candidates for agents** `[IMPLEMENTATION-READY]`:
  - new target class `agent.purpose` (agent-scoped);
  - scoped to **that agent's owner only**, never applied to other users — this avoids the OD-JDG-5 cross-user leak class by construction;
  - applied only when the owner accepts it through `agent.define.update` (a new spec version, `consequential`);
  - candidates may never target envelope, budget, trigger, delegation, runtime or model selection.
- **Judge disabled:** the Agent Factory is fully functional (AGENT-T28).

---

## 19. Endpoints

| Surface | v1 behaviour |
|---|---|
| Android / desktop inbox | `GET /api/v1/agents/inbox` (owner); a new channel frame `agent_result` for clients with `features: ["agent_inbox"]`; notification text is the agent name plus "result ready" only — content is fetched over the authenticated API |
| Approvals | creation, update, delete and delegation confirmations use the existing confirmation card (deterministically rendered spec diff) |
| Stop | every agent screen has Stop (`agent.control.stop`, always allowed) |
| Device execution | none for delegated runs (structural). Present-user runs may use device capabilities only if a future template includes them — no v1 template does |
| Voice | a transcript may *start* an on-demand run by filling the task box (VOI-B2); voice never confirms (`27` §3) |

---

## 20. Security Model

### 20.1 Invariants (each maps to tests)

| Invariant | Tests |
|---|---|
| NO MODEL OR AGENT RUNTIME CAN CREATE AUTHORITY | AGENT-T2, T3, T4, T5, T16, T26 |
| AGENT OUTPUT ≠ AUTHORITY | AGENT-T16, T17 |
| EXTERNAL RUNTIME ≠ TRUST ROOT; COMPROMISED RUNTIME ≠ COMPROMISED JARVIS | AGENT-T11, T12, T13, T23 |
| MEMORY/STATE ≠ AUTHORIZATION | AGENT-T17; notebook never read by authorization code (import contract AF-C6: `server.graph`/`server.capabilities` never import `server.agents`) |
| NO AUTHORITY SURVIVES REVOCATION | AGENT-T8, T10, T24, T27, T30 |

### 20.2 Threat rows (proposed for `14`)

| ID | Threat | Block | Test |
|---|---|---|---|
| SEC-AG1 | prompt injection requests out-of-envelope tool | envelope gate before 04 | AGENT-T15 |
| SEC-AG2 | draft smuggles authority fields | strict schema, rejected | AGENT-T2 |
| SEC-AG3 | agent requests `agent.*` / self-grant | never mappable; floor | AGENT-T5, T4 |
| SEC-AG4 | stolen run token replayed | expiry, revocation, nonce | AGENT-T6, T7 |
| SEC-AG5 | compromised external runtime calls arbitrary hosts | netns egress only to gateway | AGENT-T12 |
| SEC-AG6 | runtime obtains provider key | Model Gateway; keys never in context | AGENT-T13 |
| SEC-AG7 | cross-user agent access | owner-only resource auth, 404 | AGENT-T9 |
| SEC-AG8 | orphaned external agent keeps running | reconciliation + token revocation | AGENT-T19 |
| SEC-AG9 | budget runaway via loops | per-run/per-month budgets + stall detection | AGENT-T22 |
| SEC-AG10 | template edited to widen envelope silently | version bump → revalidation | AGENT-T25 |

---

## 21. External Runtime Isolation `[BLOCKED BY INFRASTRUCTURE]` (requirements fixed now)

Before any external provider is enabled, every item below must exist and be tested:

1. **Container per runtime instance** (per owner for runtimes holding state, e.g. Letta; per run for contained workspaces, e.g. OpenHands, Browser Use): non-root, read-only root filesystem, no host mounts except a per-run scratch volume, capabilities dropped, `no-new-privileges`.
2. **Network namespace** whose only routes are the Agent Gateway (model + tool endpoints) and, for `jarvis_gateway_plus_egress_proxy` runtimes, an egress proxy enforcing the operator `EgressPolicy` with checked-IP = connected-IP (`10` §5).
3. **No credentials in the container** beyond two run tokens. No provider keys, no user tokens, no SecretStore access. Runtime telemetry is disabled (the Browser Use `ANONYMIZED_TELEMETRY=false` class) and blocked by the netns anyway.
4. **Exact version pin** per runtime image; image digest recorded in the runtime profile; upgrades by PR.
5. **Kill path:** token revocation → `cancel_run` → container kill after 10 s.
6. **Reconciliation** at startup and every 10 minutes (§25.2).
7. **BR-T2 rows** measured for each container class.
8. A **JARVIS MCP server** (Tool Gateway transport) and an OpenAI-compatible Model Gateway listener bound to the container network only.
   *Owner decision OD-TOOL-3 (2026-10-02, register §2L): no MCP for the first provider. A P2 provider makes no Tool Gateway calls (its results are inbox data), so for Browser Use item 8 is the Model Gateway listener only; a P1 provider later needs a Tool Gateway transport and a new OD-TOOL-3 decision.*

None of these exist in the repository today (see the facts table), so Phase 6 is blocked by infrastructure, not by design.

**Owner decisions (2026-10-02, register §2L).** OD-AF-6: the first provider is **Browser Use, P2**. The mechanisms are ratified (2026-10-02): OD-AF-11 a rootless engine with gVisor `runsc`; OD-AF-12 no network interface, only per-run Unix sockets to the Model Gateway and the egress proxy; OD-AF-13 JARVIS's own CONNECT proxy on `server/net`, no TLS interception, authoritative; OD-AF-14 CI-built images pinned by digest; OD-AF-15 `browser.session`/`browse` at `low_write` on an explicit host list (the proxy cannot see methods inside TLS, hence not `low_read`). Browser Use's own `allowed_domains` and safety settings are advisory, never authorization: the JARVIS egress boundary is.

### 21.1 Two isolation patterns

| Pattern | For | Authority boundary |
|---|---|---|
| **P1 mediated tools** | runtimes whose effects are tool calls (native, framework-hosted, Letta, OpenClaw) | every tool call through the Tool Gateway |
| **P2 contained workspace** | runtimes whose primitives are shell or browser actions inside their own sandbox (OpenHands, Browser Use) | the boundary is the workspace: JARVIS authorizes what enters (files copied in, as `file.read` under the envelope), what the sandbox can reach (egress proxy allowlist), and what leaves (results as inbox data; any change to the owner's real files or accounts goes back through the Tool Gateway with the normal tiers, e.g. `file.write` / future connector writes with confirmation) |

In P2, JARVIS does not authorize each shell command, because the shell cannot affect anything outside a disposable container. That is safe only with items 1–3 in place. This is exactly why P2 providers are infrastructure-blocked.

---

## 22. Persistence

### 22.1 Tables `[IMPLEMENTATION-READY]` (Alembic, both SQLite and PostgreSQL, round-trip tested)

| Table | Phase | Key columns | Retention / deletion |
|---|---|---|---|
| `agent_definitions` | 1 | `agent_id` PK, `owner_user_id`, `graph_id`, `name`, `status`, `current_version`, `visibility`, `created_from_task_id`, `created_by_device_id`, timestamps, `deleted_at` | tombstone kept (ids only; `name` cleared on delete) |
| `agent_spec_versions` | 1 | `(agent_id, version)` PK, `spec_hash`, `spec_json` (canonical JSON), `created_at` | purged on delete |
| `agent_compile_previews` | 1 | `compile_id` PK, `owner_user_id`, `task_id`, `spec_json`, `expires_at` | purged after expiry (sweep) |
| `agent_runs` | 2 | `run_id` PK, `agent_id`, `version`, `task_id` (unique, FK `agent_tasks`), `kind` (`on_demand \| reminder_tap \| unattended`), `status`, `failure_code`, `started_at`, `finished_at`, `cost_total` | kept ≤ 90 days after finish, then ids only |
| `agent_run_usage` | 2 | `(run_id, usage_id)` PK, `usage_id` unique | follows `usage_events` retention |
| `agent_inbox_items` | 2 | `item_id` PK, `agent_id`, `run_id`, `owner_user_id`, `body` (≤ 16 000 chars), `created_at`, `read_at` | owner deletes; purged with the agent |
| `agent_notebook_entries` | 2 | §16.3 | purged with the agent |
| `agent_run_tokens`, `agent_gateway_nonces` | 3 | §11.3 | pruned 24 h after expiry |
| `standing_delegations` | 5 | §15.3 | kept for audit (no content) |
| `scheduled_jobs.agent_id` (nullable column) | 4 | data only | as scheduler |

All content columns are owner-private and in the OD-A1 (a) residual class, like `agent_tasks.response`. They are listed in BR-T2 when built.

### 22.2 Transactions

Follow the H-1 rule (register §2I): commit before each model call, tool run and provider call. A run's committed prefix survives a crash, and reconciliation closes `running` rows.

---

## 23. APIs

### 23.1 Agent tool capabilities (closed registry additions) `[IMPLEMENTATION-READY]`, tiers `[OWNER DECISION]` OD-AF-3

| Capability | Operation | Proposed tier | Resource type | Notes |
|---|---|---|---|---|
| `agent.define` | `compile` | low_read | `TOOL_ACTION` | pure; returns a preview |
| `agent.define` | `create` | **consequential** | `agentdefinition` (create) | confirmation card = spec |
| `agent.define` | `update` | **consequential** | `agentdefinition` (write) | card = spec diff |
| `agent.run` | `run_now` | low_write | `agentdefinition` (write) | starts a run as the present user |
| `agent.control` | `pause`, `resume` | low_write | `agentdefinition` (write) | |
| `agent.control` | `stop` | low_write | | always allowed while the agent exists |
| `agent.inspect` | `list`, `get`, `runs`, `inbox` | low_read | `agentdefinition` (read) | |
| `agent.delete` | `delete` | **consequential** | `agentdefinition` (delete) | |
| `agent.delegate` | `grant_standing` | **consequential + step-up** | | Phase 5 only |
| `agent.delegate` | `revoke_standing` | low_write | | Phase 5 only |

All `agent.*` capabilities are **excluded from every envelope** (§9.3). An agent run can never hold them.

### 23.2 HTTP endpoints `[IMPLEMENTATION-READY]` (Bearer; owner-only; `02` conventions: request_id, idempotency keys, `403 confirmation_required` + `X-Confirmation-Token` for consequential, anti-enumeration `404`)

| Method · Path | Phase | Body → Response |
|---|---|---|
| `POST /api/v1/agents/compile` | 1 | `AgentDraft` → `CompileOutcome` |
| `POST /api/v1/agents` | 1 | `{compile_id}` → `403 confirmation_required` → with token: `AgentView` |
| `GET /api/v1/agents` | 1 | → `{items: [AgentView]}` |
| `GET /api/v1/agents/{agent_id}` | 1 | → `AgentView` (+ current spec view) |
| `PATCH /api/v1/agents/{agent_id}` | 1 | `{compile_id}` (recompiled draft) → confirmation → `AgentView` |
| `DELETE /api/v1/agents/{agent_id}` | 1 | → confirmation → `204` |
| `POST /api/v1/agents/{agent_id}/pause` · `/resume` | 2 | → `AgentView` |
| `POST /api/v1/agents/{agent_id}/runs` | 2 | `{}` → `AgentRunView` (`202`, run started as a task) |
| `GET /api/v1/agents/{agent_id}/runs` | 2 | → `{items: [AgentRunView]}` |
| `POST /api/v1/agents/{agent_id}/runs/{run_id}/cancel` | 2 | → `AgentRunView` |
| `GET /api/v1/agents/inbox` · `POST /api/v1/agents/inbox/{item_id}/read` · `DELETE …/{item_id}` | 2 | owner inbox |
| `GET /api/v1/agents/{agent_id}/export` | 2 | → JSON (spec versions, notebook, inbox; no secrets) |
| `POST /api/v1/agents/{agent_id}/delegation` · `DELETE …/delegation` | 5 | grant (confirmation + step-up) / revoke |
| `GET /api/v1/admin/agents` | 3 | superuser, redacted (DSH-B1 rules) |
| `POST /api/v1/admin/control/agents/{agent_id}/pause` | 3 | superuser control (`18` §5.4 family) |
| Phase 6: `POST /gateway/v1/chat/completions`, MCP endpoint `/gateway/mcp` | 6 | run-token only; bound to the container network |

### 23.3 View schemas (user-safe)

- `AgentView {agent_id, name, status, current_version, template (id + description), runtime_display_name, model_profile_display_name, can: [str], cannot: [str], trigger_display, budget, created_at, last_run}`.
  - `can`/`cannot` are rendered from the envelope and fixed exclusion lists by deterministic code.
- `AgentRunView {run_id, agent_id, version, kind, status, failure_code, started_at, finished_at, cost_total, inbox_item_id}`.

---

## 24. Configuration `[IMPLEMENTATION-READY]` (additive to `15` §2; defaults are the safe reading)

```yaml
agents:
  enabled: false                       # whole feature off by default
  enabled_templates: []                # e.g. [research_digest, web_monitor_basic]
  max_agents_per_user: 5
  compile_preview_ttl_minutes: 15
  default_budget_per_run: 0.0          # 0 = local models only unless raised
  default_budget_per_month: 0.0
  delegation_max_days: 30              # Phase 5
  unattended_enabled: false            # Phase 5; refuses to load true unless standing_delegation_ratified: true
  standing_delegation_ratified: false  # set by the operator only after the PRD amendment lands
  model_profiles:
    - profile_id: general-agentic
      version: 1
      model_ref: agent.primary         # reference to an existing ModelEntryConfig key
      features: [agentic_reasoning, tool_calling, structured_output]
      context_window: 32768
      latency_class: medium
      supported_runtimes: [native]
      enabled: true
  model_profiles_open_to_all: [general-agentic]
  runtimes:
    native: {enabled: true}
    letta: {enabled: false}            # load fails if true while required_infrastructure is missing
    openhands: {enabled: false}
    browser_use: {enabled: false}
```

Load-time validation (fail closed):
- unknown template id;
- profile referencing an unimplemented provider;
- `cost_class` contradicting pricing;
- non-native runtime enabled without its infrastructure;
- `unattended_enabled: true` without `standing_delegation_ratified: true`.

---

## 25. Failure / Recovery

### 25.1 Failure table `[IMPLEMENTATION-READY]`

| Failure | Behaviour | Code |
|---|---|---|
| Draft malformed | compile refused, observation, audit | `agent.draft.rejected` |
| No template / runtime / model | `needs_clarification` or `rejected` with reason; nothing persisted | `no_template` / `no_runtime` / `no_model` |
| Provider unavailable at run start | run `failed` | `dependency_unavailable` (class `agent_runtime`) |
| Model chain exhausted | run `failed` (existing) | `worker_chain_exhausted` / `model_unavailable` |
| Stall / loop | existing `18` §4 detection | `stalled` |
| Budget | refused before the call | `budget_exceeded` / `agent_budget_exhausted` |
| Out-of-envelope call | denied observation; repeated denials → breaker (`18` §5.1 `denial_limit`) | `emergency_stop` after limit |
| Spec changed mid-run | run cancelled | `spec_changed` (new `AgentFailureCode`) |
| Runtime crash | run `failed`; no replay | `runtime_crashed` (new) |
| Server restart | reconciliation | §25.2 |

### 25.2 Reconciliation (startup; every 10 min for external providers)

1. Every `agent_runs` row in `queued`/`running`/`waiting` whose task is terminal or missing → run marked from the task's outcome, or `failed: interrupted`; tokens revoked.
2. For each enabled external provider: `list_runtime_agents()` minus active definitions' refs → `deprovision`; audit `agent.orphan.deprovisioned`.
3. Definitions whose `spec_hash` fails recomputation → `revoked`, `spec_tampered`.
4. Definitions whose selection is no longer satisfiable → `needs_reapproval`.

External agents never receive recovery authority. Recovery is the supervisor's (`18`).

---

## 26. Observability

| Signal | Where |
|---|---|
| Audit events | `agent.draft.rejected`, `agent.compiled`, `agent.created`, `agent.updated`, `agent.paused`, `agent.resumed`, `agent.deleted`, `agent.run.started`, `agent.run.finished`, `agent.envelope.denied`, `agent.notebook.write`, `agent.token.revoked`, `agent.orphan.deprovisioned`, `agent.delegation.granted/revoked/expired` — ids and reason codes only |
| Usage | `usage_events` + `agent_run_usage` |
| Judge trace | existing JDG-B1 with `agent_id`/`run_id` |
| Console (read-only) | `GET /admin/agents`: counts by status, runs per day, spend per agent (redacted names), failures by code, provider health |
| Selection explainability | `selection.selection_reason` stored in the spec; shown to the owner on request ("why this engine?") |

---

## 27. Testing

### 27.1 Required tests (test first; each lists its phase)

| ID | Test | Phase |
|---|---|---|
| AGENT-T1 | creating an agent requires `agent.define` activation and a consequential confirmation | 1 |
| AGENT-T2 | a draft containing any forbidden field (§9.2 list, parametrized) is rejected whole | 1 |
| AGENT-T3 | the compiled envelope ⊆ template ∩ requested abilities; at run time effective ⊆ owner's live grants | 1 / 2 |
| AGENT-T4 | no run can change its spec, envelope, tier or budget (no path; attempted update from inside a run is refused) | 2 |
| AGENT-T5 | `agent.*` never appears in an envelope; a run's request for `agent.define` is refused | 1 / 2 |
| AGENT-T6 | a run token is bound to one run, agent, spec_hash and purpose | 3 |
| AGENT-T7 | an expired token is rejected | 3 |
| AGENT-T8 | revocation makes the next gateway call fail (no cache) | 3 |
| AGENT-T9 | a user cannot read, run, pause or delete another user's agent (`404`) | 1 |
| AGENT-T10 | a deleted agent cannot run; reminder removed | 2 / 4 |
| AGENT-T11 | a simulated compromised runtime calling tools with forged fields (principal, capability, tier) is refused | 6 |
| AGENT-T12 | the runtime container reaches only the gateway (egress probe) | 6 |
| AGENT-T13 | no provider key appears in any container env, file or request (secret scan) | 6 |
| AGENT-T14 | every agent model call yields exactly one `UsageEvent` and one `agent_run_usage` row | 2 / 3 |
| AGENT-T15 | every agent tool call passes the envelope gate and `04` (instrumented engine counts) | 2 |
| AGENT-T16 | agent output containing permission-like text changes no authorization state | 2 |
| AGENT-T17 | agent run output never reaches Mem0, with `auto_extract` on | 2 |
| AGENT-T18 | deleting an agent removes its notebook, inbox and spec versions | 2 |
| AGENT-T19 | provider deprovision removes runtime state; reconciliation deprovisions orphans | 2 (native) / 6 |
| AGENT-T20 | the selector returns only enabled, compatible, healthy providers; property test: deterministic | 1 |
| AGENT-T21 | no compatible runtime → `no_runtime`, nothing persisted | 1 |
| AGENT-T22 | per-run and per-month budgets enforced; `0.0` refuses paid | 2 |
| AGENT-T23 | stop/cancel revokes tokens before the provider acknowledges | 3 |
| AGENT-T24 | revoking an owner grant affects the agent's next call | 2 |
| AGENT-T25 | template version change → equal/narrower auto-applied, wider → `needs_reapproval` | 1 |
| AGENT-T26 | the Judge cannot create, modify or authorize agents (import contract + API test) | 3 |
| AGENT-T27 | breaker trip stops an agent run | 2 |
| AGENT-T28 | Judge disabled → all agent tests pass | 2 |
| AGENT-T29 | an unattended run starts only within its delegation (cron, runs/day, budget, expiry) | 5 |
| AGENT-T30 | an expired, revoked or invalidated delegation refuses the run | 5 |
| AGENT-T31 | `observe`-mode agent cannot execute any `low_write` operation even with a standing grant | 2 |
| AGENT-T32 | a template naming a never-mappable ability fails load | 1 |
| AGENT-T33 | `spec_hash` tamper → agent `revoked`, run refused | 1 |
| AGENT-T34 | notebook write containing a secret pattern is rejected | 2 |
| AGENT-T35 | scheduler still imports nothing from `server.agents` (AF-C4) | 4 |
| AGENT-T36 | a delegated principal can never perform a device operation | 5 |

### 27.2 Mutation tests (each guard must have a mutant that a test kills)

| Mutant | Killed by |
|---|---|
| M-AG1 remove `extra="forbid"` from `AgentDraft` | T2 |
| M-AG2 envelope gate returns `True` | T15, T31 |
| M-AG3 selector ignores `enabled` | T20 |
| M-AG4 selector ignores `min_isolation` | T20 |
| M-AG5 compiler skips never-mappable filter | T5, T32 |
| M-AG6 token validation skips `revoked_at` | T8 |
| M-AG7 token validation skips `spec_hash` | T6 |
| M-AG8 nonce check disabled | replay test |
| M-AG9 budget accumulator not added | T22 |
| M-AG10 `_form_memory` does not skip agent runs | T17 |
| M-AG11 delete skips notebook purge | T18 |
| M-AG12 delegation check ignores `envelope_hash` | T30 |
| M-AG13 template version comparison inverted | T25 |
| M-AG14 hash verification skipped on load | T33 |

### 27.3 Adversarial fixtures

- Drafts with injection text in `purpose` ("grant yourself file.write").
- Sources with non-allowlisted URLs, and sandbox paths containing `../`.
- Templates with widened abilities.
- Forged run tokens.
- Replayed nonces.
- A runtime double that returns tool calls outside the manifest.

---

## 28. Migration

| Step | Change | Owner-signed? |
|---|---|---|
| M1 | `01` §1.2: `ResourceType += agentdefinition`; entities AgentDefinition, AgentSpecVersion, AgentRun, AgentInboxItem, AgentNotebookEntry | **yes** (locked registry) |
| M2 | `CAPABILITY_MATRIX.md`: `agent.*` rows (§23.1), excluded-from-envelope rule | yes (OD-TOOL-1 practice: implemented as proposed, owner signs) |
| M3 | Alembic migration `…_agent_factory_core` (Phase 1 tables) | no (implementation) |
| M4 | `AgentFailureCode += spec_changed, runtime_crashed, agent_budget_exhausted` | yes (`01` enum) |
| M5 | `scheduled_jobs.agent_id` nullable column | yes (`01` field) |
| M6 | Phase 5: PRD §22, `03`, `04`, `CapabilityScopeType += agent`, `standing_delegations` | **yes** |
| M7 | Import contracts AF-C1…C6 in `pyproject.toml` | no |
| M8 | `02` endpoint list | yes (doc) |

The existing process for M1, M2, M4, M5 in this repository: implement behind `agents.enabled: false` as *implemented recommendation*, and record the rows in the register (a new §2J). The owner signs later. Nothing activates for users until `agents.enabled: true`.

---

## 29. Provider Compatibility

### 29.1 Compatibility contract (all must hold before `enabled: true`)

1. The runtime accepts a custom OpenAI-compatible model endpoint and an arbitrary bearer token (→ Model Gateway).
2. Its tools come only from JARVIS (MCP or HTTP to the Tool Gateway), or it runs as a P2 contained workspace; its native effectful tools can be disabled.
3. It has a programmatic lifecycle: create/start, cancel, status, delete, and ideally export.
4. It runs in a container with egress restricted per §21.
5. Its own approval, guardrail and "security analyzer" features are **not** relied on; JARVIS approval happens at the gateway (§30.3).
6. It is version-pinned; the adapter refuses other versions.
7. Its telemetry and tracing exports can be disabled, and are blocked by the netns regardless.

### 29.2 Future templates by specialist class `[FUTURE]`

| template_id | Class | Runtime | Pattern | Extra requirements |
|---|---|---|---|---|
| `browser_monitor` | browser automation (read) | `browser_use` | P2 | browser sandbox container; egress proxy allowlist from operator policy; no stored credentials (`sensitive_data` unused in v1); `use_vision` allowed only for non-sensitive domains |
| `repo_maintenance` | coding maintenance | `openhands` | P2 | repository copied in from a granted sandbox label; results = a patch in the inbox; applying it = `file.write` with normal tiers; no push capability (a GitHub connector would be a new capability family) |
| `stateful_assistant` | long-running stateful | `letta` | P1 | per-owner Letta container; MCP tools → Tool Gateway; export/deprovision via Letta agents API |

---

## 30. Candidate Provider Research

Versions verified against PyPI / npm on **2026-09-30** (§30.4 sources). "Pin" is the version the adapter would be written against. Re-verify at implementation time.

### 30.1 Categories

- **Native:** JARVIS runtime.
- **General agent runtimes (own a server and state):** Letta, OpenClaw.
- **SDK / orchestration frameworks (libraries; a JARVIS-owned worker process hosts them):** OpenAI Agents SDK, Google ADK, LangGraph, Pydantic AI.
- **Specialized runtimes / agents:** OpenHands (coding), Browser Use (browser).

### 30.2 Assessment matrix

| | Native | Letta | OpenClaw | OpenAI Agents SDK | Google ADK | LangGraph | Pydantic AI | OpenHands | Browser Use |
|---|---|---|---|---|---|---|---|---|---|
| Version assessed | repo `ddbb038` | `letta` 0.33.8 (server), `letta-client` 1.12.1 | npm `openclaw` 2026.9.6 | `openai-agents` 0.22.3 | `google-adk` 2.10.0 | `langgraph` 1.2.12 | `pydantic-ai` 2.51.0 | `openhands-sdk` 1.50.0 (`openhands-ai` 1.11.0) | `browser-use` 0.13.10 |
| License | project | Apache-2.0 | MIT | MIT | Apache-2.0 | MIT | MIT | MIT (`openhands-ai`); SDK license field empty on PyPI — verify | MIT |
| JARVIS calls | runtime port | REST agents API | Gateway WS protocol / admin RPC | `Runner.run` in a worker process | `Runner.run_async` in a worker process | `graph.invoke/stream` in a worker process | `agent.run` in a worker process | agent-server REST/WS (conversations) | `Agent.run` in a browser container |
| Owns model loop | yes (JARVIS) | yes | yes | yes | yes | yes (graph) | yes | yes | yes |
| Persists state | volatile task + notebook | yes (Postgres) | yes (SQLite, workspace files) | sessions (pluggable) | session services | checkpointers | message history (app-held) | event-sourced conversations | browser profile (disable) |
| Tools / MCP | tool catalog | custom tools + MCP servers | native tools + plugins + MCP | function tools + MCP (stdio/SSE/streamable HTTP) | function tools + MCP toolsets | tools via LangChain; MCP adapters | tools + MCP toolsets | built-in terminal/editor/browse + MCP | built-in browser actions |
| Route model via JARVIS | yes | configurable endpoint `[verify]` | custom `models.providers.baseUrl` | custom model client / OpenAI-compatible base URL | model config (LiteLLM-compatible) `[verify]` | any chat model client | OpenAI-compatible provider | LiteLLM base URL | OpenAI-compatible chat client |
| Credentials kept out | yes | yes (only server password; JARVIS-held) | yes (JARVIS as sole operator; token = full admin) | yes | yes | yes | yes | yes | yes |
| Egress restrictable | n/a (in-process; `10`) | container netns | container netns | worker netns | worker netns | worker netns | worker netns | container netns (P2) | container netns + proxy (P2) |
| Cancellation | event + breaker | API | WS run cancel | task cancel of the worker coroutine | runner cancel | stop streaming + checkpoint | task cancel | `conversation.pause()` (takes effect between steps) + container kill | container kill |
| Export / delete | yes | agents API | agents.delete + workspace wipe | app-held session store | session service | checkpointer rows | app-held | conversation delete | container discard |
| Best fit | general repetitive agents | long-running stateful | none that JARVIS cannot do more safely (§30.3) | framework-hosted general agents | framework-hosted general agents | graph workflows with durable steps | typed tool agents | coding agents | browser agents |
| Security boundary introduced | none new | a stateful server with cross-user access by one password | one-operator gateway; history of critical CVEs | third-party code in a JARVIS worker | same | same | same | shell inside the sandbox | a browser inside the sandbox |
| Adapter needs | envelope gate, attribution | Letta adapter, MCP bridge, container | per-user container, tool denylist, WS client | worker harness, MCP → gateway, tracing off | same | same | same | agent-server client, P2 workspace | P2 workspace, egress proxy, telemetry off |

### 30.3 Findings that shape the design `[RESEARCH]`

1. **Framework approval features are not JARVIS confirmation.** All four frameworks offer human-in-the-loop approval: OpenAI Agents SDK `needs_approval`/interruptions, Google ADK tool confirmation, LangGraph `interrupt` with a checkpointer, Pydantic AI deferred tools with `requires_approval`. These are developer conveniences inside the untrusted runtime. Two sources show why JARVIS never relies on them:
   - an open Pydantic AI issue (#7537) reports a case where a queued message at a deferred-tool pause discards the pause, so the approval-requiring tool call proceeds without approval;
   - OpenHands documents that `conversation.execute_tool()` bypasses its analyzer and confirmation policy.

   **JARVIS approval therefore happens only at the Tool Gateway** (`human_approval_mode: jarvis_gateway`).
2. **"Security analyzers" are probabilistic.** OpenHands' analyzer is LLM-based risk scoring (LOW/MEDIUM/HIGH) with a confirmation policy, and its headless mode disables confirmation. It is useful inside the P2 sandbox; it is never authority.
3. **Browser Use enforces `allowed_domains` inside its own process.** Navigation outside the list is blocked by the library, and `sensitive_data` masks credentials from the model. JARVIS still enforces egress at the netns proxy (the library is inside the untrusted boundary). v1 browser templates use no credentials. Telemetry must be disabled (`ANONYMIZED_TELEMETRY=false`).
4. **OpenClaw is a single-operator system.** Its security model is one trusted operator per gateway; a gateway token is full admin; sandboxing is off by default; it has had critical CVEs. It is kept as a possible provider only under §21 with a per-owner container and all native effectful tools denied. Nothing in v1 or the first external provider depends on it.
5. **Letta is a stateful agent runtime** (memory blocks, archival memory, sleep-time agents), not a memory store for another runtime (doc 29 assessment, unchanged). It is the best fit for the `stateful_assistant` class.
6. **SDK frameworks overlap the native runtime** for general agents. Their value appears for specific patterns: durable graph workflows (LangGraph checkpointers; Temporal/DBOS integrations for Pydantic AI and ADK). They are hosted in a JARVIS-owned worker process, never in the gateway process: importing third-party agent code into the gateway would enlarge the OD-A1 blast radius.

### 30.4 Sources

- **Versions and licences:** PyPI JSON API (`openai-agents`, `google-adk`, `langgraph`, `pydantic-ai`, `letta`, `letta-client`, `openhands-ai`, `openhands-sdk`, `browser-use`) and the npm registry (`openclaw`), queried 2026-09-30.
- **OpenAI Agents SDK:** openai.github.io/openai-agents-python (human-in-the-loop, MCP, running agents, release notes); developers.openai.com guardrails and approvals guide.
- **Google ADK:** google.github.io/adk-docs (function tools, long-running tools); adk-python README (Task API, tool confirmation); Temporal ADK integration docs.
- **LangGraph:** docs.langchain.com (interrupts, checkpointers and durability modes, human-in-the-loop).
- **Pydantic AI:** pydantic.dev docs (deferred tools, `DeferredToolRequests`, capabilities); GitHub issue pydantic/pydantic-ai#7537.
- **OpenHands:** docs.openhands.dev (SDK conversation API: pause, confirmation policy; security and action confirmation); OpenHands SDK paper.
- **Browser Use:** docs.browser-use.com (browser parameters, `allowed_domains`, sensitive data, secure setup); Pydantic AI browser-use harness page.
- **OpenClaw and Letta:** as cited in the previous revision of this document (OpenClaw security, sandboxing, gateway protocol and custom providers docs; Letta SDK, sleep-time and Docker docs; CVE reports).

`[verify at implementation]`:
- Letta's and ADK's exact custom-endpoint configuration for an OpenAI-compatible proxy on the pinned versions;
- the OpenHands SDK licence field;
- how each framework disables default trace export (e.g. the OpenAI Agents SDK's default tracing to OpenAI's platform) on the pinned version.

---

## 31. Implementation Sequence

Each phase is test-first: tests from §27 → implement → run the full suite on SQLite **and** PostgreSQL → import contracts → mutation tests → BR-T2 when data classes change → commit → push → record in the register.

| Phase | Scope | Exit criteria |
|---|---|---|
| **0** | Register §2J rows for OD-AF-1…10 as pending; schemas in `shared/schemas/agent_factory.py` (provisional); template YAML format + README; ability table; conformance fixtures | schemas import; fixtures validate |
| **1** | Registries (templates, model profiles, native runtime profile); compiler; selector; `agent_definitions`, `agent_spec_versions`, `agent_compile_previews`; `agent.define` (compile/create/update), `agent.inspect`, `agent.delete`; APIs §23.2 Phase 1; AF-C1…C6 | T1, T2, T3 (compile half), T5, T9, T20, T21, T25, T32, T33; M-AG1, 3, 4, 5, 13, 14 |
| **2** | Native provider; `envelope.py`; on-demand runs; pause/resume/stop; inbox; notebook; attribution; export; memory exclusion | T3 (runtime half), T4, T10, T14, T15, T16, T17, T18, T19 (native), T22, T24, T27, T28, T31, T34; M-AG2, 9, 10, 11 |
| **3** | Agent Gateway modules (in-process), run tokens + nonces (data model and validation used by native for parity), Judge agent attribution and `agent.purpose` candidates, console views + control pause | T6, T7, T8, T23, T26; M-AG6, 7, 8 |
| **4** | Reminder-tap: scheduler job creation via port, `scheduled_jobs.agent_id`, `agent_reminders` channel feature, Android "Run agent" screen | T10 (reminder half), T35 |
| **5** | StandingDelegation, DelegatedPrincipal, unattended trigger loop, owner notifications | T29, T30, T36; M-AG12 |
| **6** | First external provider: **`browser_use`, P2** (OD-AF-6, ratified 2026-10-02); no MCP (OD-TOOL-3); slices 6A HTTP Model Gateway → 6B egress boundary → 6C container + namespace → 6D Browser Use adapter → 6E/6F integration, kill path, reconciliation, BR-T2 (OD-AF-11…15 ratified 2026-10-02) | T12, T13, T19 (external); BR-T2 container rows. T11 (forged tool-call fields) applies once a P1 provider uses the Tool Gateway over the network |
| **7+** | Further providers, one per concrete task class | per-provider suite |

> **Implementation note (Track B; not a ratification).** Engineering phases 3
> and 4 are implemented as one milestone, while retaining separate acceptance
> criteria. The phase labels and exit criteria above are unchanged: Phase 3
> (T6, T7, T8, T23, T26; M-AG6, 7, 8, with M-AG70…M-AG102 added) and Phase 4
> (T10 reminder half, T35; M-AG103…M-AG122 added) are each tested on their
> own. What was built, and where it departs from this document, is recorded
> in `DECISION_REGISTER.md` §2J. No owner decision in §32 is ratified by it.

**Exact Stage 1 implementation boundary** (for a coding agent), in scope:
- `shared/schemas/agent_factory.py`: `AgentDraft`, `TriggerRequest`, `SourceRef`, `AgentTemplate`, `AgentModelProfile`, `AgentRuntimeProfile`, `AgentSelection`, `CompiledAgentSpec`, `CompileOutcome`, `AgentView`, and the enums `TaskTag`, `AbilityName`, `ModelFeature`, `ModelPreference`, `TriggerKind`, `OutputKind`, `IsolationMode`, `InfraRequirement`, `AgentStatus`;
- `server/agents/registry/*`, `server/agents/abilities.py`, `server/agents/compiler.py`, `server/agents/selector.py`, `server/agents/service.py` (definitions, spec versions, previews, delete);
- `server/composition/agents.py`: the `agent.define`/`agent.inspect`/`agent.delete` tool adapters;
- the registry entries for those capabilities;
- the gateway router `server/gateway/routers/agents.py` (Phase 1 endpoints);
- the Alembic migration; config section `agents` with validation;
- templates `research_digest`, `web_monitor_basic`, `knowledge_keeper`, `file_organizer`;
- import contracts AF-C1…C6;
- tests and mutants listed for Phase 1.

**Out of Stage 1:**
- running agents (`agent.run`), `envelope.py`, the inbox, the notebook;
- gateway tokens, scheduler integration, delegation;
- any external provider.

---

## 32. Owner Decisions

| ID | OWNER DECISION REQUIRED | Recommended v1 value (not ratified) | Blocks |
|---|---|---|---|
| **OD-AF-1** | Ratify the Agent Factory abstractions (AgentDefinition, template/model/runtime registries, compiler, selector, Agent Gateway, AgentRuntimeProvider; no separate AgentStateProvider) | ratify | nothing in code (built behind `agents.enabled: false`); needed before `enabled: true` for real users |
| **OD-AF-2** | Amend PRD §22 to allow unattended execution under StandingDelegation (§15.8 wording) | yes, after Phases 1–4 ship | Phase 5 |
| **OD-AF-3** | Sign the `agent.*` tier table (§23.1) | as proposed | real-user enablement |
| **OD-AF-4** | Consequential actions in unattended runs | not allowed in v1 | Phase 5 scope |
| **OD-AF-5** | Default delegation lifetime | 30 days, step-up renewal | Phase 5 |
| **OD-AF-6** | First external provider | **Ratified 2026-10-02: `browser_use` (P2)** (register §2L) | Phase 6 |
| **OD-AF-7** | Per-user agent quota and default budgets | 5 agents; explicit non-zero budgets set by the operator | real-user enablement |
| **OD-AF-8** | Agent outputs beyond the owner's inbox (email, messages) | not in v1 | — |
| **OD-AF-9** | Attribution: join table (§12.4) vs extending `01` `UsageEvent` | join table | — |
| **OD-AF-10** | Adding `agentdefinition` to `ResourceType`, the new entities and enum values to `01` | approve | real-user enablement (code may land behind the flag, register practice) |
| (existing) OD-MT-2 | Cloud primary by default | unchanged here; agents inherit whatever model entries the operator configures | — |
| (existing) OD-TOOL-3 | Enabling MCP at all | **Decided 2026-10-02: none** for the first provider (register §2L) | — |
| **OD-AF-11…15** | Browser Use infrastructure: container mechanism, network namespace, egress proxy, image pinning, browsing capability/tier | **Ratified 2026-10-02** at the register's recommendations (§2L) | — |
| (existing) OD-JDG-5 | Cross-user Judge guidance | agent candidates are owner-scoped by design (§18); the global guidance question stays open | — |

---

## 33. Future / Darwin Boundary

- **Agent Factory:** *create and operate a reusable agent from a user goal.*
- **Darwin:** *search and optimize strategies against an evaluator.* `[FUTURE]`, not started.

Darwin may later propose changes to an AgentDefinition's **strategy fields only** (`purpose` wording, `sources` ordering, notebook conventions). Each change goes through the same `agent.define.update` path: a new version, a consequential owner confirmation, and a Judge/evaluator record. Darwin may never manipulate authorization, grants, envelopes, delegation, budgets, secrets, confirmation rules, templates, the selector or security floors. It would sit behind the IntelligenceProvider socket (PRD §25) or the worker slot (`18` §8), never inside the Agent Factory's authority path.

Also `[FUTURE]`:
- graph-shared agents;
- agent-to-agent messaging;
- child agents (would require depth-1 rules: child envelope ⊆ parent, budget carve-out, cascade cancel);
- external recipients;
- device execution by agents;
- streaming model responses through the gateway.

---

# Implementation Readiness

| Phase | Status | Why |
|---|---|---|
| **0** — contracts, schemas, fixtures | **READY TO IMPLEMENT** | documentation and provisional schemas; no canonical change |
| **1** — registries, compiler, selector, definitions, define/inspect/delete | **READY TO IMPLEMENT** | reuses existing activation, confirmation, resource-authorization patterns; new registry/`01` values land behind `agents.enabled: false` as implemented recommendations (register practice), owner signs OD-AF-3/10 before real-user enablement |
| **2** — native runs, envelope gate, inbox, notebook, attribution | **READY TO IMPLEMENT** | present-user principal (no new identity); `observe` mode already enforces read-only; envelope gate mirrors `modes.py` |
| **3** — Agent Gateway (in-process), tokens, Judge/console integration | **READY TO IMPLEMENT** | in-process only; the network surface is Phase 6 |
| **4** — reminder-tap | **READY TO IMPLEMENT** | scheduler contract unchanged (reminder carries data); needs one nullable `01` field (behind the flag) and an Android screen |
| **5** — unattended recurring agents | **BUILT** (off by default) | OD-AF-2/4/5 ratified, PRD §22 amended, DelegatedPrincipal in `03`/`04` (register §2K); implementation facts AF-P5-1…11 (register §2J) |
| **6** — first external provider | **6A BUILT**; **6B–6F READY TO IMPLEMENT** | OD-AF-6 (`browser_use`, P2), OD-TOOL-3 (no MCP) and OD-AF-11…15 (rootless engine + gVisor, per-run sockets, CONNECT proxy, digest pinning, `browser.session`) ratified 2026-10-02 (register §2L) |
| **7+** — additional providers | **FUTURE** | one per concrete task class, after Phase 6 |
| Darwin | **FUTURE** | separate subsystem, not started |

*End of 29. Proposal status unchanged: not canonical until the owner decisions in §32 are ratified.*
