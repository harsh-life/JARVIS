# 30 — Intelligence Workbench (Phase 0)

**Status:** built on `feature/intelligence-workbench`. Owner-approved direction
(Phase 0 brief, 2026-11-06). **Configuration and control plane only:** nothing
here runs a model in production, authorizes anything or changes the
production request lifecycle. Darwin is still deferred. Track A's
IntelligenceProvider socket (PRD §25, `server/intelligence/`) is a different
thing and is untouched.

> Model proposes → deterministic infrastructure authorizes → tools execute →
> human confirms where risk requires. Selecting a model or an orchestration
> backend grants nothing.

## 1. What is functional, what is only configurable, what is a placeholder

| Part | State today |
|---|---|
| **Central, production** (the user's own model) | **Functional.** This is M2's `/config/model` + `/secrets`, reused unchanged. It is the only setting on this page that is active in production. |
| Central, connection test | **Not available.** The server has no user-facing provider probe; only the operator console can probe (`/admin/health?probe_models=true`). The UI says so. Not built here (it would be a new outbound, user-triggered call; see §8). |
| Experiment profiles (Central, orchestration, specialists, execution, context, evaluation) | **Functional configuration:** create, validate, version, export a snapshot, delete. **Not active in production.** A valid profile runs only in the isolated harness (`experiments/`, docs/31). |
| Orchestration backends: `deterministic_templates`, `model_planner` | **Integrated in the harness** (real adapters, run against `ModelProvider`s). Not in production. |
| `scripted_agentic_runtime` | **A scripted test double** of an agentic runtime, so the proposal-oriented contract can be tested. It is not an integration. |
| Letta, LangGraph, Jev, a JARVIS-native orchestrator | **Not integrated.** An operator may list one as a catalogue entry; it is then shown "Not integrated" and can never be run. No connector exists. |
| Browser sign-in | **Not built** (pilot step M3, C1 — `DevicePlatform.web`). The page accepts a pasted access token and keeps it in memory only. |

## 2. Where it lives

| Concern | Place | Why there |
|---|---|---|
| Shared contract: `IntelligenceProfileDocument`, `profile_hash`, `ExperimentSnapshot`, the state and backend vocabularies | `shared/schemas/intelligence.py` | the server and the harness use the *same* documents; `shared` imports nothing from `server` |
| Operator catalogue (`workbench:` in `config.yaml`) | `server/config/schema.py` (`WorkbenchConfig`) | operator-controlled, validated at load |
| Profile ↔ catalogue validation, component states, snapshot resolution | `server/config/workbench.py` | pure functions over config + shared; no storage, network or secrets |
| Persistence | `intelligence_profiles`, `intelligence_profile_versions` (migration `b3d5f7a9c1e4`) | owner-scoped, versions immutable |
| HTTP | `server/gateway/routers/intelligence.py` | authenticated principal only |
| Browser client | `web/` served at `/ui` (`server/gateway/web_ui.py`) | the C11 decision: plain ES modules, no bundler, no CDN, strict CSP |

`server/intelligence/` was deliberately **not** used: it is PRD §25's
Track A socket, disabled by default, and the runtime may not depend on it
(INTEL-003).

## 3. Operator vs user

**The operator controls** (in `config.yaml`, never over HTTP): which models
exist (M2's `agent.primary` / `agent.fallback`, plus experiment-only
`workbench.<id>` entries with their endpoint and price), which orchestration
backends are offered and their limits, which specializations and prompt
profiles exist, and the ceilings (`workbench.limits`).

**A user controls:** their production model (M2), and their own experiment
profiles: which catalogue entries to use, prompt profile and version, timeouts,
fan-out, budgets and context policy, all inside the operator's ceilings.

A user never supplies an endpoint, a price, an `env:` reference or an
identity. The document has no field for any of them, every model has
`extra="forbid"`, and an attempt is refused `422` with nothing echoed. A
backend's `endpoint_ref` is an operator label. The API reports only whether
one is configured. One catalogue, not two: experiment entries appear in the
same list as M2's but are marked `production_selectable: false`, and
`PUT /config/model` still accepts only M2's ids.

Model weights, inference endpoints, provider integrations and model
assignments stay separate: a catalogue entry names a provider adapter
(`server/models`), a model name and an operator-held endpoint; an assignment
names the catalogue entry. A future Hugging Face Inference Endpoint, a managed
inference API or a self-hosted server is a new catalogue entry (and, for a new
wire protocol, a new adapter in `server/models`), never a user-typed URL.

## 4. The profile document

`IntelligenceProfileDocument` (schema version 1):

* `central`: `catalog_id`, `prompt_profile` (`central.baseline` = the
  production runtime's own prompt, used for E0; `central.contract` = the E1 Central contract), `prompt_version`.
* `orchestration`: `backend_id`, `model_catalog_id` (required exactly when the
  backend needs a model), `timeout_seconds`, `max_fan_out`, `max_depth` (fixed
  at **1**: specialists never spawn specialists), `budget`.
* `specialists[]`: `specialization` (an operator catalogue id, not a closed
  enum, so new categories are data), `enabled`, `catalog_id`,
  `prompt_profile`/`prompt_version`, `output_schema`, `context_policy`
  (`evidence_subset` or `none`; `task_only` is refused for a specialist),
  `timeout_seconds`, `max_output_tokens`, `budget`.
* `execution`: `mode` (sequential / parallel), per-call and overall timeouts,
  per-call and overall budgets.
* `context_policy` (Central's), `evaluation` (`suite_id`, `rubric_version`,
  `repetitions`).

**Versioning:** every save is a new row in `intelligence_profile_versions`
with the canonical SHA-256 of the normalized document (`profile_hash`). A
version row is never updated; an ORM `before_update` listener raises
`ImmutableRowError`, and no code path updates one. A new version must name
its `base_version`; a stale one is refused `409`, so a concurrent edit is
never silently overwritten.

**Snapshots:** `GET …/versions/{v}/snapshot` re-validates the version against
*today's* catalogue and returns an `ExperimentSnapshot`: the document, its
hash, and the referenced catalogue entries resolved to provider, model and
price (**no endpoint, no key**). The harness verifies the hash on load and
records it on every run. Changing the profile afterwards cannot change a
snapshot that has already been exported.

## 5. Validation (`server/config/workbench.py`)

Stable codes, each with a path. **Errors refuse a save:** `unknown_model`,
`unknown_backend`, `model_required`, `model_not_used`, `model_not_allowed`,
`fan_out_exceeds_limit`, `timeout_exceeds_limit`, `budget_exceeds_limit`,
`budget_not_reserved` (orchestrator + specialist budgets above the overall
budget), `parallel_unsupported`, `external_state_not_permitted` (a backend
that keeps its own memory cannot run in this phase), `unknown_specialization`,
`schema_mismatch`, `unknown_prompt_profile`, `unknown_prompt_version`,
`context_too_broad`. **Warnings are shown and saved:** `not_integrated`,
`paid_model_without_budget`.

A saved version that no longer fits the catalogue (because the operator
withdrew a backend, for example) is shown `validation_failed` and cannot be
exported. It is not modified.

## 6. States the UI shows

`available`, `configured`, `connection_required` (a paid model; the harness
needs a credential from its own provider map), `unavailable`,
`experimental_only`, `not_integrated`, `not_active_in_production`,
`validation_failed`. `production_active` is `false` on every Workbench
response. `runnable_in_harness` is true only when validation passes and the
backend has an implemented adapter.

## 7. API

All routes authenticate first (route inventory), then answer
`503 dependency_unavailable {"dependency": "workbench"}` unless
`workbench.enabled`. Another user's profile is `404`, the same answer as for
no profile at all.

| Route | Does |
|---|---|
| `GET /intelligence/catalog` | models, backends (with `integration` decided by code), specializations, Central prompt profiles, limits |
| `POST /intelligence/validate` | `{document}` → `{valid, issues, config_hash, document, status}`; saves nothing |
| `GET /intelligence/profiles` | the caller's profiles |
| `POST /intelligence/profiles` | `{name, document}` → version 1 |
| `GET /intelligence/profiles/{id}` | profile, versions, latest (with validation against today's catalogue and status) |
| `POST /intelligence/profiles/{id}/versions` | `{base_version, document}` → new version |
| `GET /intelligence/profiles/{id}/versions/{v}` | one version |
| `GET /intelligence/profiles/{id}/versions/{v}/snapshot` | the `ExperimentSnapshot` |
| `DELETE /intelligence/profiles/{id}` | profile and its versions |

Audited: `config.intelligence_profile.created|versioned|deleted`.

## 8. Remaining decisions (owner)

1. **Browser sign-in (M3/C1).** Until it exists, the Workbench needs a pasted
   access token. Recommended next milestone.
2. **User-facing connection test.** Should a user be able to probe their own
   key? It would be a user-triggered outbound call. Proposal: `health()` only
   (no tokens billed), rate-limited, through the same adapter and endpoint as
   real calls, audited, and never echoing provider error bodies.
3. **Graph-scoped profiles.** Deferred with graph-scoped model configuration
   (OD-PILOT-2).
4. **Server-side experiment run records.** Runs are recorded by the harness
   (with the config hash), not in the production database. A server table
   would make the harness write to production, which this phase forbids.
