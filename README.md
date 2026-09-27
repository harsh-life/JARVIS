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

## Current state: Phase H (final hardening and release validation)

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

**Still not built** (each is a later, separate subsystem; nothing in the
runtime depends on it): the operator **dashboard** (`/admin/*` views, docs/28;
the superuser control endpoints `/admin/control/*` do exist), the **Judge**
beyond its contract (docs/19: J1–J2 — the evaluation contract, TaskTrace,
providers, metering and the improvement-target registry — arrived in PR #23;
`evaluation.enabled` is `false` by default and nothing in the runtime or the
composition root calls it yet), **Darwin**, a real
**IntelligenceProvider** (only `GET /intelligence/status` → `{enabled: false}`),
account deletion (`DELETE /account`, 02 §3), and `PUT /config/agent` (02 §6).
Absent means absent: no capability, route or code path pretends otherwise.

### Before putting real data anywhere near this

`docs/OD_A1_BR_T2.md` records the **measured** blast radius under simulated
application-level RCE (BR-T2). It now covers every software dimension:
relational store, secrets, filesystem, egress, process execution, memory, and,
since Phase H, the Android device. The owner decided OD-A1 as **RESOLVED FOR
PILOT — ACCEPTED RESIDUAL** (option (a)): logical isolation, with the measured
in-process residual accepted for the pilot. That is not an isolation claim.

Real-user readiness needs 17 §5's whole release-blocking set green *and* the
OD-A1 gate decided. As of Phase H the gate is **still closed**. Some RB criteria
lack the required evidence: #29 has no dashboard, and #32 fairness under load
is not met on the single-writer store. At-rest BR-T2 rows (memory 27–28, push
token 36) wait for owner decisions. The Android client is validated only
against fakes, the JVM and Robolectric, not on a phone.
`docs/RELEASE_VALIDATION.md` §11 has the exact status. Until then:
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
                          tests/scheduler/, tests/voice/, tests/evaluation/, tests/dashboard/)
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
