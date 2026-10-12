# PILOT_EXECUTION_PLAN.md
## JARVIS / Track B — from "subsystems that pass" to "a pilot a person can use"

**Written:** 2026-10-12 · **Audited against:** this branch, `main` @ `0597422` (Agent Factory Phases 1–6 merged).
**Scope:** assemble and validate the minimum complete Track B product. Not Phase 7. Not Darwin. Not Track A. No new framework.
**Authority order is unchanged** (`DECISION_REGISTER.md`): PRD > register > subsystem contracts > implementation > this plan. Nothing here edits a locked text; where a decision *amends* one, §A.3 says so and step M0 asks for the signature.

Every claim about the repo below was read from the code or its tests on this branch, not recalled.

---

## 0. What the audit found, in six lines

1. The **authority core and the runtime are done and tested** (1,809 test functions; PostgreSQL-green; import contracts; BR-T2). Do not touch them except where §C/§D say.
2. **No browser UI exists** and **no browser can log in**: the only session path is Google OIDC → bootstrap token → *device* registration with an Ed25519 key → `/sessions/token` (`server/gateway/routers/auth.py`, `sessions.py`). `DevicePlatform` has one value, `android` (`shared/schemas/enums.py:34`, `01` §1.2).
3. **Nothing can write a model configuration or a secret over HTTP.** `AgentConfiguration` rows are created only in tests; `SecretStore.set` has no route. `PUT /config/agent` (`02` §6) is on the README's "not built" list.
4. **Agents run on the operator's model, not the user's.** `ConfiguredModelResolver.resolve(agent_model_ref=…)` builds only server-config entries; `primary_model_ref` returns `None` for any owner with their own row, so only "open" profiles apply (`server/composition/models.py`, `server/composition/agents.py:400`). A per-user model page would silently not apply to agents unless this is wired.
5. **The user API has holes the dashboard needs**: no task list, no device list, no user usage/budget view, no `/me`, no bulk memory delete/export, no readiness probe (`/health` is liveness and deliberately always 200).
6. **Missing entirely**: character/presentation layer, generic image input (only the Android screenshot path exists), any MCP adapter (OD-TOOL-3 ratified *no MCP* for the first Phase 6 provider), deployment artifacts (no systemd/Caddy/compose/backup), admission control (any Google account that completes OIDC creates a user: `find_or_create_user`).

---

## A. Essential decisions to close

### A.1 Already decided — **not reopened** (cite, don't relitigate)

| Area | Decision of record |
|---|---|
| Invariant, OD-A1 (logical isolation, residual accepted for pilot), OD-D1 (SecretStore/KEK), OD-E1 (roles) | `DECISION_REGISTER.md` §1 |
| Confirmation / step-up / tiers / floor | OD-F1, OD-TOOL-1, `CAPABILITY_MATRIX.md` §2; PRD §15–16 |
| Agent Factory: definition, native runtime, 5 agents/user, unattended ≤ `low_write`, 30-day delegation, inbox-only output | OD-AF-2/3/4/5/7/8 (§2K) |
| Browser Use contained runtime | OD-AF-6, 11–15 (§2L); stays **off** in the pilot |
| Runtime store = PostgreSQL | H-1 (§2I) |
| Android = UI/perception/execution endpoint, never an authority; voice never confirms; scheduler never executes; Mem0 behind `MemoryProvider`, Vault separate | docs/23, 27, 22, 21 |

### A.2 Decisions to close now (24)

Disposition is my recommendation and becomes the register entry on your word (M0). `L` = LOCKED, `D` = DEFERRED, `R` = REJECTED.

| # | Question | Answer | Why it matters for the pilot | Exact architectural consequence |
|---|---|---|---|---|
| P1 | One primary model for the pilot? | **L** — one primary per user; every agent uses it. Fallback only via the existing `agent.fallback` / `recovery.chain` (operator-configured). | Simplest thing that makes "many agents under one model" true. | No multi-model routing work. `models_as_tools` stays off in pilot config. |
| P2 | Who configures the model? | **L** — a user picks from an **operator-published catalog** (`agent`, `agent.fallback` entries in `config.yaml`, which already carry endpoint + pricing) and may attach **their own key** stored in SecretStore. A user never chooses an endpoint. | Gives "configure through the dashboard" without SSRF: `models.py` already refuses a stored row that picks an endpoint or an unpriced paid model. | New `GET/PUT/DELETE /api/v1/config/model`, `GET …/catalog`, write-only `POST/DELETE /api/v1/secrets` (class fixed to `model_api_key`). Writes an `AgentConfiguration(scope=user)` row. No schema change. |
| P3 | Do agents follow the user's model? | **L** — yes. An agent whose profile is `agent.primary` runs on the **owner's resolved primary**. | Otherwise the model page lies for agents (finding 4). | Touches `ConfiguredModelResolver.resolve`, `AgentFactory.primary_model_ref`, and the Model Gateway's "exactly the configured provider/model" check (AF-P3-3). **The only change in this plan that reaches Phase 3–4 invariants**; it gets its own tests (§G). A user-scoped key resolves with `SecretRequester.user(owner)`; confirm against `12` §2 in the first commit. |
| P4 | Provider-replacement boundary | **L** — `ModelProvider` Protocol + `build_provider` + one config entry. Nothing else may name a vendor. | "Replaceable later" must be a test, not a hope. | Add one contract test: swap provider in config → full task passes with no code change (`tests/runtime`). |
| P5 | Remote primary allowed? (conflicts OD-MT-2 / PRD §19 "default local") | **L, amends OD-MT-2** — config choice; `config.example.yaml` stays local-Ollama, `config.pilot.example.yaml` ships a priced remote entry. | The owner's register note C1: a remote default fails every task while `per_task_budget`/`security.budgets` are `0.0`. | Pilot config sets non-zero `agent.bounds.per_task_budget`, `security.budgets.*`. Needs your signature (amends a ratified default). |
| P6 | Low-risk autonomous chaining | **L as OD-F1** — `low_read`/`low_write` auto inside activated capabilities, under `agent.bounds` (12 iterations, 16 model calls, 24 tool calls, 120 s). | Already the behaviour. | No change. Pilot keeps defaults. |
| P7 | Consequential confirmation / high-impact step-up | **L as OD-F1** — `consequential`: single-use token bound to the exact action, from any endpoint that can show the canonical card. `high_irreversible` and delegation grants: step-up by **device re-attestation**. | The browser has no user-presence key. | A web device can confirm `consequential`, **cannot** satisfy step-up. Enforced server-side by device platform in the confirm path, not by the SPA. |
| P8 | Prohibited actions | **L, no change** — the absolute floor; absence, not policy. | — | None. |
| P9 | Memory semantics | **L** — *create*: user-explicit (`POST /memory`) or the owner's own metered `memory.auto_extract` through the write gate; *retrieve*: permitted facts hydrated per task; *update*: `PATCH`/`DELETE`; **agents never write memory** (AGENT-T17). | You asked for "outcome → memory update". This delivers it without letting agent output become memory. | `memory.auto_extract: true` in pilot config (disposable data only — OD-MB-4 stays open). An agent result reaches memory only through a **user-initiated "Save to memory"** on an inbox item = a normal `POST /memory`. |
| P10 | Retrieval ranking | **L** — deterministic hybrid in hydration, **after** authorization and the `readable()` re-check: semantic score × recency decay × fact-type weight. **D** — model re-rank/compress of already-authorized candidates (adds a metered call per task, adds no authority). | "Not keyword-only" is met by semantic search today but it is score-only (`hydration.py`: `sort(key=score)`; `MemoryCandidate` has no timestamp or type). | `MemoryCandidate` += `timestamp`, `fact_type`; `Mem0Provider._candidate` fills them; one pure `rank()` in `server/memory/hydration.py`. No new package. |
| P11 | User control over memory | **L** — list/edit/delete/visibility exist; add bulk delete (confirmed, `consequential`) and export. | Pilot privacy promise. | `DELETE /memory` and `GET /memory/export` over existing `MemoryProvider.delete_all_for_user` / `list_facts`. |
| P12 | Mem0 + Vault | **L** — Mem0 OSS behind `MemoryProvider`; Vault separate, Git-backed, **no HTTP write path**. Dashboard shows Vault status and runs `vault/query`; it never edits it. | Already built (`docs/21`). | `memory.enabled`, `vault.enabled` true in pilot config; `python -m server.memory provision` is part of deploy. |
| P13 | Can agents actually run? | **L** — yes, native runtime, present-user on demand + reminder-tap. `agents.enabled: true` in pilot config. Letta/OpenClaw/OpenHands/Browser Use: **R for pilot** (reserved ids, not mandatory, not enabled). | The core claim of the pilot. | Config only. Add the 2 templates in §D. |
| P14 | Unattended / standing delegation | **D** — ships built, **off** (`agents.unattended_enabled: false`). Trigger to enable: Android hardware validation passes (the delegation grant requires device re-attestation). If enabled later, OD-AF-4/5/7 apply unchanged: ≤ `low_write`, ≤ 30 days, ≤ 24 runs/day, explicit non-zero budgets, inbox only. | "Every morning" is served by reminder-tap (a notification you tap) with zero new autonomy. | None now. |
| P15 | Default quotas / budgets | **L** — 5 agents/user (OD-AF-7). Template defaults 0.05/run, 1.0/month. Operator sets `security.budgets.per_user_daily_cost_limit` and `global_daily_cost_limit` **> 0** in the pilot config (the number is yours; zero means every paid call is refused). Dashboard shows the user's spend vs. limit. | Without it, the dashboard "usage" page is empty or the system is dead. | `GET /api/v1/usage` over the existing ledger. |
| P16 | Android as primary endpoint | **L** — and Android hardware validation (`RELEASE_VALIDATION.md` §G) is part of "Pilot Ready". The sensitive-app lists ship **empty**, so no UI control works until you classify apps (recommended starter in §E). | The pilot's perception/control claim is unproven until a phone runs it. | No code; owner input + a phone. |
| P17 | Tablet / desktop | **L** tablet = the same APK (layout check only). **D** desktop client (docs/24 plan intact). The browser dashboard is the pilot's "computer endpoint": tasks, results, confirmations, no device control. | Honours "prepare for desktop/tablet" without a rewrite. | `endpoint_class`-style distinction is just `DevicePlatform` (P18). |
| P18 | Browser as an endpoint | **L, amends `01` §1.2** — `DevicePlatform += web`. The browser generates a **non-extractable WebCrypto Ed25519** key, registers its public key through the **existing** bootstrap flow, then uses `/sessions/token`. Access token held in memory only. Capability ceiling for `web`: no `device.*`/`app.*` grants, no step-up (server-enforced). **R**: cookie sessions, password login (a second identity system; PRD auth is Google OIDC). | The website must be a *client of the same identity/runtime*, not a new auth path. | enum + Alembic migration; `server/auth/device.py` already accepts public-key registration; a `return_to=web` variant of the callback redirects to `/ui/#bootstrap=…` (fragment is never sent to a server — same trick as the App Link, docs/23 §3). Locked registry edit → needs your signature. |
| P19 | Admission to a pilot server | **L** — invite-only: `security.oidc.allowed_emails` (+ optional `allowed_domains`); a login from anyone else is `403` with no user row. | Today open signup + a paid remote model = open wallet. (Google's "Testing" consent screen is a second, external gate.) | One config field; check in `OIDCLoginFlow.complete` *before* `find_or_create_user` (preserves AUTH-T1: no mutation on failure). |
| P20 | Character / presentation boundary | **L** — a per-user `PresentationProfile` (name, persona text, tone, style, palette, avatar, reference images, voice hooks) is **presentation only**: (a) rendered by clients; (b) a length-capped, delimited *style block* appended to the worker prompt **after** the fixed system prompt. It is read by nothing in `server/graph`, `server/capabilities`, `server/secrets`, `server/security`, `server/agent` (policy side). Prosody/emotion model: **D**. | Adds personality with no path to authority. | Table `presentation_profiles`; `shared/schemas/presentation.py`; `GET/PUT /api/v1/config/presentation`; avatar/reference upload (≤ 512 KB, PNG/JPEG/WebP by magic bytes, stored because the user explicitly saved them). One import-linter contract: presentation may not be imported by the engine/policy packages. Prompt hook in `server/agent/context.py::system_prompt`. |
| P21 | Image input | **L** — `POST /agent/tasks` accepts ≤ 3 images, ≤ 4 MB each, PNG/JPEG/WebP verified by **magic bytes** (not `Content-Type`); held in request memory; passed as `ImageInput` (already in `server/models/provider.py`) to a model the operator marked `vision: true`; metered as a model call; **never** written to the idempotency store, audit, logs, memory extraction or disk; discarded at task end. No new capability (registry is closed): the gates are the existing model budget/rate and, for any resulting tool call, the usual engine. | Proper image input without a storage problem. | Add `vision: bool` to model entries; refuse with a typed error if the primary lacks it (never silently drop). Android already has the vision wiring for screenshots (`android.vision`) — reuse the provider path, not the screenshot capability. Known residual: image EXIF reaches a cloud model (stripping needs a decoder; **D**). |
| P22 | Voice | **L as built** — on-device push-to-talk → transcript → ordinary task; server STT/TTS providers **off** (OD-VOI-1); voice never confirms. Prosody into the character layer is **D**; the presentation profile carries only `voice` hooks (TTS voice/rate/pitch) mapped onto existing `/voice/*` config. | Transcript interaction is acceptable per your brief. | None beyond the profile fields. |
| P23 | MCP | **L, amends OD-TOOL-3 and adds one registry capability** — *inbound only, read-only, HTTP(S) only (no stdio, no subprocess)*: a small operator-pinned list (`tools.mcp_servers[]`: id, https URL, `secret_ref`, and an explicit list of tool names, each wrapped in a `ToolContract`). New capability `mcp.tool` with one operation `read` at `low_read`; users enable it through the ordinary `/capabilities` grant. Schemas are hashed at startup; a changed tool schema disables that tool. Results are rendered as quoted, labelled **untrusted data** (as device observations are). **R** for pilot: write/act MCP tools, outbound MCP server, stdio servers, a registry of servers. | You want MCP usable without a bypass. This keeps TOOL-004 literally true (wrapped contract, granted capability, egress-bound, audited). | New `server/tools/mcp.py` adapter (a `ToolAdapter`), egress via `server.net` to the pinned host only; registry edit in `server/capabilities/registry.py` + `CAPABILITY_MATRIX.md`; config model in `schema.py`. Tier table is deterministic; the model never sets a tier. |
| P24 | Deployment model | **L** — single host: Caddy (or a Cloudflare named tunnel) → uvicorn on `127.0.0.1` → PostgreSQL + a data dir (Mem0, Vault, SecretStore material) under systemd. Laptop and VPS use the **same** `config.yaml` schema with different values. A Dockerfile is optional packaging, not the primary path (Landlock/seccomp confinement needs the host kernel). **R**: Kubernetes, managed multi-tenant, any required paid infra. | You said "no purchase now". The one unavoidable external item is a **domain name** for a stable HTTPS hostname (Google redirect URI + Android App Link). | `deploy/` directory (§F). |
| P25 | Dashboard vs operator | **L** — User dashboard = static SPA, only a *client* of `/api/v1/*` with the user's token; no logic. Operator console = the existing JSON `/admin/*` + `/admin/control/*`; **no admin UI in the pilot** (OD-DASH-2 stays open/**D**). A superuser token never enters the SPA. | Keeps DASH-002 and the "dashboard is not the security boundary" rule. | SPA CSP forbids inline script; a test asserts the SPA bundle contains no superuser route and every admin route rejects a user token. |

**Also needed from you (inputs, not decisions):** the model/provider + a key for the pilot; the Google OIDC client; the domain; a phone; the sensitive-app starter list; the starting budget values.

### A.3 The four items that amend something already ratified

You must sign these specifically; everything else above is additive.

1. **OD-TOOL-3** (no MCP) → P23 (inbound read-only MCP, one new capability).
2. **OD-MT-2 / PRD §19** (default local) → P5 (remote primary permitted by config).
3. **`01` §1.2 `DevicePlatform`** (android only) → P18 (`web`).
4. **`CAPABILITY_MATRIX`** (closed registry) → P23's `mcp.tool/read`.

---

## B. Existing pieces that are already ready (use as-is)

| Need | Already built | Where |
|---|---|---|
| Login, device keys, sessions, step-up | OIDC+PKCE, Ed25519 device credentials, opaque tokens, re-attestation | `server/auth/`, `routers/auth.py`, `sessions.py` |
| Authorization / isolation | five-dimension engine; fail-closed; anti-enumeration | `server/graph`, `server/capabilities`, `tests/security_core` |
| Runtime loop, ceilings, recovery, breaker | propose→parse→authorize→confirm→execute→observe | `server/agent/`, `tests/runtime` |
| Task API + confirmation | `POST /agent/tasks`, `/confirm`, `/cancel`, `GET /agent/tasks/{id}`, idempotency | `routers/agent.py` |
| Model providers | Ollama + OpenAI-compatible; images in `ModelMessage` | `server/models/` |
| Agent Factory | compile → deterministic card → confirmed create; list/get/patch/delete; runs; cancel; pause/resume; inbox; notebook; export; reminder-tap; delegation (off) | `routers/agents.py`, `server/agents/`, `tests/agents` (≈ 45 files) |
| Memory | `GET/POST/PATCH/DELETE /memory`, status, hydration with visibility re-check, write gate | `routers/memory.py`, `server/memory/` |
| Vault | `GET /vault/query`, status | `routers/vault.py`, `server/vault/` |
| Scheduler | `POST/GET/DELETE /jobs`, cron with `CRON_TZ`, misfire/restart handling | `routers/jobs.py`, `server/scheduler/` |
| Permissions | `GET/POST/DELETE /capabilities` (the "grid") ; `GET /config/tools` | `routers/capabilities.py`, `agent.py` |
| Devices / Android | registration, channel, perception ladder, UI primitives behind the app gate, confirmation card, presentation state, push (off) | `routers/device_channel.py`, `android/` (140 Kotlin files; `:contract` 54 + `:app` 206 tests) |
| Operator | read-only console + control endpoints, break-glass, global stop | `routers/admin.py`, `control.py` |
| Judge | `evaluator: rules` needs no model | `server/evaluation/` |
| Test harness | fake OIDC, fake device, scripted model, 2–3-user fixtures, PostgreSQL mode | `tests/support.py`, `tests/fake_device.py`, `tests/dbsupport.py` |
| CI | SQLite + PostgreSQL jobs, import-linter, migration round-trip, BR-T2 | `.github/workflows/ci.yml` |

---

## C. Missing dashboard / configuration pieces

Each item is the smallest change that closes the gap. All new routes live under `/api/v1`, take `get_principal`, and let the engine decide; none holds policy.

| ID | Item | Files | Purpose | Depends on | Smallest change | Validation |
|---|---|---|---|---|---|---|
| C1 | `DevicePlatform.web` + web login handoff | `shared/schemas/enums.py`, `server/storage/models.py`, new Alembic revision, `server/auth/device.py`, `routers/auth.py` | The browser is a device like any other | P18 signed | enum value + migration; `return_to=web` redirect to `/ui/#bootstrap=` on the callback (fixed path, no open redirect); platform ceiling in confirm/step-up/capability paths | `tests/security_core`: web device registers, gets token, confirms a `consequential` action, is **refused** step-up and any `device.*` grant; migration round-trip on SQLite+PG |
| C2 | Admission control | `server/config/schema.py` (`OIDCConfig.allowed_emails/allowed_domains`), `server/auth/login.py` | Invite-only pilot | none | check before `find_or_create_user` | test: disallowed email → 403, **zero** user/device rows, audited |
| C3 | `GET /me`, `GET /ready` | new `routers/me.py`, `routers/health.py` | Home/status; real readiness (DB reachable, migrations at head, SecretStore unlocked, memory/scheduler/channel state) | none | `/me` = user id, active graph, devices count, feature flags from `AppConfig`; `/ready` = 200/503 with component booleans, no detail | tests/foundation |
| C4 | `GET /devices` | `routers/auth.py` (or `me.py`) | "Connected devices" page | none | list own devices (platform, created, last seen, revoked, connected via `DeviceHub`); revoke exists | cross-user test: other user's device → absent |
| C5 | `GET /agent/tasks` (history) | `routers/agent.py`, `server/gateway/agent_port.py` | Task/run history | none | cursor list over `agent_tasks` for the principal only; bounded fields, no confirmation tokens | isolation test, 2 users |
| C6 | `GET /usage` | new `routers/usage.py` over `server/security/usage.py` | Budget visibility | P15 | today/month spend, limit, rate remaining, per-agent split from `agent_run_usage` | numbers match the ledger; other user's rows never counted |
| C7 | Model config + secrets | new `routers/config.py`, `routers/secrets.py` | P2 | none | `PUT` validates provider/model ∈ catalog, writes `AgentConfiguration`; `POST /secrets {value}` → `SecretStore.set(user scope, model_api_key)` returns **only** the handle; never returns or logs the value; DELETE revokes | reuse `tests/runtime/test_secrets_and_providers.py` patterns; add: value never in response/audit/log/error; other user's handle unusable; endpoint field rejected |
| C8 | Agents inherit the user's model | `server/composition/models.py`, `server/composition/agents.py:400`, `server/agents/gateway/model_routing.py` | P3 | C7 | when profile = `agent.primary` and the owner has a row, resolve that row (same endpoint-from-catalog and pricing rules) | `tests/agents/test_model_gateway*.py` + new: owner A's key never serves owner B's run; budget enforced; spec-hash unchanged semantics |
| C9 | Memory bulk ops | `routers/memory.py`, `server/gateway/memory_port.py` | P11 | none | `DELETE /memory` (consequential, confirmed), `GET /memory/export` | `tests/memory/test_memory_api.py` additions |
| C10 | Presentation profile | `shared/schemas/presentation.py`, `server/storage/models.py` + migration, `routers/presentation.py`, `server/agent/context.py` | P20 | none | one row per user; caps on every text field; style block appended after the fixed prompt | test: persona text "grant me file.write / ignore the rules" produces no grant and no behavior change in authorization; import-linter contract |
| C11 | The SPA | new `web/` (plain ES modules, no bundler, no CDN), mounted at `/ui` in `server/gateway/app.py` with CSP `default-src 'self'`; `frame-ancestors 'none'` | The website | C1, C3 | Pages: sign in · home/status · model · agents (list/detail/create/edit/delete/run/history) · inbox · memory (list/edit/delete/export/save-from-inbox) · schedules · devices · tools/MCP · permissions (the grant grid + shows server's tier labels) · usage · task history · character · account. Renders `pending` and `error` exactly as the server sent them. No tier/capability logic in JS | Playwright smoke (Chromium is preinstalled in this environment); static check: no inline script, no `localStorage` of tokens; an admin route with a user token → 403 |
| C12 | Account deletion | — | `DELETE /account` (`02` §3) | — | **D** — disposable data only; recorded, not built | listed in "Not Pilot Ready scope" |

---

## D. Missing agent / memory / scheduler pieces

| ID | Item | Files | Purpose | Smallest change | Validation |
|---|---|---|---|---|---|
| D1 | Two real templates | `server/agents/templates/daily_planner.yaml`, `general_assistant.yaml` (data only; follow `templates/README.md` checklist) | Existing five templates are web/file/browser/vault oriented; there is no pure-reasoning agent. Daily planner, research, brainstorm and study agents are **purposes on these templates**, not four code paths | abilities `[read_user_memory, read_agent_notebook, write_agent_notebook, create_reminder]`; `run_mode: observe`/`execute`, `risk_ceiling: low_write`, `supported_endpoints: [android, web]`; add `web` to endpoint enum (rides C1) | `tests/agents/test_registry.py`, `test_compiler.py`, `test_selector.py` cover the new ids; a golden compile of "plan my day every morning" → reminder trigger + 0.05 budget |
| D2 | "Create an agent" UX | `web/` + existing `/agents/compile`, `POST /agents` | Two paths already exist: form → `AgentDraft` → deterministic card → confirmed create; and an ordinary task where the worker calls `agent.define` | no server code | e2e: both paths produce an `AgentView`; the card shown is the server's `CompiledAgentSpecView`; a tampered draft (owner, tier, capability) is rejected by the strict schema (already tested in `test_schemas.py`) |
| D3 | Hybrid retrieval | `server/memory/provider.py` (`MemoryCandidate` += `timestamp`, `fact_type`), `mem0_provider.py::_candidate`, `hydration.py` | P10 | pure `rank(candidates, now)`; deterministic; applied after `readable()` | `tests/memory/test_memory_hydration` style: a newer equal-relevance fact outranks; an unauthorized fact never appears regardless of score; MEM-T1 unchanged |
| D4 | Save-to-memory from inbox | `web/` only | P9 | calls `POST /memory` with the user's own words | gate rejects secrets/emotional content (existing `test_write_gate.py`) |
| D5 | Memory in the loop, end to end | pilot config + test | Prove retrieve → reason → act → outcome → update | `memory.enabled`, `auto_extract: true` (ordinary tasks only), embedder provisioned | `tests/memory/test_memory_runtime_real.py` extended: task 1 states a preference; task 2 (other device) is answered using it; delete it → task 3 does not |
| D6 | Scheduler for a person | existing `/jobs` + agent `reminder` trigger | "Every morning at 8, help me plan my day" | no server code: dashboard "Schedule" = `CRON_TZ=… 0 8 * * *` job (or the agent's reminder trigger, which makes the same job) → delivery to the Android device → **tap runs the agent** (`reminder_tap`). Web shows `last_firing` + inbox; browser push is **D** | `tests/scheduler` covers firing/misfire/restart; add one agent-reminder-tap e2e on the pilot config |
| D7 | Judge in the pilot | config | "Judge evaluation if enabled" | `evaluation.enabled: true`, `evaluator: rules`, `provider: null`; improvements stay unapproved | existing `tests/evaluation`; assert a pilot run produces an `Evaluation` row |

---

## E. Missing endpoint / input pieces

| ID | Item | Files | Smallest change | Validation |
|---|---|---|---|---|
| E1 | Android hardware smoke | `android/`, `docs/RELEASE_VALIDATION.md` §G checklist | A budget-class phone, the stable hostname, an OIDC client; run AND-T1..T8 + instrumentation; re-run BR-T2 rows 30–32 on hardware | evidence appended to `RELEASE_VALIDATION.md` (new section, not an edit of old rows) |
| E2 | Sensitive-app starter classification | `config` (`android.app_classification`) | Owner-signed. Recommended: **non-sensitive** = calculator, clock, a notes app, Settings (read-only flows); **sensitive/payment** = banking, payments, messaging, email, password managers; everything else unclassified = denied (the shipped default) | `tests/runtime/test_sensitive_app_gate.py` already holds; add the chosen list to the e2e |
| E3 | Image input — server | `routers/agent.py` (multipart or JSON base64), `server/agent` message assembly, `server/config/schema.py` (`vision: bool`) | P21 | tests: wrong magic bytes, oversize, 4th image, non-vision primary (typed error), image absent from idempotency replay/audit/log/DB dump, discarded after task |
| E4 | Image input — clients | `web/` (file input), `android/…/tasks/` + `ui/` (picker → same request) | One attach button in each task box | Robolectric test for the request shape; Playwright upload |
| E5 | Tablet | `android/` | Verify layouts on a tablet-size AVD/phone profile; no new code expected | screenshot check |
| E6 | Desktop | — | **D** (docs/24, DESKTOP_CONFORMANCE_PLAN stay as they are) | — |
| E7 | MCP adapter | new `server/tools/mcp.py`, `server/capabilities/registry.py` (+ matrix), `server/config/schema.py` | P23. Pin one or two read-only servers (e.g. a documentation/search server you run or trust). Startup: fetch tool list, hash schemas, register only the allow-listed names | tests (§G row "MCP"): egress only to the pinned host; oversize/slow result bounded; injected "instructions" in a result are rendered as data and cannot change a proposal; changed schema disables the tool; no ungranted call; every call audited |

---

## F. Missing deployment pieces

All under a new `deploy/` plus `docs/DEPLOYMENT.md` (one document; supersedes nothing).

| ID | Item | Purpose | Smallest change | Validation |
|---|---|---|---|---|
| F1 | `config.pilot.example.yaml` | The production configuration model, identical on laptop and VPS | pilot values from P1–P24; **no secrets**: `env:`/`secretstore:` refs only; `database_url` has no password (config already refuses one) | `tests/foundation/test_config.py` loads it; repo-secret-scan stays green |
| F2 | Bootstrap script `deploy/bootstrap.sh` | One command on a fresh host | venv, `pip install -e ".[memory]"`, `alembic upgrade head`, KEK generation printed once with custody instructions, embedder provision, superuser bootstrap | run twice (idempotent) in a clean container |
| F3 | PostgreSQL | Pilot store (H-1) | `deploy/postgres.md`: local cluster, `PGPASSWORD`/`~/.pgpass`, role with only this DB | CI `postgres` job already exercises it |
| F4 | systemd unit `deploy/jarvis.service` | Start/restart/readiness | `User=jarvis`, `Restart=on-failure`, `ExecStart=uvicorn … --host 127.0.0.1 --proxy-headers --forwarded-allow-ips=127.0.0.1`, `UMask=0077`, KEK from `LoadCredential=` (not env in the unit file) | kill -9 mid-task → restart → task failed closed (existing reconcile) — scripted check |
| F5 | Reverse proxy `deploy/Caddyfile` (+ cloudflared alternative) | HTTPS, HSTS, request size cap (image uploads), per-IP rate limit on `/api/v1/auth/*` and `/sessions/token`, no access log of query strings | one site block; `/ui` + `/api` + `/app/login` | `curl` checklist; Android App Link `assetlinks.json` served |
| F6 | Cloudflare/domain assumptions | Stable hostname without buying servers | document: a domain on Cloudflare, a **named** tunnel (a quick tunnel's random URL cannot be an OIDC redirect URI); Google OIDC client with that redirect; App Link host = same host; `server.base_url` set to it | the §I smoke uses it |
| F7 | Backup / restore `deploy/backup.sh`, `restore.sh` | Recovery | `pg_dump` + tarball of `data/` (Mem0, Vault, index) with 0600 perms; **the KEK is stored separately** (a backup without it is useless — by design, BR-T3); restore drill documented | scripted drill: restore into a scratch DB+dir → server starts → an existing user logs in via fake OIDC → a stored memory is retrieved → a SecretStore handle resolves |
| F8 | Logging / observability | Operate without reading user content | JSON log formatter (request id already exists via `RequestIdMiddleware`); uvicorn access log without query strings; `docs/DEPLOYMENT.md` says which `/admin/*` view answers which question | test: secret patterns absent from logs for a failing model call (extends `test_foundation_hardening`) |
| F9 | Optional Dockerfile + compose | Packaging only | app + Postgres; documented limits (confinement, no Browser Use) | not release-blocking |
| F10 | Health vs readiness | Process managers/monitors | C3 `/ready`; `/health` stays public liveness | tests/foundation |

---

## G. Hardening / test gaps

Principle: the architecture is not rewritten. Existing suites already cover most rows; the table lists the **evidence that exists** and the **gap to close** (only real gaps).

| Surface | Existing evidence | Gap to close |
|---|---|---|
| Auth / endpoint authentication | `tests/security_core/test_auth_identity_session.py`, `test_device_channel.py`, two silent-rejection paths audited (commit `d093578`) | **Unauthenticated throttling:** `RateLimitsConfig` is evaluated against the *usage ledger* (metered calls), so `/auth/*` and `/sessions/token` appear unthrottled — verify, then enforce at the proxy (F5) plus a small in-app limiter if verification confirms |
| Signup | none | C2 test |
| Dashboard/API authorization | engine tests per resource; `tests/dashboard` for `/admin` | **Route-inventory test** (none exists): enumerate `app.routes`; every non-public route → 401 unauthenticated; every `/admin*` → 403 for a user token; every `{id}` route → 404 for another user's id. One generic test covers the old and all new routes |
| Graph / user isolation | MEM-T1, graph lifecycle, `test_phase34_isolation.py`, ten-user concurrency | extend to the new routes via the inventory test; usage/devices/tasks lists show only own rows |
| Memory isolation | `test_br_t2_memory_rows.py`, `test_agent_memory_exclusion.py` | ranking (D3) cannot surface an unreadable fact; bulk delete/export are own-only |
| Secret containment | `test_secretstore.py`, `test_apk_secret_scanner`, repo scan | `POST /secrets`: value absent from response, audit, log, error body, idempotency store; handle of user A unusable by B; class forced |
| Tool boundary / filesystem / network-SSRF | `tests/execution/*`, BR-T2 execution rows | MCP (E7) SSRF + size + timeout; image upload size/DoS at the proxy and in-app cap |
| Confirmation races | `test_confirmation_boundary.py`, `test_run_control.py` | web device confirm; web device refused step-up; double-confirm from two devices |
| Cancellation / restart / recovery | `test_lifecycle_and_blast_radius.py`, `test_misfire_restart.py`, supervisor tests | scripted `kill -9` on the **deployed** topology (F4) + restore drill (F7) |
| Budget / rate | `tests/runtime`, `test_model_gateway.py` | per-user model key: a user with their own key cannot exceed the *operator's* per-user budget; P3 inheritance does not bypass the agent's monthly budget |
| Device boundaries | `test_device_hub.py`, `test_br_t2_android_rows.py` | a `web` device cannot receive a device operation |
| Agent containment | `tests/agents/*` (envelope gate, gateway tokens, replay, delegated principal) | the two new templates compile to ≤ `low_write` envelopes with no `net`/`file`/`device` reach |
| Malformed model output / provider failure | `test_units.py`, `test_worker_chain.py`, `test_secrets_and_providers.py` | provider-swap contract test (P4); vision-less primary typed error (P21) |
| Scheduler authorization | `tests/scheduler/*` (fire re-checks owner, job, graph) | reminder-tap e2e on pilot config |
| Presentation | none | injection test (C10); import-linter contract |
| MCP boundary | none (not built) | E7 tests |
| BR-T2 | `docs/OD_A1_BR_T2.md` (45 rows; re-measured in CI) | add rows for: web device session, secrets write endpoint, image upload, MCP adapter, presentation upload. Re-run; adjudicate any REACHABLE row under OD-A1 (a) exactly as before |

**Frontend rule, enforced:** the SPA never decides. A review checklist item plus the inventory test; the server's `pending`, `error`, tier and `failure_code` strings are rendered verbatim.

---

## H. Exact build order

Slices are small PRs; each lands green on SQLite and PostgreSQL, with `lint-imports` and the migration round-trip.

| Step | Work | Depends on | Exit |
|---|---|---|---|
| **M0** | Sign §A.3; transcribe P1–P25 into `DECISION_REGISTER.md` (one commit, new §2M); provide the inputs (model+key, OIDC client, domain, phone, starter list, budgets) | you | register updated; inputs in hand |
| **M1** | F1, F2, F3, F4, F5, F6, F10/C3 (`/ready`): run the *existing* server on the real topology (Postgres + Caddy/tunnel + systemd + stable HTTPS hostname) with OIDC and a real model | M0 inputs | `GET https://host/api/v1/ready` = 200 from a phone's browser. **Start this first: the phone, hostname and OIDC client are the longest-lead items and unblock E1** |
| **M2** | Backend API (parallel with M1): C2, C4, C5, C6, C7, C9, C10 | none | route-inventory test green; all new routes isolated |
| **M3** | C1 web device + login handoff; C11 shell (sign-in, home) | M1, M2(C3) | a browser signs in via Google, registers a `web` key, shows `/me` |
| **M4** | C8 agents inherit the user's model; D1 templates; D3 ranking | M2(C7) | owner-A/B isolation tests green; a "plan my day" agent runs on the user's model |
| **M5** | C11 pages: model, agents, inbox, memory, schedules, devices, tools, permissions, usage, history, character, account; D2, D4, D6 | M3, M4 | Playwright covers each page's happy path + one refusal |
| **M6** | E3/E4 image input (server → web → Android) | M2 | §G image tests |
| **M7** | E7 MCP read-only adapter + registry capability | M0 §A.3 signed | §G MCP tests |
| **M8** | E1/E2/E5 Android on hardware (can begin right after M1) | M1 | AND-T1..T8 evidence |
| **M9** | Hardening pass: §G gaps, BR-T2 re-run with new rows, F7 restore drill, F8 | M2–M8 | all §G rows closed or recorded as accepted residual |
| **M10** | End-to-end pilot (§I.2), evidence in `docs/PILOT_VALIDATION.md`. **Stop.** | all | "Track B Pilot Ready" (§I.1) |

Critical path: M0 → M1 → M8. Everything else is parallel behind M2.

---

## I. Definition of "Track B Pilot Ready"

### I.1 Gates (all must hold, each with evidence in `docs/PILOT_VALIDATION.md`)

1. **Decisions:** P1–P25 recorded; §A.3 signed.
2. **Automated:** full suite green on SQLite **and** PostgreSQL; `lint-imports` green (incl. the presentation contract); migrations round-trip + `alembic check`; BR-T2 re-run with the new rows, no new REACHABLE row outside the accepted OD-A1 (a) class (or each adjudicated); route-inventory test green.
3. **Deployed topology:** the system runs from `config.pilot.example.yaml`-derived config under systemd behind HTTPS on a stable hostname; `/ready` is truthful; backup + restore drill passes; a `kill -9` mid-task recovers fail-closed.
4. **Browser:** a person signs up (allow-listed), configures the model + key, creates an agent, runs it, sees its result, inspects/edits/deletes memory, schedules it, sees usage vs. budget and task history, revokes a device, edits the character — with **no authorization logic in the SPA** and all of it through `/api/v1`.
5. **Android on hardware:** AND-T1..T8 pass on a real phone (login, key-bound registration, task + result, confirmation card, screenshot/perception where supported, tap/swipe/type/launch/back/home on a classified app, allowlist off → refused at once).
6. **Image input** and **one read-only MCP tool** work end to end and fail closed on every §G negative case.
7. **Judge (rules)** produced an evaluation for a pilot run.

### I.2 The one pilot path (automated against fakes in `tests/integration/test_pilot_e2e.py`; repeated by hand on the real stack)

```
 1 allow-listed Google sign-in (browser)           → user + web device + session
 2 configure model (catalog entry + own key)       → AgentConfiguration + handle; key never echoed
 3 create agent "plan my day every morning"        → compile → card → confirm → AgentView (daily_planner)
 4 connect Android                                  → login handoff, key-bound device, channel up
 5 task 1 on phone: "I prefer deep work before 11" → auto-extract → one fact (memory)
 6 run the agent                                    → retrieves that fact (permitted), reasons on the user's model
 7 agent proposes a calendar-like note + a reminder → low_write automatic; a consequential step → confirmation card on phone
 8 device action on a classified app (tap/launch)   → engine → device guard → result
 9 attach an image to a task                        → vision model describes it; nothing persisted
10 MCP read-only tool call                          → granted capability, pinned host, data rendered untrusted
11 schedule "every morning at 8"                    → job; reminder fires to phone; tap runs the agent (reminder_tap)
12 audit + usage                                    → rows for each step; dashboard budget moves
13 Judge (rules) evaluation recorded
14 restart the server mid-run, restore from backup  → fail-closed recovery; user, memory and handle intact
```
Steps 4, 7(confirmation), 8 and 11's delivery need the phone and are the only manual parts.

### I.3 What "Pilot Ready" does **not** mean

- The **real-data gate stays CLOSED.** Pilot Ready is for disposable/test data. Still open: OD-MB-4 (memory at rest), H-2 (push token at rest, only if FCM is on), OD-JDG-5 (approved Judge guidance crossing users — keep improvements unapproved), OD-A1's accepted residual.
- Not built, by decision: Phase 7, Darwin / `IntelligenceProvider`, unattended agents (P14), desktop client, account deletion, admin UI, model re-rank of memory, EXIF stripping, write/act MCP, multi-model orchestration, Letta/OpenClaw/OpenHands/Browser Use, marketplace, wearables/IoT.

### I.4 After Pilot Ready (sequencing you set, unchanged)

Validate Track A independently → define the `IntelligenceProvider` boundary → validate Black Box Darwin separately → integrate through that boundary only → test the five improvement claims. Darwin never becomes the authority layer.

---

*End of plan. When I.1 holds, stop.*
