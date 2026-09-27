# Decision Register — owner decisions and runtime-branch proposals

**Authority order** (owner instruction, 2026-09-22):

```
canonical PRD (Working Markdown/00_CANONICAL_PRD.md)
  > locked decision register (this file, owner rows)
  > subsystem contracts (01–17)
  > implementation
  > proposals
```

`00_CANONICAL_PRD.md` §47 is the index of every open decision. This file records
**how** each one the owner has now decided was decided, and lists the `[PROPOSED]`
values the runtime branch had to choose. A `[PROPOSED]` row is *not* a lock; it
stands until the owner ratifies or changes it.

The PRD's canonical file name in this repository is
`Working Markdown/00_CANONICAL_PRD.md`. `TRACK_B_ARCHITECTURE_INDEX.md` refers to
the same document as `Track_B_PRD_Refined.md`.

---

## 1. Owner decisions (2026-09-22)

### OD-A1 — cross-user isolation under single-laptop application RCE

**Status: RESOLVED FOR PILOT — ACCEPTED RESIDUAL** · option **(a)** of 14 §4.

- The owner explicitly accepts, for the controlled pilot, the current
  **logical-isolation** architecture and its measured residual blast radius
  (`docs/OD_A1_BR_T2.md` §3): a compromised live application process may reach data
  and secrets that are already available to that running process.
- This is a **risk acceptance, not an isolation claim.** Logical isolation ≠
  physical process/store isolation under RCE. INV-20 still holds: the package does
  not claim cross-user isolation under application RCE, and the BR-T2 experiment
  keeps asserting the reachable rows *stay* reachable so the residual cannot quietly
  turn into a false claim.
- **Not weakened by this decision:** cross-user logical isolation is mandatory.
  The five-dimension engine, the visibility predicate, anti-enumeration, and
  handle-only secrets are unchanged, and every one of their tests still runs.
- **Intentional:** one user's multiple devices share that user's authorized state
  (tasks, grants, memory, configuration). A different user's private state stays
  isolated.
- **Deferred, not rejected:** (b) per-user process isolation and (c) per-user store
  isolation remain future hardening for a hostile-hosting or larger multi-tenant
  deployment. Not implemented in this branch.
- **What this does and does not open.** PILOT-004's OD-A1 gate is decided. Real
  user data still requires 17 §5's *whole* release-blocking set to be green, and
  that set includes suites whose subsystems do not exist yet (`09` FS-T9, `10`
  NET-T2, `11` MEM-T1 on a real Mem0 store, `08` AND-T*). Those are separate gates,
  not OD-A1. BR-T2 is re-run when `09`/`11` land; a reachable row *outside* the
  accepted class would go back to the owner.

### OD-D1 — SecretStore key custody / crypto specifics

**Status: RESOLVED** — as implemented in `server/secrets/` against `12`:
AES-256-GCM AEAD with the handle as AAD, DEK sealed under an external KEK supplied
out of band, handle-only agent access (the agent requester is refused
unconditionally), a separate superuser/master-key boundary, rotation, revocation,
and fail-closed resolution. Asymmetric (Ed25519) device credentials. Not reopened.

SecretStore key custody and persistent-memory encryption are **separate
boundaries**. Memory encryption is recorded as future hardening (§4), not folded
into OD-D1.

### OD-E1 — graph roles beyond owner/member

**Status: RESOLVED FOR MVP** — `membership.role ∈ {owner, member}`. No graph-level
RBAC beyond that. A system Owner/Admin is a separate principal (the superuser,
12 §4) and is not a graph role. User, device, session, graph, graph owner, graph
member, resource ownership, visibility, capability, authorization, and system
administrator stay distinct concepts. Richer roles later extend the enum without a
schema break; `_role_permits` already denies an unrecognised role.

### OD-F1 — confirmation policy

**Status: RESOLVED (policy)** · concrete mapping `[PROPOSED]` in
`docs/CAPABILITY_MATRIX.md` §2–§3.

Four tiers plus the absolute floor, exactly PRD §15: `low_read` automatic,
`low_write` automatic inside the activated boundary, `consequential` explicit
confirmation, `high_irreversible` strong confirmation (confirmation + step-up),
floor never. Confirmation sits at meaningful risk boundaries, not on every
primitive. No timeout auto-approves. The model never decides whether confirmation
is needed. Financial / account-security actions keep conventional controls:
explicit operation details, deterministic authorization, action-bound single-use
tokens with expiry (transaction binding + replay protection), step-up, scoped
credentials, audit, fail-closed.

(OD-F1 is the owner's identifier for this policy; PRD §47 tracks the same ground
under OD-TOOL-1's tier table and OD-AUTH-3's step-up window.)

### OD-TOOL-1 — exact risk-tier assignment per operation

**Status: RATIFIED AT THE SEMANTIC-CAPABILITY LEVEL** — capabilities are broad
semantic authorization classes the agent composes within; they are not command
menus. Concrete names are derived from the repository and documented in
`docs/CAPABILITY_MATRIX.md`. The per-operation tier table and the reserved floor
names remain `[PROPOSED]` until the owner signs them.

---

## 2. Runtime-branch proposals (`[PROPOSED]`, pending ratification)

| ID | Decision needed | Proposed value | Where |
|---|---|---|---|
| OD-02 | Agent-runtime bound values | `max_iterations` 12 · `max_model_calls` 16 · `max_tool_calls` 24 · `max_model_tool_nesting_depth` 1 · `wall_clock_timeout_seconds` 120 · `per_task_budget` 0.0 (paid calls refused unless raised) · `max_parse_retries` 2 · `max_observation_chars` 4000 | `agent.bounds` in config |
| OD-RT-1 | Max model-tool nesting depth | **1** — a model-tool is single-shot and cannot itself propose tool calls | `agent.bounds.max_model_tool_nesting_depth` |
| OD-RT-2 | Context compaction | Deterministic: keep the system prompt, the hydrated context, the user input, and the newest observations within `max_context_chars`; older observations are *dropped* and replaced by a fixed marker. No summarization, so nothing can be fabricated (RT-T9). | `server/agent/context.py` |
| OD-RT-3 | AgentConfiguration precedence | **user scope → graph scope → server config default**, first match wins. A user's model choice (and its cost) is theirs; a graph owner cannot switch another member's model. | `server/composition/models.py` |
| OD-USE-1 | Numeric rate/budget ceilings | existing `security.rate_limits` (60/min per user and per device, applied to metered calls) and `security.budgets` (0.0 — paid calls refused by default), plus concurrency: 1 task per session, 2 per user, 8 global; 600 metered calls/min globally | config |
| OD-USE-2 | Counter mechanism | Rates and budgets are **derived from the `usage_events` ledger** by query (USAGE-002); concurrency is an in-process gate (single-process pilot). A ledger read that fails is treated as at-limit (fail-closed). | `server/security/usage.py` |
| OD-USE-3 | Budget scope | per-user + global (13 §8's recommendation) | `server/security/usage.py` |
| (new) | Agent task record | `agent_tasks` table holds lifecycle only (owner, status, counters, final response or failure code). The working transcript and the paused action are **volatile** and never persisted (MEM-001); a restart fails a paused task closed. | migration `…_agent_runtime` |
| (new) | Resource-less tool operations | `ResourceType.TOOL_ACTION` + `Operation.CREATE`: authorized on capability, floor and tier alone; not loadable, so nothing can be read through it. Tools that touch a real resource (e.g. a `FileResource`) declare that resource type instead and get D3/D4. | `shared/schemas/authorization.py` |
| (new) | Model pricing | A non-local provider must declare `pricing`; a paid provider without pricing is a config error, because an unknown cost cannot be checked against a budget. | `server/config/schema.py` |
| (new) | Execution platforms | `server`, `linux`, `android` — engine vocabulary, not a `01` §1.2 registry value | `shared/schemas/agent.py` |
| (new) | Step-up for approving `high_irreversible` actions (03 §5.5, docs/23 §3) | **Re-attestation, not token age.** The device refreshes its access token in the background with its proof, so token freshness proves nobody is present. The confirm path now requires a signature over a single-use, 60 s server challenge by the device's **step-up key** — ECDSA P-256 in the Android Keystore, usable only after the user's biometric or device credential — within 5 minutes. The key may be registered only in the 15-minute window after the device was enrolled through the interactive login (or replaced after re-attesting with the old one), so a stolen device credential alone cannot plant one. A device without a step-up key (no secure lock screen) cannot approve tier-4 actions until re-enrolled. Other step-up uses (credential rotation) keep 03's token-freshness rule. | `server/auth/step_up.py`, migration `…_device_step_up_key` |
| (new) | Waiting for an on-device dependency (docs/23 §5.3) | Task status `waiting_for_platform`: a device operation refused `platform_unavailable` (e.g. Shizuku after a reboot) pauses the **task**, never the operation — nothing is queued on the device. Bounded: `max_platform_waits` 2 per task, `max_platform_wait_seconds` 600 each; expiry fails the task `platform_unavailable`. Resumed only when **that** device reports **that** dependency available; the call is then proposed again through every check and the engine (a revoked grant is honoured; a consequential action needs a **new** confirmation), and a fresh operation is built. Volatile like a paused confirmation: a restart fails it closed. | `server/agent/runtime.py`, migration `…_agent_task_waiting_for_platform` |
| (new) | The per-app grid's server side (PRD §13, docs/23 §5.2) | Each ON toggle is backed by a **device-scoped** `CapabilityGrant` narrowed to exactly `{package_name}`, created through the existing `POST /capabilities` (the server checks the scope id is the caller's own device). The capabilities come from the shared mapping (`screen_read` → `device.read` + `app.interact`; `ui_interaction` → `app.interact` + `device.ui_control`; `screenshot` → `device.read`), and are requested only where the cached classification would allow the operation at all. Turning a toggle **off** refuses locally first and then revokes. Turning one **on** authorizes nothing until the server records it. A failed sync is retried on the next connection. The grid revokes only its own grants: this device, one app, a mapped capability. It leaves user-wide or dashboard-made grants alone, and the device-side grid still refuses under them. **`device_state` (battery) is never synced:** `read_battery` takes no app, so its grant would be an unscoped `device.read` covering every app's screen at the server layer. That grant stays a deliberate owner action from the dashboard. A capability grant covers all its operations, so e.g. `app.interact` for reading also covers `tap` server-side; the per-toggle split is enforced by the device guard (two layers, 08 §4). | `android/.../permissions/GridSync.kt`, `contract/Grants.kt`, `shared/android/grant_samples.json` |
| (new) | Push wake (docs/23 §4, OD-AND-5 stays open) | **Optional and off by default** (`android.push.provider: none`). The only payload is `shared/schemas/push.py`'s `fcm_wake_message`: an FCM *data* message `{"type": "wake"}` plus fixed routing (high priority, 600 s TTL, one collapse key) — no task, operation, user content, id, token or authorization (ANDC-T9, tested at the model and at the HTTP transport). The phone binds its **own** registration token over its authenticated session (`PUT /devices/me/push-token`; one token → one device; another user's token cannot be taken over; revocation clears it). When an operation is sent to an offline device that can be woken, the operation **still fails at once** (`device_unavailable`, never queued — ANDC-T2) and a coalesced wake is sent; the *task* may wait in the existing bounded `waiting_for_platform` state (dependency `device_channel`) and resumes only when that device's channel re-authenticates — re-authorized from scratch, a fresh operation. The push itself resumes nothing and authorizes nothing. The server's sending credential is a Google service-account key, resolved per use from the SecretStore (server-owned, class `oauth_token` only) or an env var; its assertion goes only to Google's fixed token endpoint. On the phone the Firebase SDK is linked but never initialized unless the server offers FCM **and** the user opts in: its init provider is removed, auto-registration is off, and both the library receiver and our handler ship disabled. A wake only (re)connects the channel, never overrides the user's Disconnect, and is ignored by a revoked phone. *(Phase H: the scheduler build merged after this row and now uses the same waker for a reminder owed to an offline device — the same fixed wake; `tests/scheduler/test_firing.py::test_offline_wake_goes_through_the_configured_push_waker`.)* | `shared/schemas/push.py`, `server/execution/device_wake.py`, `server/auth/push_tokens.py`, migration `…_device_push_token`, `android/…/push/` |
| (new) | Android presentation layer (docs/23 §7, Phase G) | **Presentation shows; it never decides.** One content-free `PresentationState` (task status, pending risk tier, step-up needed, what a task waits for, device context incl. perception rung and push/platform state, break-glass, error kind, cancel/retry affordances) derived by one pure mapping from the server's task answers and the device's own state; precedence revoked > server task answer > connection; unknown server values never map to success. The current task lives in an app-scope tracker holding only the server's last answer: one task at a time (no duplicate submission), a retry only of an unanswered submission with the same Idempotency-Key, live tasks re-read every 3 s (the server resumes them — nothing is re-run client-side), only the task id persisted for re-attach. A waiting task is described as paused, never queued. **Overlay:** optional (off by default, `SYSTEM_ALERT_WINDOW` granted by the user), attached by the existing channel foreground service, never focusable, shows state only, hidden while JARVIS is in front; its actions are open app / cancel task / hide — **it never approves** (a floating approval could be tapjacked). Approval happens only on the in-app canonical confirmation card (unchanged: built from the server's pending action), which now states risk tier, step-up and expiry and drops touches while obscured (`filterTouchesWhenObscured`). Push-to-talk (docs/27) shows as *listening* / *recognizing* only while no task phase outranks it — display only, never read as an approval or a command. The final character is deferred: `StatusIndicator` reads signals only and is the one component it replaces (boundary tested on the source). | `android/app/.../presentation/`, `.../overlay/`, `.../tasks/TaskTracker.kt`, `.../ui/` |

---

## 2A. Execution-branch proposals (`[PROPOSED]`, pending ratification)

| ID | Decision needed | Proposed value | Where |
|---|---|---|---|
| OD-FS-1 | Filesystem containment mechanism (09 §8) | **`mediated`** — real, `dir_fd`-walking, `O_NOFOLLOW`-at-every-hop path resolution, TOCTOU-resistant by construction (the containment check *is* the syscall that does the work, not a separate stat performed earlier). `09` §8's `[REC]` `mount_isolated` mode (mount-namespace isolation) is not implemented — this branch has no privilege to create one in its development environment. `FilesystemSandbox` refuses to start if configured for `mount_isolated` rather than silently run the weaker mode under that name. | `server/fs/paths.py`, `server/config/schema.py`'s `FilesystemSandboxConfig.containment_mode` |
| OD-NET-1 | Network egress enforcement mechanism (10 §3) | **`mediated_proxy`** — a hand-built HTTP/1.1 client that resolves, classifies every candidate IP, and connects only to the checked IP (DNS-rebinding defense), rather than `netns_filtered` (kernel-level, `[REC]`). Same refuse-rather-than-misrepresent posture as OD-FS-1. | `server/net/client.py`, `NetworkEgressConfig.enforcement_mode` |
| OD-NET-3 | DNS-rebinding mitigation mechanism | Resolve once, classify every candidate address, connect to the exact IP that passed classification — never a second, independent resolution between check and connect. | `server/net/client.py` |
| (new) | `sandbox_root` scope-value semantics | Treated as an opaque **label** the operation is authorized against, never a filesystem path — even though 09 §1's own illustrative example writes it as one (`"sandbox_root": "/…"`). The physical root is always derived from the request's own authorized `user_id`/`graph_id`; the label only selects a sub-sandbox *within* that principal's own area. Accepting the scope value as a literal path would be exactly the "raw path from a client" 09 §1 forbids. | `server/fs/paths.py::allocate_root`, `server/fs/__init__.py` |
| (new) | fs operation addressing | `file.read`/`file.write` operations are authorized as resource-less `TOOL_ACTION`s (capability + tier + `resource_scope`), addressed by `sandbox_root` label + `relative_path` — not by a `FileResource.resource_ref`. Wiring individual-`FileResource` D3/D4 visibility into a physical read needs a DB session no `ToolAdapter.execute` receives; that integration is left to `11`/a future branch, not invented here. | `server/tools/platforms.py` |
| (new) | `net.request` capability | Added to the closed registry (`get` → low_read, `post` → consequential) now that `10`'s egress boundary exists to back it — `docs/CAPABILITY_MATRIX.md` §3.2 had withheld it for exactly this reason. Registered with a **closed-by-default** `EgressPolicy` (`execution.network.default_*` all empty/false); an operator opts a destination in. | `server/capabilities/registry.py`, `server/composition/execution_tools.py` |
| (new) | `build_application`'s default tool set | `extra_tools=None` (production's default) now builds the real execution tools from `ExecutionConfig` rather than none at all — `file.read`/`file.write` become immediately usable once granted; `net.request`/`system.shell` register but stay inert (empty destination/executable allow-lists); the Android tools use `UnavailableDeviceTransport`. An explicit `extra_tools` list, as every test passes, still fully substitutes. | `server/composition/__init__.py`, `server/composition/execution_tools.py` |
| (new) | `execution` module-boundary layer | Inserted between `graph \| capabilities` and `net \| fs` in the layering contract; `server.tools`/`server.modeltools`/`server.agent` are additionally barred from importing `subprocess`/`socket` directly (direct-import check only — `allow_indirect_imports = true`, since the legitimate dependency on `server.net`/`server.execution.process` necessarily uses them transitively). | `pyproject.toml`'s `[tool.importlinter]` |

**OD-A1 note.** The owner's `docs/OD_A1_BR_T2.md` decision already anticipated this: "BR-T2 is re-run when `09`/`11` land." `09` has now landed; `11` (Mem0) has not *(Phase H: both have, and so has the Android client — BR-T2 §3b, §3c, §3d)*. Re-running BR-T2 against the real filesystem sandbox, and any adjudication of a newly-reachable row outside the previously accepted class, is separate follow-up work this branch does not itself perform — OD-A1's pilot-acceptance decision (§1 above) is unchanged by this branch, not reopened by it.

---

## 2B. Integration-hardening decisions and proposals (`[PROPOSED]` unless marked)

The `integration-hardening` branch reviewed the composed system (Track B) against
the canonical PRD and the locked decisions above. Everything below either
**consumes** a locked decision without changing it, or is a proposal pending
ratification. None of it reopens OD-A1 or OD-D1.

| ID | Decision | Value | Where |
|---|---|---|---|
| OD-EXEC-1 | Kernel confinement for `system.restricted` | **Default `landlock`**: Landlock filesystem + TCP rules (read-only system dirs, read-write task temp root without EXECUTE, no TCP bind/connect, signal/abstract-socket scoping on ABI ≥ 6), a seccomp filter denying `socket()`, `io_uring_setup` and foreign/x32 syscall ABIs, `no_new_privs`, and `RLIMIT_FSIZE`. **Fails closed** (`PLATFORM_UNSUPPORTED`) where Landlock is unavailable — including every macOS host. This is a kernel boundary around one child process; it is not a container, and it is not process isolation for the server itself. | `server/execution/confinement.py`, `execution.process.confinement_mode` |
| OD-EXEC-2 | `confinement_mode: unconfined` opt-out | **Owner decision needed.** Proposed: permitted only for disposable-data development on hosts without Landlock; logged as a warning at startup; never with real data. BR-T2 §3b row 17 shows it makes another user's files reachable to an *authorized* command. *(Phase H status: `docs/20` records the owner's ratified task-bound break-glass form (2026-09-24); the code implements that form and the global switch no longer exists (BG-T2). Transcribing the ratified wording here is OD-BG-1, which waits for the owner's authorization — this row is left as written until then.)* | same |
| OD-EXEC-3 | Allow-listed executable identity | A bare allow-list name is resolved once at construction and exec'd by absolute path; model-supplied `PATH` cannot swap it. `LD_*`, `DYLD_*`, `GCONV_PATH` overrides are refused. | `server/execution/process.py` |
| OD-GRAPH-1 | `approve_member` semantics | The owner can admit only a user with a **pending access request** to a **shared** graph; a private graph is not joinable and an owner cannot enrol a user who never asked. Consumes OD-E1 (owner/member only); no new role. | `server/graph/service.py` |
| OD-IDEM-1 | Idempotency-key namespace | Client keys are namespaced by the authenticated user id (max 200 chars), so one user's key can never replay another's cached response. | `server/gateway/routers/graphs.py` |
| OD-IDEM-2 | Confirmation token at rest | The stored idempotency replay copy has `confirmation_token` nulled; only its hash is persisted (ConfirmationToken). A retry recovers the token through the owner-only task read. | `server/storage/idempotency.py`, `server/gateway/routers/agent.py` |
| OD-SEC-1 | `env:` references from stored rows | A stored `AgentConfiguration` may reference only a `secretstore:` handle. `env:` remains valid in operator config only — a stored row naming `env:` would otherwise ship the server's own environment (e.g. the KEK) to a provider. Consumes OD-D1 unchanged; no new secret mechanism. | `server/composition/models.py` |
| OD-SEC-2 | Model-key resolver class restriction | The resolver hands a secret to a provider only when its class is `model_api_key`; any other class is refused and audited `blocked`. | `server/composition/facade.py` |
| OD-ID-1 | Principal freshness | The runtime's per-step principal check also requires the **user** to be `active`, so a suspended user's in-flight task stops at its next step. | `server/composition/security_port.py` |
| OD-DEV-1 | Device binding of tool calls | Every `ToolInvocation`/`ExecutionRequest` carries the authorizing principal's `device_id`; Android operations without one are refused, and `app.interact` requires a `package_name` scope. The device is an execution target, never a trust root. | `shared/schemas/agent.py`, `server/execution/android.py` |
| OD-CAP-1 | Device/session-scoped grants | Grant listing matches the grant's scope to the caller's own user/device/session id exactly. | `server/gateway/routers/capabilities.py` |
| OD-NET-4 | Shared address space (`100.64.0.0/10`) | Classified non-global and refused unless the destination opts into private networks; total-deadline, chunk-size and chunk-header bounds added to the egress client. | `server/net/policy.py`, `server/net/client.py` |
| OD-FS-2 | Quota unit | Per principal (all of a user's private roots, or all of a graph's shared roots), not per label — a new label no longer resets the quota. | `server/fs/sandbox.py` |
| OD-RT-4 | Cancellation of a running tool | `/cancel` on a running task sets an event the tool call races against; the call is cancelled and its process group killed. A `/cancel` racing a `/confirm` resolves to exactly one outcome. Task temp roots are released when a task ends. | `server/agent/runtime.py`, `server/tools/registry.py` |

**Recorded residuals (not fixed here, not claimed fixed):**

- SQLite foreign-key enforcement stays off (`PRAGMA foreign_keys` is not set):
  audit rows deliberately reference ids that may not exist (probes of unknown
  graphs/users). Integrity is maintained by the service layer, not the database.
- `FilesystemSandbox` performs synchronous file I/O on the event loop; bounded by
  per-file and per-principal quotas, not by a thread pool.
- Filesystem and egress containment remain `mediated` / `mediated_proxy`
  (OD-FS-1, OD-NET-1): application-level for in-process code. BR-T2 §3b rows 13
  and 14 are inside OD-A1 (a); `mount_isolated` and `netns_filtered` stay future
  hardening (§4).
- ~~The Android device client (`08`) does not exist; its BR-T2 rows and
  release-blocking suite are pending.~~ *(Phase H: the client exists (docs/23,
  Phases B–G); its suites run in CI and its BR-T2 rows are measured —
  `docs/OD_A1_BR_T2.md` §3d.)* The real-data gate (17 §5) stays closed for the
  reasons in `docs/RELEASE_VALIDATION.md` §11: **disposable or test data only**.

## 2C. Memory-build proposals (`[PROPOSED]`, pending ratification)

The memory build (`docs/21_MEMORY_PROVIDER_VAULT.md`) implements the recommended
reading of every `[OPEN — OWNER]` item in docs/21 §8. None is ratified by being
implemented; each stands until the owner confirms or changes it.

| ID | Decision | Value implemented | Where |
|---|---|---|---|
| OD-MEM-A | PRD §41's `litellm/deepseek` Mem0 LLM | Read as "never default to OpenAI; provider is config". Stronger in practice: Mem0 has **no** model at all (a refusing stub), so no Mem0 provider or key is configurable. | `server/memory/mem0_provider.py` |
| OD-MEM-B | Pilot mechanism (a) or (b) | **(a)**: extraction by JARVIS through the task's own metered model call; Mem0 stores with `infer=False`. Off by default (`memory.auto_extract`). | `server/agent/runtime.py` (`_form_memory`), `server/memory/extraction.py` |
| OD-AUTHZ-1 | Shared facts on graph-leave | **Stay** shared; the leaver loses read access at once (membership is read live). | engine D1/D4 |
| OD-VLT-1 | API write path to the vault | **None.** Git commit + `python -m server.vault reindex` only; `vault.git_backed: false` fails to load. | `server/vault/`, `server/config/schema.py` |
| OD-MB-1 | Mem0 version | `mem0ai==2.2.1`, exact pin; the adapter refuses any other version. | `pyproject.toml` |
| OD-MB-2 | HTTP semantics of memory share/delete | The tier table already makes `mem0fact` share and delete `consequential`, so `PATCH`/`DELETE` return `403 confirmation_required` and run with the single-use token in `X-Confirmation-Token`. | `server/composition/memory.py` |
| OD-MB-3 | Emotional/relationship classifier (docs/21 §4 step 2) | Deterministic lexicon, reject-only, conservative. A model-assisted classifier can be added under the same reject-only contract. | `server/memory/gate.py` |
| OD-MB-4 | Memory at rest (BR-T2 §3c rows 27–28) | **Owner decision needed.** Store is plaintext with 0700 permissions; deletion removes text from every store file, but deleted facts' embedding vectors remain in Chroma's HNSW file until an index rebuild. Accept for the pilot, or require encryption / rebuild first. | `docs/OD_A1_BR_T2.md` §3c/§5 |

The real-data gate (17 §5) stays closed: the memory suites now run on a real
Mem0 store, but ~~the Android suite does not exist yet and~~ OD-MB-4 is open.
*(Phase H: the Android suites exist; see `docs/RELEASE_VALIDATION.md` §11.)*

---

## 2D. Scheduler-build proposals (`[PROPOSED]`, pending ratification)

The scheduler build (`docs/22_SCHEDULER.md`, operator guide
`docs/RUNNING_SCHEDULER.md`) implements task-linked reminders under docs/22 §0:
**a firing reminder delivers a message; it never executes.** Every row below is
the value implemented where docs/22 leaves a choice; none is ratified by being
implemented.

| ID | Decision | Value implemented | Where |
|---|---|---|---|
| OD-SCH-1 | `scheduler.create` name and tier | `scheduler.create{create_reminder: low_write}` in the closed registry — one entry to change. The agent tool is one switch (`scheduler.agent_tool_enabled`), independent of the API and of firing. | `server/capabilities/registry.py`, `server/composition/scheduler.py` |
| OD-SCH-2 | Fire-time `suggest` task | **Not implemented.** No principal at fire time, no automatic task. The notification's "Start task" only pre-fills the task box; the user sends it. | `android/…/reminders/` |
| OD-SCH-3 | Scheduled execution | `[FUTURE]`; nothing in the build can express it (no standing delegation, the scheduler cannot import the runtime). | `pyproject.toml` contracts |
| SCH-B1 | Backend | APScheduler's cron/one-shot **trigger semantics** over the application database as the one job store (`scheduled_jobs.next_fire_at`). APScheduler's own pickling `SQLAlchemyJobStore` is not used (a deserialization surface and a second source of truth). | `server/scheduler/backend.py` |
| SCH-B2 | `job.status` | Kept to the locked `active\|cancelled\|fired` (01 §1.2). Outcomes (`delivered`, `queued`, `undeliverable`, `missed`, `cancelled_recheck`) live in `scheduled_job_firings`; a failed fire-time re-check sets `cancelled`. | `server/storage/models.py` |
| SCH-B3 | Timezones | Carried in the `schedule` string: an ISO datetime must have an offset; a cron may be prefixed `CRON_TZ=<IANA zone> ` (UTC otherwise). The entity keeps its one field. | `server/scheduler/schedule.py` |
| SCH-B4 | Agent-path `task_reason` | The runtime passes the task's own `user_input` to a tool registered with `binds_task_input`; a worker-supplied `task_reason` is refused. Longer than `max_task_reason_chars` → cut at the bound (still a prefix of the user's words). Stored `reason_source=task_input`, `origin_task_id`, `created_by_device_id`. | `server/agent/runtime.py`, `server/composition/scheduler.py` |
| SCH-B5 | Agent-created jobs' visibility | `private`, `graph_id` null (the most restrictive): personal reminders. | `server/composition/scheduler.py` |
| SCH-B6 | Cancelling | `DELETE` is `consequential` in the tier table, so it returns `403 confirmation_required` and runs with the single-use token — the memory-deletion flow. Also withdraws reminders still queued for a device. | `server/composition/scheduler.py` |
| SCH-B7 | Quota | `max_active_jobs_per_user` 50 and `scheduler_creations_per_hour` 20, counted over the `scheduled_jobs` rows (never deleted, so create+cancel does not reset it); `LimitExceeded` → `429`; fail-closed. | `server/security/usage.py` |
| SCH-B8 | Bounds | Recurring reminders at most every 5 min; first fire within 730 days; reason ≤ 1000 chars. | `server/config/schema.py` |
| SCH-B9 | Misfires | Within `misfire_grace_minutes` (60): delivered, flagged `late`. Beyond: one `missed` firing row (folding every missed occurrence of a recurring job, `coalesced`), audited, reported in `GET /jobs` `last_firing`; no late notification hours after the fact. | `server/scheduler/firing.py` |
| SCH-B10 | Offline devices | Reminders queue per owner device (`reminder_deliveries`), re-checked at send time, re-sent until acknowledged, expired after 72 h. The wake payload is fixed to `{"type":"wake"}`. *(Phase H: when `android.push.provider: fcm` is configured, the reminder wake goes through the push waker; it is off by default and OD-AND-5 stays open.)* | `server/scheduler/firing.py` |
| SCH-B11 | Channel compatibility | Reminder frames go only to sockets whose `hello` declared `features: ["reminders"]`, so an older client never receives a frame it would reject. | `shared/schemas/device_channel.py` |

---

## 2E. Voice-build proposals (`[PROPOSED]`, pending ratification)

The voice build (`docs/27_VOICE.md`, operator guide `docs/RUNNING_VOICE.md`)
keeps docs/27 §0: voice is an input method, not an identity, and it is
detachable. Rows are values implemented where docs/27 leaves a choice.

| ID | Decision | Value implemented | Where |
|---|---|---|---|
| OD-VOI-1 | Any server STT at pilot | **None by default** (`stt: device`, `tts: device`). A server provider exists only when configured; a cloud STT then receives user audio, a disclosed choice. | `server/config/schema.py` |
| OD-VOI-2 | Wake word / always listening | `[FUTURE]`, not built. Push-to-talk only; leaving the app cancels listening. | `android/…/voice/SpeechInput.kt` |
| VOI-B1 | On-device recognition | Only Android's on-device recognizer (API 31+). Without one, voice input is unavailable — no fallback to the network recognizer, so raw audio never leaves the phone. | `android/…/voice/AndroidOnDeviceRecognizer.kt` |
| VOI-B2 | Transcript routing | A transcript only fills the task box; the user presses Send. It never reaches the confirmation card, and there is no voice task path or field. | `android/…/voice/VoiceRouting.kt`, `server/gateway/routers/agent.py` |
| VOI-B3 | Audio retention opt-in (LIFE-002) | **Not built.** `audio_retained` is always false and no switch exists. Building opt-in retention needs an owner decision on where retained audio lives and for how long. | `server/voice/service.py` |
| VOI-B4 | Speaker slots | `diarization` / `speaker_id` load only as null. `SpeakerContext` is frozen and re-validates on copy/construct, so `is_authorization_signal` is false through every door. | `shared/schemas/voice.py` |
| VOI-B5 | Server provider contract | OpenAI-compatible (`/audio/transcriptions`, `/audio/speech`). Declared-origin egress, no redirects, key by `secret_ref` (SecretStore class `model_api_key` only), pricing required unless loopback, one `model_call` UsageEvent per call. | `server/voice/openai_compatible.py`, `server/composition/voice.py` |
| VOI-B6 | Default TTS | `tts: device` placement; reading results aloud is a per-phone switch, **off** by default. | `android/…/voice/Speaker.kt` |
| VOI-B7 | Transcript persistence | None: `voice_events` stays empty (transcribe → process → delete); the transcript is returned to the caller only. | `server/voice/service.py` |

The donor `voice/Speaker` module (docs/23 §2) was not available to this build;
the TTS wrapper here is new code against Android's `TextToSpeech`.

---

## 2F. Phase H — final hardening findings (2026-09-27)

Phase H audited and validated the integrated tree; it added no feature. What it
found that needs a decision is listed here; everything else is in
`docs/RELEASE_VALIDATION.md`.

| ID | Finding | What the code does now | Status |
|---|---|---|---|
| H-1 | **PRD #32 (per-principal fairness under ~10-device load) is not met on SQLite.** A task's request holds the single write lock for the whole task, so while one user's task runs, another user's write is refused `503 storage` (explicit, retryable, nothing persisted). Already documented as a deployment limit (`RUNNING_RUNTIME.md` §4a); Phase H measured it at pilot size (`tests/memory/test_pilot_concurrency.py`). | Unchanged — the documented limitation. Per-principal concurrency caps hold; isolation under concurrency holds. | **OPEN — owner/engineering**: a multi-writer store (no PostgreSQL driver is declared or tested today, so "a config change" is not validated) or shorter write transactions in the runtime (changes FAIL-001's retry semantics; needs design) |
| H-2 | Push registration token stored in plaintext (BR-T2 §3d row 36, at rest). Usable only with the server's separate FCM credential, and only for the content-free wake. | Plaintext column; cleared on revocation. | **OPEN — owner** (low severity): accept for the pilot, or encrypt the column |
| H-3 | A symlink swapped in at a sandbox leaf between the last check and the open escaped as a raw `OSError` (task failed as an internal error) instead of a typed `FORBIDDEN_PATH`. Containment itself held (`O_NOFOLLOW`). | **Fixed** (`server/fs/sandbox.py::_open_leaf`), race tests added. | IMPLEMENTED (defect fix, not a decision) |

## 3. Genuinely unresolved owner decisions

| ID | Question | Why it is the owner's |
|---|---|---|
| (matrix §5.1) | Sensitive-app classification for UI primitives | A tap can complete a payment. Which apps are "sensitive" and how far their tiers rise is a product-risk call. Needed before `08` ships device control. *(Phase H: the mechanism is implemented (`android.app_classification`, server gate + device guard) and the lists ship **empty**, so no app is UI-controllable until the owner classifies one. The lists themselves are still the owner's.)* |
| OD-TOOL-3 | Which MCP servers (if any) are enabled at pilot | Default none; unchanged. |
| OD-DP-9 | Ratify DecisionProvider at all | Unchanged; nothing in the runtime depends on it. |
| OD-MT-2 | Any cloud primary by default | Default local (ollama); unchanged. |
| OD-SCH-1 | Ratify `scheduler.create` / `low_write` | Implemented as proposed (§2D); the owner signs the tier table. |
| OD-SCH-2 | Fire-time `suggest` task with a sessionless principal | Not implemented (docs/22 recommendation); would introduce a principal with no session. |
| OD-VOI-1 | Whether any server STT provider is enabled at pilot | A cloud STT receives user audio; default is on-device only (§2E). |
| VOI-B3 | Opt-in raw-audio retention (LIFE-002) | Not built; where retained audio would live and for how long is a privacy call. |

---

**Phase H consolidation.** Every decision still open anywhere in the package —
this table, the `[OPEN — OWNER]` items of docs 18–28, the proposals of §2–§2E
that are implemented but not ratified, and the Phase H findings of §2F — is
listed in one place, with its status in code, in `docs/RELEASE_VALIDATION.md`
§10. Implementing a recommendation there is labelled *implemented
recommendation*, never *owner decision ratified*.

## 4. Future hardening (recorded, not in scope)

- Per-user process isolation (OD-A1 option b) and per-user data-store isolation
  (option c).
- Per-user / per-graph key separation for persistent memory at rest. This protects
  stored ciphertext against storage and backup compromise; it does **not** protect
  against a compromised live process that is legitimately using the plaintext.
- A periodic or on-demand rebuild of the memory vector index, so deleted facts'
  embedding vectors do not persist in Chroma's HNSW file (BR-T2 §3c row 28).
- A separate model-service process holding no auth DB, SecretStore, or vector store.
  Today the model adapter runs in the gateway process and is handed only the
  authorized, hydrated context for the task.
