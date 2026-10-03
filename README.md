# JARVIS / Hypermind Track B

A configurable, self-hostable personal-agent runtime. See
`Working Markdown/00_CANONICAL_PRD.md` for the full product and architecture
specification, and `Working Markdown/TRACK_B_ARCHITECTURE_INDEX.md` for how
the 17 subsystem documents relate to it.

The central invariant every branch of this codebase is built to preserve:

```
AGENT PROPOSES
    ↓
DETERMINISTIC INFRASTRUCTURE AUTHORIZES
    ↓
TOOLS EXECUTE
    ↓
HUMAN CONFIRMS WHERE REQUIRED
```

Model output is never the security boundary.

## Current state: Phase H (final hardening and release validation) and Stage 5 (Judge + operator console)

Everything below is merged: five foundation branches, the runtime foundation
(U0–U6), the memory build, the Android client (docs/23, Phases B–G), the
scheduler, and voice. Phase H audited the integrated tree, fixed what it found,
and recorded the evidence and the release-gate status in
**`docs/RELEASE_VALIDATION.md`**. It added no feature. The gate for real user
data is **not** open (see "Before putting real data anywhere near this" below).

**`foundation`** — the technical substrate: shared data contracts,
configuration, persistence/migrations, and the API skeleton (versioning,
request IDs, the error envelope, a health check). See
`docs/RUNNING_FOUNDATION.md`.

**`security-core`** — the deterministic security authority every later
branch must pass through:

- **Authentication** (`03`): Google OIDC with issuer/audience/state/nonce/PKCE
  validation, subject-keyed identity, Ed25519 device credentials with
  rotation/revocation/replay protection, opaque access tokens with immediate
  revocation, and step-up on sensitive operations.
- **Authorization** (`04`): the five-dimension engine — membership, role,
  ownership, visibility, capability — as the single place resource access is
  decided, with anti-enumeration surfaces and fail-closed behaviour.
- **Capabilities, risk tiers, the absolute floor, confirmation tokens** (`07`):
  a closed capability registry with enumerated operations, a deterministic
  tier table, prohibition by absence, and confirmations bound to one exact
  action.
- **SecretStore** (`12`): AES-256-GCM under an external KEK, handle-only
  access, master-key/superuser separation, and fail-closed resolution.
- **Audit primitives** (`01` §11.1) on every security-sensitive operation.

**`runtime`** — the deterministic agent runtime around that authority:

- **The loop** (`05`): propose → parse → authorize → confirm → execute →
  observe, under hard ceilings (iterations, model/tool calls, nesting, wall
  clock, budget, concurrency), each ending in an explicit failure.
- **Model providers** (`06`): one normalized interface; local Ollama by default;
  OpenAI-compatible adapters with keys resolved by handle at call time;
  LLM-as-a-tool through the same capability, authorization, and metering path.
- **Tools** (`07`): a registry that asserts every contract against the closed
  capability registry, per-platform adapters, and on-demand capability
  activation scoped to one task.
- **Usage, rate, budget** (`13`): one ledger; rates and budgets derived from it;
  fail-closed.
- **Memory hydration** (`11` §3): visibility pushed into the store query and
  re-checked with the engine's own predicate.

**`execution`** — the mechanism through which an authorized tool operation
becomes an actual operation on a target platform (`server/execution/`'s module
docstring: "Runtime owns orchestration. Security Core owns authorization.
Execution owns constrained execution."):

- **Filesystem sandbox** (`09`): real, `dir_fd`-walking, `O_NOFOLLOW`-at-every-hop
  path containment (not a `path.startswith(root)` check — see
  `server/fs/paths.py`), zip/tar-slip-safe archive extraction, per-file and
  per-sandbox size caps, least-privilege file modes.
- **Network egress** (`10`): default-deny, per-tool destination allow-listing,
  resolved-IP classification (metadata/loopback always blocked; private ranges
  only with explicit policy) before every connection, and a checked-IP-is-the-
  connected-IP guarantee against DNS rebinding (`server/net/client.py`).
- **Process execution** (`system.restricted`, `08` §6): `argv`-only (never a
  shell), a closed-by-default executable allow-list, a from-scratch
  environment, POSIX resource limits, and whole-process-group cleanup on
  timeout (`server/execution/process.py`).
- **Android/Shizuku** (`08`), server-side half: the capability→operation→
  primitive mapping and dispatch contract. When this branch landed,
  `UnavailableDeviceTransport` was the only transport. The authenticated device
  channel came later (see the Android client below); it is off by default, and
  the unavailable transport is what runs when it is off. The mapping is exported as the versioned
  shared artifact `shared/android/device_mapping.json` (docs/23 §5.1).

**`integration-hardening`** — a review of the composed system against the
canonical PRD and the locked decisions, fixing what only shows once the layers
meet: kernel confinement (Landlock + seccomp) for `system.restricted`, failing
closed where unavailable; graph admission only from a pending access request;
per-user idempotency namespaces and no confirmation token in stored replays;
stored model configs limited to SecretStore handles of class `model_api_key`;
suspended users stopped mid-task; tool calls bound to the authorizing device;
cancellable running tools; egress deadline/chunk bounds; per-principal fs
quotas. End-to-end acceptance cases live in `tests/integration/`, and BR-T2 was
re-run for the execution dimensions (`docs/OD_A1_BR_T2.md` §3b). Decisions are
in `docs/DECISION_REGISTER.md` §2B.

### Boundaries this codebase keeps distinct

- **Authentication ≠ authorization.** A valid token identifies a principal; it
  grants nothing. Every resource access still goes through the engine.
- **Capability ≠ tool ≠ adapter.** A capability is a closed-registry permission;
  a tool is a contract that declares which capability it needs; an adapter is
  the code that runs it. Registering an adapter grants no capability.
- **Model ≠ authority.** Model output is a proposal, parsed and authorized like
  any untrusted input.
- **Runtime ≠ execution.** The runtime orchestrates; execution performs the
  already-authorized operation under its own constraints.
- **Device ≠ trust root.** A phone is an execution target bound to a principal,
  not a source of authority.
- **SecretStore ≠ model context.** Secrets are resolved by handle at call time
  and never enter a prompt or an observation.
- **Sandbox ≠ path check.** Filesystem containment walks `dir_fd`s with
  `O_NOFOLLOW`; `system.restricted` gets a kernel ruleset, not a prefix check.
- **Logical isolation ≠ process isolation.** All users share one server
  process. Code running inside it reaches what it can reach — the accepted
  OD-A1 (a) residual. Nothing here is a claim of isolation under
  application-level RCE, and the build is not production-ready.

The owner's decisions (OD-A1, OD-D1, OD-E1, OD-F1, OD-TOOL-1) are recorded in
`docs/DECISION_REGISTER.md`; the capability/risk/confirmation matrix is
`docs/CAPABILITY_MATRIX.md`. See `docs/RUNNING_RUNTIME.md` and
`docs/RUNNING_EXECUTION.md` to run it.

**Memory build** — persistent memory and the Knowledge Vault (`11`,
`docs/21_MEMORY_PROVIDER_VAULT.md`; operator guide `docs/RUNNING_MEMORY.md`):

- **`MemoryProvider`** (`server/memory/provider.py`): the one interface JARVIS
  depends on. Providers store and retrieve; the authorization engine decides —
  `mem0fact` operations go through the same five-dimension engine as every other
  resource, and hydration re-checks every result with its `readable()` predicate.
- **Mem0 OSS, self-hosted, as a library** (`server/memory/mem0_provider.py`,
  pinned `mem0ai==2.2.1`, not forked): telemetry off, no Mem0 model calls
  (writes use no-inference mode; extraction, when enabled, is JARVIS's own
  metered call), an offline local embedder, no history file, and deletion that
  removes the text from the store files.
- **A deterministic write gate** (`server/memory/gate.py`): typed facts only; no
  secrets, emotional/relationship content, tool observations or payloads.
- **The Knowledge Vault** (`server/vault/`): Git-backed markdown, indexed from the
  committed tree into its own Chroma client and directory; no HTTP write path.

Disabled by default (`memory.enabled`, `vault.enabled`); the stack is the
optional `memory` extra.

**The Android client** (`android/`, docs/23, Phases B–G). Built: the wire
contract (`shared/schemas/device_channel.py`), the shared mapping artifact,
device-held Ed25519 keys (Keystore) registered by public key, the App Link
login return, and the authenticated device channel (`WS
/api/v1/devices/channel`, `server/execution/device_hub.py`) with exact-device
delivery, no queue, cancellation and revocation — **off by default**
(`android.enabled`); the device-side guard (docs/23 §5.2) held to the shared
conformance vectors on both sides; and the sensitive-app gate
(`android.app_classification` — every app unclassified by default, so no UI
control until the owner classifies one); and screen perception (docs/23 §6):
the Accessibility tree, bounded and with password fields redacted on the
device, then app metadata, then on-device ML Kit OCR only when the tree has no
readable text — plus battery and notification reads. Every device result is
validated server-side against its primitive's declared shape
(`server/execution/device_observations.py`, held to
`shared/android/perception_samples.json` on both sides) and rendered to the
worker as quoted, labelled untrusted data; none of it is stored or reaches
memory extraction. `capture_screenshot` is its own operation (its own grid
toggle, off by default; refused for any app not classified non-sensitive and
for FLAG_SECURE windows, one attempt, no retry); the image goes, in memory
only, to the vision model configured in `android.vision` (none by default —
the image is then dropped unread), and the agent reads only that model's
description; the call is budget-checked and metered as a model call. The UI
execution primitives (tap, directional scroll, typing, launching the named
app, back/home/recents/notifications) act only inside the app the operation
names, on the node an Accessibility selector found — never at raw coordinates,
and never typing into a password field. An operation refused because an
on-device dependency is missing (Shizuku after a reboot, Accessibility turned
off) puts the *task* into a bounded `waiting_for_platform` state — the
operation is never queued; when that device reports the dependency back, the
call is re-authorized from scratch (a consequential one asks for a new
confirmation) and a fresh operation is sent. Shizuku is used on demand for its
one typed primitive (`force_stop`, a fixed AIDL call — no shell, no argv): bound
for that call and released after, a lost binding reported as
`platform_unavailable` with a notification telling the user what to turn on.
Approving a `high_irreversible` action needs step-up by re-attestation: the
device signs a single-use server challenge with a Keystore key that only
unlocks after the user's biometric or device credential (registered only at
enrollment), because a background-refreshed access token proves nobody is
present. From the phone, a task is typed in and its result shown; a paused
action is shown on a confirmation card built only from the server's canonical
pending action (capability, operation, app, exact arguments — never model
prose), and the user's answer goes to `/agent/tasks/{id}/confirm` with the
server-issued token. The per-app grid is backed by the user's own
device-scoped, single-app capability grants (turning a toggle off refuses on
the phone at once, then revokes). Push wake is optional and **off by
default** (`android.push.provider: none`): with `fcm`, a phone that was offline
when an operation was sent is sent a content-free data message — exactly
`{"type": "wake"}` — and reconnects its authenticated channel; the operation
itself had already failed (`device_unavailable`, never queued), and only the
*task* waits, re-authorized when the channel is back. The phone binds its own
token, opts in explicitly, and never initializes Firebase otherwise. The
presentation layer (docs/23 §7) derives one content-free `PresentationState`
from the server's task answers and the device's state; the task panel, the
status header and an optional floating overlay (off by default; state only,
never focusable, never approves) all show it, and a waiting task is shown as
paused with what to turn on. The final on-screen character is deferred; it
will replace only the plain status indicator.

**Scheduler build** — task-linked reminders (`docs/22_SCHEDULER.md`, operator
guide `docs/RUNNING_SCHEDULER.md`). *A firing reminder delivers a message; it
never executes.*

- **Creation**: `POST /api/v1/jobs`, or `scheduler.create.create_reminder` inside
  a user-instructed `execute` task. The reminder's `task_reason` is the user's
  own words: typed, or the task input as bound by the runtime. The worker never
  writes it.
- **Firing**: re-checks the owner, the job and graph membership from live
  state, then delivers only to the owner's own devices. Offline devices get it
  on reconnect, and any push carries only `{"type":"wake"}`.
- **Durability**: the application database is the one job store. Misfires are
  delivered late within grace, or recorded `missed` and reported.
- **Boundaries**: two import contracts keep `server/scheduler` away from the
  runtime, tools, devices, authority, secrets, memory and the Judge.

**Voice build** — STT / TTS / speaker context (`docs/27_VOICE.md`, operator guide
`docs/RUNNING_VOICE.md`). *A voice is an input method, not an identity; voice is
detachable.*

- **On the device by default**: the phone's on-device recognizer (no network
  fallback) and system TTS. Raw audio never leaves the phone. Push-to-talk
  fills the task box, and the user sends it as an ordinary task.
- **Voice cannot confirm or step up**: a spoken "yes" never approves anything.
  `SpeakerContext` can never be an authorization signal, whatever pydantic
  path is used.
- **Optional server providers** behind `/api/v1/voice/*` (declared egress, key
  by reference, metered). Audio is held in request memory only and never
  stored.
- **Detachable**: every setting `null` → Track B unchanged (VOI-T1).

**Judge build** — evaluation and human-approved improvement (`docs/19_JUDGE_EVALUATION.md`,
operator guide `docs/RUNNING_EVALUATION.md`). *The Judge observes and scores. It
never authorizes, never executes, and never kills on its own authority.*

- **Optional, off by default** (`evaluation.enabled`): Track B is unchanged
  without it. The runtime never imports it — it notifies an observer port, never
  waits, and never reads anything back.
- **One task, one user, redacted**: the trace is the worker's own proposals, the
  observations it was fed, and the task's audited events (with each
  authorization's tier) — secret patterns redacted and audited before any call.
- **Strict output**: a typed `Evaluation` with no field that could carry
  authority; malformed output (including "approve"/"resume"/"authorize") is
  rejected and recorded, never coerced. Scores are records, never controls.
- **Stop only through the breaker**: a live `stop_requested` becomes a breaker
  `trip()` only with `may_request_stop` (off by default); the breaker enforces.
- **Metered on its own budget**: never charged to the task or to the user's own
  budget or rates (OD-JDG-2 open on the global budget).
- **Improvement under human oversight**: candidates only for a closed set of
  targets (worker guidance, tool descriptions, recovery bounds, rubric,
  suggestion template); security policy is refused at creation; nothing applies
  until a superuser approves; every change is a versioned, rollback-able row.

**Operator console** — `GET /api/v1/admin/*` (`docs/28_DASHBOARD_OPERATOR_CONSOLE.md`,
operator guide `docs/RUNNING_CONSOLE.md`). *The dashboard shows. Controls live
elsewhere.* Superuser only; ten read-only views (health, tasks, recovery &
breaker, break-glass, evaluations, usage, memory & vault, devices, audit,
configuration) with a persistent banner while break-glass is enabled or a global
stop is latched. Secret-free (handles plus whether they resolve) and
PII-redacted server-side; unredacted content is a separate, audited request.
Actions stay in `/api/v1/admin/control/*`, owned by the supervisor, break-glass
and the Judge's control layer. No browser UI ships (OD-DASH-2).

The owner decisions these builds leave open — OD-JDG-1..4, OD-DASH-1/2 — are
listed in `docs/DECISION_REGISTER.md` §2G/§3; none is ratified by being built.

**Agent Factory, Phases 1 to 4** (`docs/29`, `[PROPOSAL — NOT CANONICAL UNTIL
RATIFIED]`; off by default, `agents.enabled: false`). An ordinary user task can
turn a repeated goal into an owner-private agent *definition*: the worker
writes a strict `AgentDraft` (intent only — no owner, graph, capability, tier,
budget ceiling, runtime, model or endpoint), a pure deterministic compiler
selects an operator-enabled template, runtime and model profile and compiles an
immutable, hashed spec whose envelope is only a *ceiling*, and the owner
approves the deterministically rendered card (`agent.define.create` is
`consequential`). Definitions are listed, read, updated as new versions and
deleted through `agent.*` tools or `/api/v1/agents`, all decided by the
authorization engine. **Phase 2 runs an agent on demand, for its present
owner only** (`POST /api/v1/agents/{id}/runs`, empty body). The run is an
ordinary task of that owner, run by the native runtime provider in the spec's
mode, under the spec's bounds and budget. Every action goes through the
envelope gate (a ceiling, never a grant), then activation, the one engine and
confirmation. The definition and run are re-validated, fresh, at every step and
every tool call, so a delete, pause, stop, new version, revoked grant or lost
graph membership stops the run. Other Phase 2 pieces:

- the owner can stop a run and pause an agent (never confirmed), and resume it
  (re-checked and confirmed);
- an agent may ask for a model by role (`agent.model`: JARVIS picks a
  permitted, budgeted model tool, and the agent never sees a key);
- an agent has its own bounded, gated notebook, which is never Mem0;
- each result goes to the owner's inbox as plain bounded text;
- every model and tool call is attributed to its run (`agent_run_usage`) and
  counts against a monthly budget;
- the owner can export the agent, with no secret in the export.

**Phases 3 and 4** (built as one milestone, each with its own acceptance
tests) put every run behind the in-process **Agent Gateway** and add the
**reminder tap**:

- each run holds two short-lived, hashed, run-bound tokens; every model and
  tool request carries one with a fresh nonce, and the gateway re-checks the
  run, the agent and its spec hash, rejects stale or replayed requests and
  answers a repeated request with its stored result;
- the **Model Gateway** admits only the approved model profile (or a model
  tool the envelope names), re-checks the owner's model policy at every call,
  and enforces the agent's monthly budget live across concurrent runs; keys
  stay server-side;
- the **Judge** sees which agent a run was and may suggest a clearer wording
  of that agent's purpose — shown to its owner only, applied only by the
  owner's confirmed update, never by a superuser — and nothing else about an
  agent;
- the operator console lists agents and runs (redacted), and the operator can
  pause one agent through the existing stop; its owner cannot resume past it;
- an agent with a reminder trigger gets an ordinary private scheduler job
  ("Run agent: {name}") that carries the agent's id as data; firing delivers
  a message and runs nothing. On the Android app, "Run agent" opens the app;
  the owner presses Run, which is the same authenticated run as on demand
  (`reminder_tap`, one run per tap). The app also lists the owner's agents.

**Phase 5 — unattended agents** (PRD §22 as amended by OD-AF-2; off unless
the operator sets `agents.unattended_enabled: true`, which also needs
`agents.enabled` and `agents.standing_delegation_ratified`). An owner may
grant one agent a **StandingDelegation** (`POST /api/v1/agents/{id}/delegation`:
a confirmation bound to the exact terms, then a fresh device re-attestation;
`DELETE` revokes, never confirmed). It is a **ceiling, never a grant**: bound
to the agent's exact spec and envelope hash, schedule, explicit non-zero
budgets, ≤ 24 runs a day and an expiry of at most `delegation_max_days` (30).
The Agent Factory's own trigger loop — not the scheduler — runs the agent on
that schedule as a **DelegatedPrincipal** (owner, agent, delegation, run; no
device, no session), through the same gateway, envelope gate and engine. An
unattended run:

- uses only the owner's existing standing grants and never asks for a
  confirmation; anything above `low_write`, any device or app action,
  `system.restricted`, `agent.*` and `net.request` other than `get` are
  refused before the engine;
- is re-checked against its owner, graph, delegation, spec and run at every
  step, so a revoke, pause, new version, delete, lost grant or lost graph
  stops it; the global latch and the operator's pause stop it too;
- misses are coalesced (one run at most within the grace, never a catch-up
  storm), and the day's limit and the month's budget skip runs;
- reports only to the owner's inbox, with closed-code notices (missed,
  coalesced, skipped, expiring, expired, revoked, invalidated).

**Phase 6 (built, off by default)** adds one external runtime, Browser
Use (OD-AF-6, P2): a `browser_monitor` agent browses an explicit list of
hosts (`browser.session`/`browse`, `low_write`) in a rootless gVisor container
with no network interface — its model calls go only to the run's own Model
Gateway socket, its pages only through the run's own JARVIS egress proxy, and
its result only to the owner's inbox. It needs `agents.runtimes.browser_use`,
`agents.model_gateway`, `agents.containers` and a pinned image digest. Every stop — the owner's, the operator's, the deadline, a budget — revokes
the run's tokens first, then kills the container. Operator setup:
`docs/RUNNING_EXECUTION.md` §7; measured blast radius: `docs/OD_A1_BR_T2.md` §3g.

Agent output never becomes memory. MCP, Letta, OpenHands, OpenClaw,
child agents, external recipients and Darwin are not built. Decisions:
`docs/DECISION_REGISTER.md` §2J, §2K and §2L (OD-AF-2/3/4/5/7/8 ratified
2026-10-02; OD-AF-6 — Browser Use, P2 — and OD-TOOL-3 — no MCP — ratified
2026-10-02; Browser Use's infrastructure OD-AF-11…15 ratified 2026-10-02; OD-AF-1/9/10 open).

**Still not built** (each is a later, separate subsystem; nothing in the
runtime depends on it): **Darwin**, a real **IntelligenceProvider** (only
`GET /intelligence/status` → `{enabled: false}`), account deletion
(`DELETE /account`, 02 §3), and `PUT /config/agent` (02 §6). No browser UI for
the operator console (OD-DASH-2). Absent means absent: no capability, route or
code path pretends otherwise.

### Before putting real data anywhere near this

`docs/OD_A1_BR_T2.md` records the **measured** blast radius under simulated
application-level RCE (BR-T2). It now covers every software dimension:
relational store, secrets, filesystem, egress, process execution, memory, and,
since Phase H, the Android device. The owner decided OD-A1 as **RESOLVED FOR
PILOT — ACCEPTED RESIDUAL** (option (a)): logical isolation, with the measured
in-process residual accepted for the pilot. That is not an isolation claim.

Real-user readiness needs 17 §5's whole release-blocking set green *and* the
OD-A1 gate decided. The real-data gate was run on `8b15e39` (2026-09-28). Its
decision is **REAL-DATA GATE: CLOSED**. #29 now has evidence: the console's
DSH-T1..T5 pass and are mutation-tested. What blocks real data:

- The Android release-blocking set has not run on a phone. No device was
  available.
- At-rest BR-T2 rows wait for owner decisions: memory 27–28 and push token 36.
- A new authorized-path row: approved Judge guidance can carry one user's
  content to every user (row 38).
- The real-integration smoke was not run: it needs an OIDC client, a stable
  hostname and a phone.

#32 fairness is no longer on this list: since the H-1 owner decision
(`docs/DECISION_REGISTER.md` §2I) the pilot's runtime store is PostgreSQL, a
running task no longer holds the store against other users, and #32 is met on
it. SQLite stays for development and does not meet #32.

`docs/RELEASE_VALIDATION.md` §P has the exact blockers. Until they are cleared:
**disposable or test data only.**

## Repository layout

```
server/    the modular-monolith FastAPI application (one package per subsystem)
android/   the Android client (docs/23): :contract (pure-Kotlin wire contract and
           device-side guard) and :app; shares only shared/ with the server
shared/    schemas/  — canonical Pydantic data contracts, importable by both
                        server/ and the android/ client
           android/  — the versioned device mapping table, the conformance
                        vectors, the proof vectors and the perception samples
                        both sides read
tests/     pytest suite (tests/foundation/, tests/security_core/, tests/runtime/,
                          tests/execution/, tests/integration/, tests/memory/,
                          tests/scheduler/, tests/voice/, tests/evaluation/, tests/dashboard/,
                          tests/agents/)
docs/      RUNNING_*.md, OD_A1_BR_T2.md, CAPABILITY_MATRIX.md, DECISION_REGISTER.md
Working Markdown/   the architecture/PRD document package (source of truth)
```

The two halves of the authorization engine are deliberately independent
modules that never import each other (`16` §5): `server/graph` decides, and
`server/capabilities` supplies the policy it consults, meeting only at the
gateway composition root through the Protocols in `server/graph/ports.py`.
Keeping them apart is what stops the half that evaluates a capability check
from also being able to grant one.

The agent runtime (`server/agent`) is kept further still: it cannot import the
engine, the capability package, or the SecretStore at all. It reaches them only
through the Protocols in `server/agent/ports.py`, which the top-level
composition root (`server/composition/`) satisfies with the Security Core's own
objects — so there is no second authorization path to drift.

The execution layer (`server/execution`, `server/fs`, `server/net`) sits below
`graph`/`capabilities` in the same dependency graph and cannot import either —
it receives only an already-authorized `ExecutionRequest`
(`shared/schemas/execution.py`) and has no field, method, or import path that
could assert authority for itself. `server/tools`/`server/modeltools`/
`server/agent` are additionally barred from importing `socket`/`subprocess`
directly, so a tool adapter cannot open a raw connection or process that
bypasses `server.fs`/`server.net`/`server.execution` — see
`docs/RUNNING_EXECUTION.md` §3 for exactly what that guarantee does and does
not cover.

Module boundaries (who may import whom) are enforced mechanically via
`import-linter` — see `pyproject.toml`'s `[tool.importlinter]` section — and
`.github/workflows/ci.yml` runs that check, the test suite, the migration
round-trip, and the BR-T2 measurement on every pull request. A boundary
violation is a CI failure, not a review nicety (`16` §6). CI requires no
secrets of any kind.
