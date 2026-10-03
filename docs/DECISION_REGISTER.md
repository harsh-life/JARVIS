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
| OD-SCH-3 | Scheduled execution | **Resolved by reference** (OD-AF-2, §2K, 2026-10-02): unattended execution exists only as docs/29 §15's StandingDelegation, run by the Agent Factory's own trigger loop. The scheduler is unchanged and still cannot import the runtime or the factory. | `pyproject.toml` contracts |
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
| H-1 | **PRD #32 (per-principal fairness under ~10-device load) is not met on SQLite.** A task's request holds the single write lock for the whole task, so while one user's task runs, another user's write is refused `503 storage` (explicit, retryable, nothing persisted). Already documented as a deployment limit (`RUNNING_RUNTIME.md` §4a); Phase H measured it at pilot size (`tests/memory/test_pilot_concurrency.py`). | Unchanged — the documented limitation. Per-principal concurrency caps hold; isolation under concurrency holds. | ~~OPEN — owner/engineering~~ → **OWNER DECISION: PostgreSQL** (2026-09-28), implemented with shorter runtime transactions: §2I |
| H-2 | Push registration token stored in plaintext (BR-T2 §3d row 36, at rest). Usable only with the server's separate FCM credential, and only for the content-free wake. | Plaintext column; cleared on revocation. | **OPEN — owner** (low severity): accept for the pilot, or encrypt the column |
| H-3 | A symlink swapped in at a sandbox leaf between the last check and the open escaped as a raw `OSError` (task failed as an internal error) instead of a typed `FORBIDDEN_PATH`. Containment itself held (`O_NOFOLLOW`). | **Fixed** (`server/fs/sandbox.py::_open_leaf`), race tests added. | IMPLEMENTED (defect fix, not a decision) |

---

## 2G. Stage 5 — Judge and operator console (`[PROPOSED]`, pending ratification)

The Stage 5 build (`docs/19_JUDGE_EVALUATION.md`, `docs/28_DASHBOARD_OPERATOR_CONSOLE.md`;
operator guides `docs/RUNNING_EVALUATION.md`, `docs/RUNNING_CONSOLE.md`) keeps
19 §0 — *the Judge observes and scores; it never authorizes, never executes, and
never kills on its own authority* — and 28 §0 — *the dashboard shows; controls
live elsewhere*. Each row is the value implemented where a document leaves a
choice. **None of the `[OPEN — OWNER]` items below is ratified by being
implemented**: each was built behind configuration, off by default or in its
more restrictive reading, so the owner's decision changes a setting or a
document, not the architecture.

| ID | Decision | Value implemented | Where |
|---|---|---|---|
| OD-JDG-1 | EvaluationProvider separate from DecisionProvider (19 §2) | **`[OPEN — OWNER]`.** Built as the recommended separate `EvaluationProvider` Protocol; no DecisionProvider exists (OD-DP-9 unchanged). | `server/evaluation/provider.py` |
| OD-JDG-2 | Judge budget scope (own / per-user / global) | **`[OPEN — OWNER]`.** Judge spend is always checked against its own `evaluation.budget` (daily, default `0.0` → a paid Judge call is refused) and is **never** charged to the evaluated task or to the user's own budget or rate limits (19 §8). Whether it also counts toward the global daily budget is `evaluation.budget_scope`, default `own_and_global` (the more restrictive reading, as recommended). | `server/evaluation/service.py`, `server/security/usage.py` |
| OD-JDG-3 | Pilot Judge provider | **`[OPEN — OWNER]`.** None chosen: `evaluation.enabled: false` and `provider: null` by default; enabling the LLM Judge requires naming a provider (a non-local one logs that redacted traces are sent to it, 19 §4). `evaluator: rules` needs no model. | `server/config/schema.py` |
| OD-JDG-4 | `usage.kind: evaluation_call` vs `model_call` + attribution | **`[OPEN — OWNER]`.** `01` §1.2 is locked, so no enum was added: Judge calls are `model_call` rows attributed by `tool_id = "evaluator:<id>"`. Extending the enum later is a registry edit plus a data migration of those rows. | `shared/schemas/evaluation.py` |
| OD-DASH-1 | Read-only dashboard + separate control endpoints vs amending DASH-002 | **`[OPEN — OWNER]`.** Built as the recommended split: `GET /api/v1/admin/*` views (read-only, asserted at import and by an import contract) and `/api/v1/admin/control/*` owned by 18 (stop/latch), 20 (break-glass) and 19 (Judge switches, review queue). DASH-002 is not amended. | `server/gateway/routers/admin.py`, `server/dashboard/` |
| OD-DASH-2 | Console UI technology | `[IMPL]`, **not built**: the console is the JSON API only. A browser page would need either a public shell route (`02` §12: `/health` is the only data-free public endpoint besides OIDC start/callback) or an in-page superuser credential — an owner call. | — |
| JDG-B1 | Where the Judge's trace comes from | The runtime mirrors the events it already audits (plus each authorization's decision and tier, and each metered call's units and cost) in the task's volatile state, and hands a copy to an observer port. A live window must be evaluated while the task's own request still holds its uncommitted audit rows, so the trace cannot be read back from the store. The trace is never persisted. | `server/agent/runtime.py`, `server/agent/ports.py` |
| JDG-B2 | Stop-request gating | Only `live_window` evaluations may request a stop, only with `may_request_stop: true` (without it the Judge holds no breaker handle at all), and only through `trip()` with source `evaluator`. A paused task that is tripped is enforced by the runtime's own emergency-stop sequence. Post-hoc stop requests are recorded, never acted on. | `server/composition/evaluation.py` |
| JDG-B3 | Stop alert | More than `stop_alert_threshold` honoured evaluator stops in `stop_alert_window_minutes` → one `evaluation.stop_alert` audit row per window and a warning log; the operator may switch the Judge off (`/admin/control/evaluation/switches`). | `server/evaluation/service.py` |
| JDG-B4 | Operator switches | Runtime on/off for the Judge and for its stop requests, persisted (`evaluation_control`), each **capped by configuration**: "on" beyond `evaluation.enabled` / `may_request_stop` is refused `409`. | `server/composition/improvements.py` |
| JDG-B5 | Improvement targets and application | The closed registry of 19 §9 (worker system-prompt guidance, tool descriptions, recovery thresholds within the config schema's ranges, Judge rubric, suggestion template). Approval re-validates the value; `config_versions` is append-only; rollback undoes a target's *current* version. Applied values reach the worker as delimited "operator guidance (it grants nothing)" text and as recovery bounds (only where `agent.recovery` is configured), and the Judge as its rubric. `suggestion.template` is versioned but has no consumer yet. | `server/evaluation/candidates.py`, `server/composition/improvements.py` |
| DSH-B1 | Redaction | User content (task responses, evaluator notes, candidate text, applied values) appears as `{"redacted": true, "chars": n}`; secrets appear only as handles with `resolves` decided from metadata (a non-revoked `secret_references` row, an env var's presence); every other string is scrubbed with the repository's secret patterns. The DASH-006 view (`GET /admin/privileged/tasks/{id}?reason=`) is audited **before** it reads, and still scrubs secret-shaped text. | `server/dashboard/redaction.py` |
| DSH-B2 | Memory counts | Facts per user are counted through the provider's owner-filtered listing inside the composition root; no content reaches the dashboard. Capped at 10 000 per user. | `server/composition/console.py` |

**Recorded residuals (not fixed here, not claimed fixed):**

- Evaluations and candidates store model-written notes about a user's task in
  the application database, owner-private, in the same class as `agent_tasks.response`
  (BR-T2 §3: reachable under application RCE — the OD-A1 (a) residual).
- The gateway's error handler reports a routing `405` as `500 internal_error`
  ("Method Not Allowed"). Pre-existing; a non-GET request to a console view
  still reaches no handler.
- The real-data gate (17 §5) is unchanged by Stage 5: the Judge and the console
  add no release-blocking suite, and the gate stays closed.

---

## 2H. Real-data gate run — findings (2026-09-28)

The real-data gate run validated `main` @ `8b15e39` (Phase H + Stage 5). It
added no feature and closed no decision. The evidence and the gate decision are
in `docs/RELEASE_VALIDATION.md`. Findings that need a decision:

| ID | Finding | What the code does now | Status |
|---|---|---|---|
| OD-JDG-5 | **One user's content can reach every user through an approved Judge candidate** (BR-T2 §3e row 38, *authorized* path). A candidate is written from one user's trace. An approved `worker.system_prompt` applies to every user's worker prompt. Approval screens for secret-shaped text only, and the approver's default console view shows the candidate redacted. | Judge off by default. Superuser-only approval, audited, can be rolled back. Secret-shaped values refused. | **OPEN — OWNER**. Mandatory before the Judge runs with real data. Options include: require the audited unredacted view (DASH-006) before an approval; scope approved guidance to the source user; or keep `evaluation.enabled: false` for real data. Not decided here. |
| H-1 (update) | PRD #32 now **measured** (`tests/memory/test_prd32_service_measurement.py`). Ten users submitting at once are all served but fully serialized: 11.6 s wall clock for ten 0.5 s tasks, median task latency 8.7 s, 15 retryable `503`s across 9 of 10 users. One task whose model call passes the 5 s busy timeout refuses **9/9** other users' writes `503 storage`. Reads are unaffected. | Unchanged. | ~~OPEN — owner/engineering~~ → **OWNER DECISION: PostgreSQL** (2026-09-28): §2I. The baseline figures in this row are the "before" of `docs/RELEASE_VALIDATION.md` §I. |
| H-2 (scope) | The push-token at-rest row (BR-T2 36) exists only when push is enabled. With the default `android.push.provider: none` the server refuses to store a token (`409`; `test_push_is_off_by_default_and_nothing_can_be_registered`). | As §2F. | **OPEN — owner**: accept or encrypt before enabling FCM with real data. It does not arise in a push-off deployment. |
| OD-MB-4 (scope) | Memory at-rest rows (BR-T2 27–28) exist only when `memory.enabled: true` (default false). | As §2C. | **OPEN — owner**. Mandatory before memory runs with real data. |

## 2I. H-1 — the runtime relational store (OWNER DECISION, 2026-09-28)

**Status: OWNER DECISION — PostgreSQL.** Decided by the owner: *"Move the
runtime relational store to PostgreSQL to remove the SQLite single-writer
bottleneck, then re-run the PRD #32 ~10-device fairness acceptance test."*
This closes H-1 as recorded in §2F and §2H. It closes nothing else: every other
row of §2F, §2H and §3 is unchanged.

| | |
|---|---|
| **Rationale** | PRD #32 requires per-principal fairness under ~10-device load. SQLite has one writer, and §2H measured the consequence: one user's task refused 9/9 other users' writes `503 storage`. STORE-004 leaves the database `[IMPL]`; the owner chose PostgreSQL. |
| **What changed** | (1) PostgreSQL is a supported store: `postgresql+asyncpg`, through the same SQLAlchemy models and Alembic migrations. `database_url` accepts that driver and `sqlite+aiosqlite` only, and refuses a URL carrying a password (SECRET-004: `PGPASSWORD` / `~/.pgpass`). (2) One migration, `a2d6e8f4c0b9`: `audit_events`' user/device/session/graph columns become plain identifiers, since audit rows deliberately name ids that do not (or no longer) exist, which PostgreSQL's enforced foreign keys refused. (3) **Transaction scope.** A PostgreSQL store alone does not remove the bottleneck (a task's request held its transaction — and a pooled connection — across every model call). The runtime now commits what a task has written before each model call, tool run and memory search, and the memory write path commits before the provider's write. (4) Concurrency guards that short transactions need: a same-key retry while the original runs is refused `409` (never run twice), and usage admission counts calls admitted but not yet committed to the ledger (two users cannot both pass a budget with room for one). (5) A transient store conflict (deadlock, serialization failure, lock timeout, connection pressure, SQLite busy) is a retryable `503`. |
| **What did not change** | Authorization, identity, capabilities, confirmation, memory isolation, Judge authority and Android authority: no code in `server/graph`, `server/capabilities`, `server/auth`, `server/secrets`, `server/evaluation` or `android/` changed for H-1 beyond two explicit `flush()` calls that order inserts for PostgreSQL's foreign keys. 02 §1.2 holds: every request-level refusal happens before the first commit point and still commits its audit and nothing else. |
| **Consequence** | A task's committed prefix (its row, authorization decisions, audit, metered spend) survives an unexpected failure instead of being rolled back; the restart reconciliation closes a row left `running`. A spent confirmation token is durable before the approved action runs, so a retry cannot run it twice. The idempotency and admission guards are in-process: the deployment stays **one server process** (`RUNNING_RUNTIME.md` §4a). SQLite stays for development and tests and does not meet #32. JDG-B1's premise (a live window's audit rows are uncommitted) no longer always holds; its design — the trace comes from volatile state, never the store — is unchanged. |
| **Validation method** | Test first: `tests/memory/test_prd32_service_measurement.py` (the #32 acceptance, S1/S2) failed on SQLite and refuses to run on anything but PostgreSQL; `tests/runtime/test_concurrent_store.py` (no open transaction during model/tool calls, same-key retry, budget race, ten users' rows, deadlock → `503`) failed for the intended reasons before the fix. Then every suite on a real PostgreSQL server (and still on SQLite), the migration round-trip on both, the import contracts, BR-T2 and the guard mutations (M42–M44 added for the new guards). CI's `postgres` job runs all of it on every push. Measurements: `docs/RELEASE_VALIDATION.md` §I. |
| **Acceptance status** | **PRD #32: MET on PostgreSQL** (`docs/RELEASE_VALIDATION.md` §I). The Real-Data Gate is **not** opened by this: its other blockers stand (§P). |

## 2J. Agent Factory — Phases 1 to 5 (`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]`, except the OD-AF rows ratified in §2K)

`docs/29_AGENT_FACTORY_ASSESSMENT.md` is a proposal. Its Phase 1 boundary
(docs/29 §31) is built **behind `agents.enabled: false`** as implemented
recommendations: with the flag off no agent tool is registered, the worker's
prompt is unchanged, the `/api/v1/agents` endpoints answer `503` with
`dependency: agents`, and nothing writes to the three new tables. **No OD-AF
decision is ratified by being built**; each row below says what the build does
while the decision is open. Phase 2 (native, present-user, on-demand runs) is
recorded after the Phase 1 tables; Phase 5 (unattended runs, under §2K) after Phases 3–4.

| ID | Decision | Value implemented (not ratified) | Where |
|---|---|---|---|
| OD-AF-1 | Ratify the factory abstractions | **`[OPEN — OWNER]`.** Built as docs/29 describes: templates, model/runtime profile registries, a pure selector and compiler (the only writer of `CompiledAgentSpec`), owner-private definitions with immutable hashed spec versions, and the `AgentRuntimeProvider` Protocol (declared, no provider wired). No separate AgentStateProvider. | `server/agents/`, `shared/schemas/agent_factory.py` |
| OD-AF-2 | PRD §22 amendment for unattended runs | **RATIFIED 2026-10-02 (§2K).** Before that: Not built. The compiler rejects `trigger.kind: unattended` (`unattended_unavailable`) and the config refuses `agents.unattended_enabled: true` even with `standing_delegation_ratified: true`. The scheduler is unchanged and cannot import the factory (AF-C4). | `server/agents/compiler.py`, `server/config/schema.py` |
| OD-AF-3 | The `agent.*` tier table | **RATIFIED 2026-10-02 (§2K).** Before that: Registered as proposed: `agent.define` {`compile`, `compile_update` → low_read; `create`, `update` → consequential}, `agent.inspect` {`list`, `get` → low_read}, `agent.delete` {`delete` → consequential}; `agentdefinition` create/write are consequential in the resource axis too, so the HTTP path confirms as well. `agent.run`/`agent.control`/`agent.delegate` and `agent.inspect` {`runs`, `inbox`} are **still not registered** after Phase 2 (AF-P2-1). | `server/capabilities/registry.py`, `server/capabilities/risk.py` |
| OD-AF-4 | Consequential actions in unattended runs | **RATIFIED 2026-10-02 (§2K).** Before that: Moot until Phase 5; no unattended run exists. | — |
| OD-AF-5 | Default delegation lifetime | **RATIFIED 2026-10-02 (§2K).** Before that: Only the config field exists (`agents.delegation_max_days`, 30); nothing reads it. | `server/config/schema.py` |
| OD-AF-6 | First external provider | **RATIFIED 2026-10-02 (§2L): `browser_use`, P2.** Before its infrastructure exists (OD-AF-11…15, open) it stays a reserved id with no profile: enabling it fails startup. | `server/agents/registry/runtimes.py` |
| OD-AF-7 | Per-user quota and default budgets | **RATIFIED 2026-10-02 (§2K).** Before that: `agents.max_agents_per_user: 5`; `default_budget_per_run`/`per_month: 0.0`, so only local models can be selected until an operator raises them (`no_model:budget` otherwise). | `server/config/schema.py`, `server/agents/selector.py` |
| OD-AF-8 | Outputs beyond the owner's inbox | **RATIFIED 2026-10-02 (§2K).** Before that: Inbox only (`OutputKind` has one value). Phase 2 builds the inbox (AF-P2-6); there is no recipient field and no other output. | `shared/schemas/agent_factory.py`, `server/agents/service.py` |
| OD-AF-9 | Attribution: join table vs extending `UsageEvent` | **`[OPEN — OWNER]`.** Phase 2 builds the join table, as docs/29 recommends: `agent_run_usage` (run_id, usage_id). `usage_events` is unchanged; the ledger's `record` now returns the row's `usage_id`. | `server/storage/models.py`, `server/security/usage.py` |
| OD-AF-10 | `agentdefinition` in `ResourceType`; new `01` entities | **`[OPEN — OWNER]`.** Added as proposed: `ResourceType.AGENTDEFINITION` (owner-private, decided by the one engine; a deleted agent is `not_found`) and migration `e1f3a5c7b9d2` (`agent_definitions`, `agent_spec_versions`, `agent_compile_previews`). Phase 2 adds `agent_runs` (`f2a4c6e8b0d1`), `agent_notebook_entries` (`a3c5e7f9b1d4`), `agent_inbox_items` (`b5d7f9a1c3e6`) and `agent_run_usage` (`c7e9b1d3f5a8`), and the `01` failure codes `agent_unavailable`, `spec_changed`, `agent_budget_exhausted`. All additive, round-tripped on SQLite and PostgreSQL. | `shared/schemas/authorization.py`, `server/storage/` |

Build choices where docs/29 leaves a gap (`[IMPL]`, each flagged for review):

| ID | Choice | Where |
|---|---|---|
| AF-B1 | **No `awaiting_confirmation` row.** A compile writes an owner- and task-bound, single-use, 15-minute preview; the definition and its first spec version are written together when the approved preview is consumed. A rejected or expired approval leaves nothing to clean up. | `server/agents/service.py` |
| AF-B2 | **`compile_update`.** Compiling a new version names the agent (`resource_ref`), so the engine checks ownership before anything is compiled; docs/29 lists only `compile`. `GET /agents/previews/{compile_id}` renders an owner's pending card for a task paused on `create`/`update`. | `server/composition/agents.py`, `server/gateway/routers/agents.py` |
| AF-B3 | **Roles as model features.** `ModelFeature` adds `writing`, `image_generation`, `document_structured`, `speech`, and the draft adds `preferred_model_features` (ordering only). Both are routing metadata; neither can make a profile eligible or widen an envelope. | `shared/schemas/agent_factory.py`, `server/agents/selector.py` |
| AF-B4 | **Model profiles reference existing entries only** — `agent.primary`, `agent.fallback`, `models_as_tools.<id>` — and carry no provider, endpoint or key; credentials stay on the entry and resolve through the existing SecretStore path. No separate connector or credential store was built (none exists in the repository). | `server/agents/registry/models.py`, `server/composition/agents.py` |
| AF-B5 | **Owner model policy.** A profile is usable when open to all or when its `model_ref` is the owner's resolved primary; an owner with their own (or their graph's) `AgentConfiguration` resolves to no profile reference, so only open profiles apply. | `server/composition/agents.py` |
| AF-B6 | **Cost projection and class.** Per run: `max_model_calls × context_window/1000 × (input + output price per 1k)`; class thresholds `low ≤ 0.002`, `medium ≤ 0.02` per 1k combined. docs/29 derives both from pricing without fixing the formula. | `server/agents/registry/models.py` |
| AF-B7 | **Never guessed.** File abilities need a named sandbox label (`sandbox_needed`); a reminder needs an exact cron and IANA zone; a URL source outside `execution.network.default_destinations` is `source_not_allowlisted`. Each is a clarification, never a default. | `server/agents/compiler.py` |
| AF-B8 | **Interfaces declared in Phase 1, wired in Phase 2.** The envelope gate (`server/agent/envelope.py`), the provider Protocol and model-as-tool routing were declared and tested in Phase 1; Phase 2 wires all three for native runs (below). | `server/agent/envelope.py`, `server/agents/providers/`, `server/agents/gateway/` |

Contracts AF-C1…AF-C6 and a purity contract for the envelope gate are in
`pyproject.toml`; mutants M-AG1–M-AG5, M-AG13, M-AG14 are in
`tests/tools/guard_mutations.py` (all killed).

### Phase 2 — native, present-user agent runs (implementation facts)

Still behind `agents.enabled: false`, and still a proposal. A run is an
**ordinary task of the authenticated owner** in the spec's mode, through the
existing runtime, engine and confirmation path. There is **no background,
scheduled or unattended path**: nothing starts a run except the owner's own
request, and the scheduler is unchanged. Nothing external is built: no Letta,
OpenClaw, Browser Use, OpenHands, LangGraph/ADK/Pydantic AI, MCP transport or
runtime container; no StandingDelegation or DelegatedPrincipal; no child agents,
agent-to-agent messages, external recipients, Darwin or self-improvement.

**What a run is.** `POST /api/v1/agents/{id}/runs` takes an empty body. The
owner, graph, version, hash, runtime and input come from the session and the
stored, hash-verified spec. The run's per-action order is: proposal → envelope
gate → activation → the one engine (04) → confirmation → execution. Its
effective authority is the intersection of the owner's live grants, the
template maximum, the compiled envelope, the graph scope, the mode and risk
ceilings and the global floor. The envelope only removes; nothing in a spec,
prompt, model output, runtime or tool result grants anything.

| ID | Fact (`[IMPL]` unless stated) | Where |
|---|---|---|
| AF-P2-1 | **Run control is HTTP only.** `run`, `pause`, `resume`, `cancel`, inbox and export are owner endpoints authorized by the engine on the `agentdefinition`. No `agent.run`/`agent.control` capability or worker tool was registered: a run started from inside a task would nest one synchronous run inside another's tool call (bounded by the tool timeout), and a detached start would be the background path this phase excludes. Deviation from docs/29 §23.1, flagged under OD-AF-3. | `server/composition/agents.py`, `server/gateway/routers/agents.py` |
| AF-P2-2 | **Runs, pause, resume and stop are authorized as follows.** Run, pause, cancel, notebook, inbox and export are `read` on the owner's own definition. Resume is `write`, which is consequential, so the owner confirms it with a token bound to the current spec hash. Pause and cancel (the safe direction) are never confirmed. | `server/composition/agents.py` |
| AF-P2-3 | **Re-validated, fresh, at every step.** The check runs at the top of each loop iteration, at every tool call and capability request (after the model proposed it, before anything is decided), before an approval and on a platform resume. It requires: the run record still open; the definition existing, active, the owner's, and exactly the run's version and hash; and the owner still a live member of the agent's graph. Failing it stops the run with `agent_unavailable` or `spec_changed`. Nothing is cached; the engine re-decides every call, so a revoked grant stops the next one. | `server/agent/runtime.py`, `server/agents/service.py` |
| AF-P2-4 | **Start-time checks.** A run is refused (409, audited) when the agent is revoked or tampered, not active, in another graph than the session's, on an older template version (the agent is marked `needs_reapproval`), or on a model profile that is no longer configured, enabled, current or permitted to its owner. The task runs on the profile's configured entry, through the same key path as any worker. Its bounds and budget are the tighter of the server's and the spec's. | `server/composition/agents.py`, `server/composition/models.py` |
| AF-P2-5 | **Model as a tool.** The runtime serves `agent.model` (role, preference, prompt) only when the envelope holds `model.invoke`. A strict request schema refuses any provider, model, profile, endpoint or key. `route_model_call` picks from the spec, the operator's model-tool profiles and the owner's current model policy, within the run budget. The route becomes an ordinary `model.invoke` call that passes the gate, activation, engine, budget precheck and metering. A model tool cannot be named directly in an agent run. **No shipped v1 template holds `invoke_model_tool`**, so this is reachable only through an operator template. | `server/agent/runtime.py`, `server/composition/agents.py`, `server/agents/gateway/model_routing.py` |
| AF-P2-6 | **Inbox.** One item per finished run, to its owner only, with no recipient field. The status comes from the run record. The body is plain text: control characters and escapes are dropped, it is capped at 16 000 characters, and it is never parsed. A credential-shaped result is withheld, not stored. `GET /agents/inbox`, `POST …/{item}/read`, `DELETE …/{item}`; another user's item is 404. | `server/composition/agents.py`, `server/agents/service.py` |
| AF-P2-7 | **Notebook.** `agent.notebook` (get/put/list), only when the spec's `notebook_enabled`. That single flag covers read and write; there is no spec schema change. Bound to the run's own agent. At most 200 notes; the key is a slug of at most 120 characters; the value is at most 8 000 printable characters. Writes pass the memory write gates (`find_secret`, the emotion lexicon). The owner reads and clears it. Nothing in it reaches Mem0. | `server/agent/runtime.py`, `server/agents/service.py`, `server/composition/agents.py` |
| AF-P2-8 | **Memory.** An agent run's output is never extracted into the owner's memory, whatever `memory.auto_extract` says. This is proved on the real Mem0 stack with an execute-mode agent. Ordinary tasks are unchanged. | `server/agent/runtime.py`, `tests/memory/test_agent_memory_exclusion.py` |
| AF-P2-9 | **Monthly budget.** The month is the UTC calendar month; spending is the sum of the agent's runs' `cost_total`. A spent month refuses the run (429, `agent_budget_exhausted`). A run's ceiling is `min(per_run, per_month − spent)`; a zero monthly budget leaves only free (local) calls. | `server/composition/agents.py`, `server/agents/service.py` |
| AF-P2-10 | **Deletion and export.** Delete stops live runs (their paused actions can never be confirmed), deprovisions through the native provider (notebook, inbox) and closes every open run record; the service does the same on the factory worker's path. Run records stay as history. Export returns the agent view, every hash-verified spec version, runs, notebook and inbox, and nothing else: no `secret_ref` or key, no session, device or confirmation token, no grant, decision or audit row. | `server/composition/agents.py`, `server/agents/service.py` |
| AF-P2-11 | **Pre-existing, not changed here.** On PostgreSQL, a worker answer containing a NUL character fails the task's own `agent_tasks.response` write. This affects every task, not only agent runs. | `server/agent/records.py` |

Phase 2 mutants M-AG15–M-AG69 are in `tests/tools/guard_mutations.py`.
M-AG61 needs `HYPERMIND_REQUIRE_MEMORY_STACK=1`.

### Phases 3 and 4 — Agent Gateway and reminder tap (implementation facts)

Engineering phases 3 and 4 are implemented as one milestone ("Agent Gateway +
Reminder-Tap Integration"), while retaining separate acceptance criteria;
docs/29's phase labels are unchanged. Still behind `agents.enabled: false`,
still a proposal, and still **present-user only**: on demand, or tapped from
a reminder by the owner. Nothing runs unattended, on a schedule, or for anyone
but the authenticated owner. The pipeline is unchanged — agent proposal →
envelope gate → the one engine (activation, 04) → confirmation/step-up →
execution — and nothing added here is an authority: the gateway, the Judge,
the scheduler, the console and the Android client each only remove, observe,
carry data or ask. No StandingDelegation, DelegatedPrincipal, external
recipient, external runtime or framework, MCP transport, HTTP gateway, Darwin,
child agent or agent-to-agent messaging is built.

| ID | Fact (`[IMPL]` unless stated) | Where |
|---|---|---|
| AF-P3-1 | **Run tokens.** Two per run (model, tool): 32 random bytes, shown once in the run's context and stored only as SHA-256 (`agent_run_tokens`, migration `d4f6a8c0e2b5`). Each is bound to one run, agent, spec hash and gateway; it expires 30 s after the run's deadline and is revoked when the run finishes or is cancelled (before the runtime is told), and when the agent is paused (by owner or operator), changed or deleted. No session credential, provider key, SecretStore value or user credential is ever in one. | `server/agents/gateway/`, `server/agents/service.py` |
| AF-P3-2 | **Every request of a run is a gateway request**, checked in order: token form, known, purpose, binding (run, agent, spec hash), run open, the Phase 2 definition check (fresh), expiry, revocation, `sent_at` within ±60 s, nonce form, then the nonce ledger (`agent_gateway_nonces`): the same nonce and request answer with the stored, secret-scrubbed response and run nothing again; the same nonce for another request is a replay. In-process for native runs — the same check a future external runtime would meet. | `server/agents/gateway/core.py`, `server/agent/runtime.py` |
| AF-P3-3 | **Model Gateway.** A model call names an alias, never a model: `agent-model` (exactly the approved profile and version) or `model-tool:<id>` (needs `model.invoke` in the envelope, pinned if pinned). The profile must still be enabled, support the runtime, be permitted to the owner now (open to all or the owner's primary, read live) and be exactly the configured provider/model. Provider credentials stay on the entry and resolve server-side; none reaches a prompt, context, token, trace, log, notebook, inbox or client payload. | `server/agents/gateway/model_gateway.py`, `server/composition/agents.py` |
| AF-P3-4 | **The month, live.** After the per-run and owner (13) prechecks, each paid call adds the agent's live runs' attributed usage to its finished runs' spend; a call that would pass `budget.per_month` is refused (`agent_budget_exhausted`). Free calls are never refused by it. | `server/agents/service.py`, `server/agent/runtime.py` |
| AF-P3-5 | **The Judge, attributed and owner-scoped.** The Judge's record names the run's agent, run and version. `agent.purpose` is the one owner-scoped candidate target (registry: 19 §9's list plus it); it is accepted only from an agent run, stored with the agent from the trace (`improvement_candidates.agent_id`, migration `e6b8d0f2a4c7`), shown only to that agent's owner, and applied only by the owner's own confirmed update — a recompile that would change anything but the purpose is refused. A superuser approval of it is refused (`owner_scoped`). Every other `agent.*` target is forbidden (`agent_authority`). The Judge's stop request stops a run and changes nothing else. | `server/evaluation/`, `server/agents/revision.py`, `server/composition/` |
| AF-P3-6 | **Console and operator pause.** `GET /api/v1/admin/agents` is read-only and redacted (names as lengths; no purpose, source, result, notebook, inbox or token). `POST /api/v1/admin/control/agents/{id}/pause\|release` is the existing operator stop aimed at one agent: live runs tripped in memory first, the agent paused on the operator's authority, its tokens revoked; its owner can neither resume nor re-approve past the hold, and release only lifts it. | `server/dashboard/console.py`, `server/composition/supervisor.py` |
| AF-P4-1 | **The reminder is data.** `scheduled_jobs.agent_id` (migration `f8c0e2a4b6d9`; docs/29 §28 M5's `01` field) is stored by the scheduler and copied onto the frame; the scheduler interprets, authorizes and starts nothing, and still imports none of the runtime, tools, devices, secrets, memory, the Judge or the factory. No request can set it. | `server/scheduler/`, `server/storage/models.py` |
| AF-P4-2 | **The job.** A `reminder` trigger is the owner's private job, "Run agent: {name}", on the compiled `CRON_TZ` schedule, created by the composition root through the scheduler's service and the one engine. Every check that could refuse it runs before the agent's change is written (a refusal still commits its audit trail), so no agent is half-made. Pause (owner or operator), delete and re-approval cancel it; the owner's confirmed resume and an update re-create it. | `server/composition/agents.py` |
| AF-P4-3 | **`agent_reminders`.** Only a client that declared the channel feature receives `agent_id`; every other client gets the byte-identical plain frame. A firing delivers a message and nothing else. | `shared/schemas/device_channel.py`, `server/execution/device_hub.py` |
| AF-P4-4 | **The tap.** `POST /agents/{id}/runs {reminder_delivery_id}` is the owner's ordinary run, labelled `reminder_tap` (migration `b2d4f6a8c0e1`). The delivery is accepted only if it was sent to this very device of this very owner for this very agent (else `404`); the run then passes every on-demand check. One tap is one run: a repeated or racing tap gets the run it started (unique index). Identity always comes from the session, never from a reminder payload. | `server/composition/agents.py` |
| AF-P4-5 | **Android.** An endpoint, not an authority: it lists the owner's agents and sends the owner's own Run; a reminder's "Run agent" only opens the app on an offer built from the phone's own record; a run is shown, confirmed and tracked as the ordinary task it is; a server refusal is shown as one. No provider credential, token or policy is on the device. | `android/` |

Phase 3/4 mutants M-AG6–M-AG8 and M-AG70–M-AG122 are in
`tests/tools/guard_mutations.py` (four of them Kotlin).

### Phase 5 — standing delegation and unattended runs (implementation facts)

Built under the decisions of §2K (OD-AF-2/3/4/5/7/8, ratified 2026-10-02),
**off by default**: `agents.unattended_enabled: false`, and it loads as true
only with `agents.enabled` and `agents.standing_delegation_ratified`. Nothing
external is built (Phase 6: no external runtime, MCP, container or netns).
No child agents, agent-to-agent delegation, external recipients, device
execution by agents, Darwin or Mem0 extraction.

| ID | Fact (`[IMPL]` unless stated) | Where |
|---|---|---|
| AF-P5-1 | **DelegatedPrincipal.** `(user_id, agent_id, delegation_id, run_id, graph_id?)`, frozen, `extra="forbid"`, with **no `device_id`/`session_id` attribute at all** (reading one raises). Every consumer reads a device or session through `device_of()`/`session_of()`, which return `None` for it. Built only by the trigger loop, from the stored delegation and the run it just opened. | `shared/schemas/authorization.py` |
| AF-P5-2 | **The engine** decides it on D1–D5 as its owner. A tier above `low_write`, or one needing confirmation, is `DENY unattended_never_confirms` — no binding, no token. D5: device- and session-scoped grants never match; only the owner's `user`/`graph` grants are candidates. Audited with no device and no session. | `server/graph/authorization.py`, `server/capabilities/grants.py` |
| AF-P5-3 | **StandingDelegation** (`standing_delegations`, migration `d1f3b5a7c9e2`): bound to the spec version, spec hash and a recomputed envelope hash, the exact cron and zone, `max_runs_per_day` 1–24, budgets > 0 and ≤ the spec's, mandatory `expires_at ≤ created_at + delegation_max_days`, `created_with_step_up` true; at most one active per agent. The store enforces each. A task row is either a present user's (device + session) or a delegation's (neither). | `server/storage/models.py`, `server/agents/delegation.py` |
| AF-P5-4 | **Grant** (`POST /api/v1/agents/{id}/delegation`): the owner's own call only, never a tool. The agent must be active, in the session's graph, on its current template/profile/runtime, compiled `unattended` within the ceiling. The engine decides `write` on the definition (consequential): a confirmation bound to the exact terms and spec hash. The confirmed call needs a **fresh device re-attestation**, checked before the token is spent. A new grant supersedes the active one (`renewed`). **Revoke** (`DELETE …`) is always allowed, never confirmed, and stops live runs under it. | `server/composition/agents.py`, `server/gateway/routers/agents.py` |
| AF-P5-5 | **Freshness**, read from the store at every check: the run open and exactly this delegation's; the owner `active`; still a member of the graph; the delegation active, unexpired, step-up-granted and exactly the agent's current spec (version, hash, envelope hash, schedule) and within the ceiling. Checked by the security port before every step (`principal_active`) and by the run's coordinator at every step and tool call (a second, independent check). With unattended runs switched off, no delegated principal is ever fresh. | `server/composition/agents.py`, `server/composition/security_port.py`, `server/agents/service.py` |
| AF-P5-6 | **Ending.** Owner pause, operator pause, delete, a new version, re-approval and a tampered spec end the delegation (revoked or invalidated). Resume never restores it. Every end — by any path — is audited (`agent.delegation.revoked/expired/invalidated`, the reason code) and the owner gets a notice (except on delete, whose inbox goes with the agent). | `server/agents/service.py` (`delegation_ended` hook), `server/composition/agents.py` |
| AF-P5-7 | **The trigger loop** is the Agent Factory's own (`AgentTriggerLoop`, a lifespan service only when unattended runs are on), never the scheduler (which still imports no runtime, tool or factory). Per active delegation, in its own transaction: expiry (notice 3 days before, once; `expired` at expiry), freshness (`invalidated` otherwise), then the latest occurrence since the last claimed one. | `server/composition/agent_triggers.py`, `server/agents/triggers.py` |
| AF-P5-8 | **Misfires and admission.** Of the occurrences since the last claim, only the latest runs, and only within `misfire_grace_minutes` (15); earlier ones are one `misfire_coalesced` notice, or `run_missed` past the grace. The claim is a compare-and-set on `last_occurrence_at`; `(delegation_id, occurrence_at)` is unique, so concurrent passes run an occurrence once. Skipped with a notice: the global latch (`breaker_stopped`), the day's limit in the delegation's zone (`run_limit_reached`), the month's spend ≥ `min(spec, delegation)` (`budget_exhausted`). | `server/composition/agent_triggers.py` |
| AF-P5-9 | **The run** is an ordinary task of the delegated principal through the native provider, the Agent Gateway (tokens, nonces), the envelope gate and the engine. In addition, before the engine: a capability that is device-, app-, break-glass- or `agent.*`-shaped is never activated; an unattended run activates **only** the owner's existing standing grants and never asks; every tool call must pass `unattended_refusal` (≤ `low_write`, server only, `net.request` `get` only). It never pauses (a pause fails it closed); its per-run budget is `min(delegation, spec, month remaining)`, its month `min(spec, delegation)`. Results and notices go to the owner's inbox only, as data. | `server/agent/runtime.py`, `server/agent/envelope.py`, `shared/schemas/agent_factory.py` |
| AF-P5-10 | **Restart.** At the loop's start, an unattended run that never got its task is closed `failed: interrupted` with its tokens revoked; one whose task ended is closed from it. Nothing is replayed: its occurrence stays claimed. | `server/composition/agent_triggers.py` |
| AF-P5-11 | **No `CapabilityScopeType.agent`** (§2K deviation): no activation record is ever scoped to an agent or delegation. **`agent.delegate` is not a registered capability**: as AF-P2-1 did for run/control, it is an owner HTTP path, so no task can grant standing authority. | — |

Phase 5 mutants M-AG123–M-AG164 are in `tests/tools/guard_mutations.py`
(M41, M-AG19 and M-AG118 were re-anchored onto the same guards). The BR-T2
re-run is `docs/OD_A1_BR_T2.md` §3f (rows 42–45).

### Phase 6, slice 6A — the HTTP Model Gateway (implementation facts)

Built under §2L (OD-AF-6: Browser Use, P2; OD-TOOL-3: no MCP), **off by
default** (`agents.model_gateway.enabled: false`), and it loads as true only
with `agents.enabled` and an internal `listen`. It needs none of the open
infrastructure decisions OD-AF-11…15, and it builds none of them: no
container, namespace, egress proxy, image, browser capability or Browser Use
adapter exists. Nothing calls it yet but tests; 6D's adapter will.

| ID | Fact (`[IMPL]` unless stated) | Where |
|---|---|---|
| AF-P6-1 | **A separate internal listener.** Its own FastAPI app with one route, `POST /v1/chat/completions`, and no docs, schema or other route; **never included in the public app**. Served by its own background service on `agents.model_gateway.listen`: a Unix socket created `0600` before it accepts anything (a non-socket file at that path is refused, never replaced), or a loopback `address:port`. Wildcard, private, routable and hostname bindings are refused at load: a container network's address is OD-AF-12's to decide. | `server/gateway/routers/model_gateway.py`, `server/composition/model_gateway.py`, `server/net/listen.py` (the only module besides egress that opens a raw socket), `server/config/schema.py` |
| AF-P6-2 | **One core.** The token is checked by `AgentGateway.authenticate` (the native runtime's core; model purpose only), the profile by `AgentFactory.model_screen` and the month by `AgentFactory.agent_budget` — both moved from the native coordinator unchanged, so one implementation serves native and external runs. Order: token → request shape → alias → global stop → run and task running and the owner's → profile permitted now → deadline → model-call bound → the run's budget, the owner's budget and rates, the agent's month → provider. Authentication comes first: an unauthenticated caller learns nothing else. | `server/composition/model_gateway.py`, `server/composition/agents.py` |
| AF-P6-3 | **A closed request.** `model` must be exactly `agent-model` (a `model-tool:` alias is refused here too); text messages with roles system/user/assistant only; no tools, functions, response format, images or unknown fields. `stream: true` and `n ≠ 1` are refused with `400`. Sampling hints (`temperature`, `top_p`, `max_tokens`) are accepted and **not** forwarded: the configured entry decides. A body over `max_request_bytes` (1 MiB) is refused unread (`413`). | `shared/schemas/agent_factory.py`, `server/agents/gateway/model_gateway.py` |
| AF-P6-4 | **Errors** (docs/29 §12.3): `401 invalid_run_token`, `400 schema_invalid`, `403 model_not_allowed`, `409 run_not_running` (the run not running, its task ended, the agent paused/changed/unavailable, the deadline passed, or the global stop), `429 budget_exceeded \| agent_budget_exhausted \| rate_limited \| max_model_calls`, `503 dependency_unavailable`. **`max_model_calls`** is a code docs/29 does not name: the run's model-call bound (`min(runtime, spec)`) needed one, and none of the three it lists is accurate. Messages are fixed text; no provider error, endpoint, model name, key or token ever appears in a response. Every refusal is audited (`agent.gateway.denied`). | `server/agents/gateway/model_gateway.py`, `server/composition/model_gateway.py` |
| AF-P6-5 | **Bounds without a race.** The run's model-call bound is taken by one conditional `UPDATE` on its task (`model_calls < limit`), so concurrent requests cannot overrun it. The run's budget is `min(runtime per-task budget, spec per-run, delegation per-run)` against its live attributed usage (its `cost_total` is written only when it ends). | `server/agents/service.py` |
| AF-P6-6 | **Metering.** Every provider attempt is a `UsageEvent(model_call)` as the run's owner (a delegated run's has no device or session), joined to the run in `agent_run_usage`; a failed, timed-out or abandoned attempt is metered at zero units. No transaction is open while the provider answers (H-1). A caller that disconnects cancels the provider call. | `server/composition/model_gateway.py` |
| AF-P6-7 | **The answer** names only the alias; its text is scrubbed of anything secret-shaped and cut at `max_completion_chars` (16 000) with a marker and `finish_reason: length`. docs/29 §12.2 points at `05` §6's observation bound; that bound (4 000) is too small for a browser runtime's structured answers, so the gateway has its own. | `server/composition/model_gateway.py` |

6A mutants M-AG166–M-AG189 are in `tests/tools/guard_mutations.py` (24/24
killed). No data class changes (no new table or column), so BR-T2 is not
re-measured for 6A; the container rows come with 6C/6E.

### Phase 6, slice 6B — the egress boundary (implementation facts)

Built under OD-AF-12/13 (§2L). Nothing starts it yet; 6C/6D put it in front
of a container.

| ID | Fact (`[IMPL]` unless stated) | Where |
|---|---|---|
| AF-P6-8 | **One resolution rule.** `checked_address` resolves a name with an injected resolver and classifies the answers with `policy.classify`. The egress client keeps its rule (`strict=False`: the first passing answer); the proxy uses `strict=True`: **every** answer must pass, so a name answering a public and a private address is refused (the shape of a rebinding attempt — stricter than the client because the caller is untrusted). | `server/net/resolve.py`, `server/net/client.py` |
| AF-P6-9 | **The CONNECT proxy**, one per run on its own `0600` Unix socket. It understands exactly `CONNECT host:port HTTP/1.1` (8 KiB head, on time); refuses any other method or form, IP literals in every spelling (dotted, integer, octal, hex — the URL standard's last-label rule), single-label names, any port but 443 and any host not exactly in the run's list — before resolving. It then resolves once, classifies every answer, connects to the checked address and **refuses a connection whose peer is another address** (`ip_mismatch`). No TLS interception: bytes are relayed and counted. Per-run limits: connections, concurrency, bytes, idle time. A refusal is a bare status with no echo of the request; every decision goes to the audit callback with a reason code. | `server/net/egress_proxy.py` |
| AF-P6-10 | **A run's hosts** come from JARVIS only: `policy_for_run` takes the exact hosts the spec names and refuses the whole run if any is outside the operator's `EgressPolicy` (its destinations, or `internet`) — never a silently narrower list. Private networks are never reachable from a run. | `server/net/egress_proxy.py` |

6B mutants M-AG190–M-AG202 (13/13 killed). The kernel-level half of the
boundary — that the container has no other route — is 6C's.


## 2K. Agent Factory owner decisions (2026-10-02)

**Owner instruction, 2026-10-02:** for the Agent Factory decisions that block
Phase 5, the recommended values docs/29 already documents (§15, §23.1, §32)
are the owner's decisions for this milestone. Each row below is that
recommendation, recorded as ratified, with its exact scope. Nothing else in
docs/29 is ratified by this: **OD-AF-1, OD-AF-6, OD-AF-9 and OD-AF-10 stay
open** (§3), and docs/29 remains a proposal outside these rows. OD-AF-6 in
particular is not derived: its recommendation depends on which task class the
owner wants first, and Phase 6 (external runtimes, MCP, containers) is not
started.

| ID | Decision (ratified) | Exact scope | Rationale |
|---|---|---|---|
| **OD-AF-2** | **PRD §22 is amended** with docs/29 §15.8's exact sentence: *"An agent with an active, step-up-granted StandingDelegation may execute unattended within its compiled envelope (≤ low_write), outputs to the owner's inbox only. The scheduler itself still never executes."* | The canonical changes of docs/29 §15.8 are made: PRD §22 (and §47), `03` §8 and `04` §1 (DelegatedPrincipal as a second principal form; device operations excluded), `01` §7.1A (StandingDelegation), `docs/22` §0 and OD-SCH-3 (resolved by reference). The feature stays **off by default**: `agents.unattended_enabled: false`, and it can be set true only with `agents.enabled: true` and `agents.standing_delegation_ratified: true` (the operator's acknowledgement of this row). | Phases 1–4 shipped (the docs/29 §32 precondition). The amendment keeps SCHED-001 intact: the scheduler still never executes; the factory's own trigger loop does, only under a delegation. |
| **OD-AF-3** | **The `agent.*` tier table of docs/29 §23.1, as proposed.** | Already-registered `agent.define`/`agent.inspect`/`agent.delete` tiers are signed as built. `agent.delegate`: `grant_standing` is **consequential + step-up**, `revoke_standing` is **low_write and always allowed**. As AF-P2-1 did for run/control, delegation is an **owner HTTP path** (`POST`/`DELETE /api/v1/agents/{id}/delegation`) decided by the engine on the `agentdefinition`, not a worker tool: no task, model or agent can grant a delegation. Every `agent.*` capability stays excluded from every envelope. | The tiers are docs/29's; keeping delegation out of the tool registry removes the only path by which an agent-mediated task could ask for standing authority. |
| **OD-AF-4** | **No consequential action in an unattended run (v1).** | Unattended envelopes are ≤ `low_write`. The compiler rejects an `unattended` trigger for any template whose `risk_ceiling > low_write`, or whose envelope reaches an operation outside the unattended ceiling. At run time a consequential (or higher) request is **refused, never paused**: no confirmation token is issued, no activation is offered, and a run that would pause fails closed. The docs/29 §15.6 *alternative* (delegated confirmation) is not built. | A confirmation needs a present human; an unattended run has none, so a pause would either strand the action or become a confirmation path without a person. |
| **OD-AF-5** | **Delegation lifetime: at most 30 days (`agents.delegation_max_days`, default 30), renewal by a new step-up grant.** | `expires_at ≤ created_at + delegation_max_days`; mandatory, never open-ended. A new grant supersedes the agent's active delegation (status `revoked`, reason `renewed`). The owner is notified (inbox notice) 3 days before expiry and at expiry. | docs/29 §15.4 / §32. |
| **OD-AF-7** | **5 agents per owner; budgets explicit, non-zero, set by the operator.** | `agents.max_agents_per_user: 5` (as built). A delegation requires `budget_per_run > 0` and `budget_per_month > 0`, each ≤ the spec's; a spec whose budgets are 0 cannot be delegated (`budget_required`). The owner's own budget (13) applies on top of both. | Unattended spending has no person watching it; zero must not mean "unbounded" and must not silently become a dead agent either. |
| **OD-AF-8** | **No output beyond the owner's inbox in v1.** | `OutputKind` keeps one value. Unattended results and every delegation notice (skipped, missed, expired, revoked, invalidated, budget, breaker) are inbox items of the owner only; no recipient field exists; notices are data and never authorize anything. | docs/29 §15.7, §19. |

**The unattended ceiling (docs/29 §15.7), as built.** An unattended run is
refused, before the engine is asked, anything outside **all** of: tier ≤
`low_write`; server platform only; capability not `device.*`, `app.*`,
`system.restricted` or `agent.*`; `net.request` only `get` (never `post`).
It runs as a `DelegatedPrincipal` (no device, no session), so device- and
session-scoped grants never match it and device operations are structurally
impossible. Its effective authority for every step is

    owner's live grants ∩ graph scope ∩ template ceiling ∩ compiled envelope
      ∩ standing delegation ∩ runtime/platform ∩ run restrictions
      ∩ risk ceiling ∩ unattended ceiling ∩ global floor

and the delegation is only a ceiling: **no capability is activated by it.**
An unattended run activates only capabilities the owner already holds as a
`user`- or `graph`-scoped standing grant; anything else is refused.

**Deviations from docs/29 §15.8, recorded.**
* `01` §1.2 `CapabilityScopeType += agent` is **not** added. docs/29 gives it
  for "delegation-scoped activation records"; since a delegation is not a grant
  and an unattended run never activates anything the owner has not granted,
  no such record exists. Adding an unused scope value would only widen what a
  grant row could name.
* Step-up for the grant is the device's **re-attestation** (`03` §5.5, the
  user-presence-bound key), the same check `/confirm` applies to a
  `high_irreversible` approval — token freshness does not count, because a
  device refreshes its token with nobody present.

Implementation facts for Phase 5 are recorded in §2J ("Phase 5").

---

## 2L. Agent Factory Phase 6 — owner decisions and open infrastructure decisions (2026-10-02)

**Owner instruction, 2026-10-02:** OD-AF-6 and OD-TOOL-3 are decided as
below, with the owner's own rationale. Nothing else is ratified by this:
the infrastructure choices Browser Use needs (OD-AF-11…15) are recorded as
**open** rows with a recommendation each, and no Phase 6 slice that depends
on one of them is built until the owner decides it. OD-AF-1, 9 and 10 stay
open (§3).

| ID | Decision (ratified) | Exact scope | Rationale (owner's) |
|---|---|---|---|
| **OD-AF-6** | **Browser Use (`browser_use`) is the first external runtime**, under docs/29 §21.1's **P2 contained-workspace** pattern. | Browser Use is **untrusted execution infrastructure**: it never authorizes anything. Its own `allowed_domains`, its safety settings and any framework approval feature are **advisory only, never authorization** (docs/29 §30.3). JARVIS stays the sole authority for task, graph, capability, risk, budget, confirmation and network policy: the JARVIS egress boundary is authoritative for every host it reaches (allowed hosts, checked-IP = connected-IP, `10` §4–§5). **No stored credentials in v1** (`sensitive_data` unused; no browser profile persists between runs). Its model calls go only through the JARVIS Model Gateway, with a run token and no provider key. Results return to the owner's inbox as data (OD-AF-8). No other external runtime is enabled: `letta`, `openhands`, `openclaw` and the SDK frameworks stay reserved ids. | It is a capability the native Agent Factory genuinely lacks (browser-based monitoring and interaction), and P2 keeps the authority boundary at the workspace: what enters, what it can reach, what leaves. |
| **OD-TOOL-3** | **T0 — no MCP, in either direction, for the first Phase 6 provider.** | No inbound MCP (JARVIS consuming third-party MCP servers, `07` §6 / TOOL-004) and no outbound MCP (a JARVIS MCP server as the Tool Gateway's transport). No MCP transport is built for future-proofing. docs/29 §21 item 8's "JARVIS MCP server" is **not required** for a P2 provider: Browser Use's effects are browser actions inside its disposable workspace, bounded by the egress boundary, and its results come back as inbox data — it makes no Tool Gateway calls in v1. PRD §17 (TOOL-004) is unchanged and still governs any future MCP. | The first provider uses the P2 path, which needs no tool transport; an unused transport would only be attack surface. |

### Infrastructure decisions that Browser Use requires

Each row is the owner's. "Requirements" are what the canonical documents
already fix; the options are the realistic ways to meet them. **All five
recommendations were ratified on 2026-10-02** (below the table); the table
is kept as the record of what was weighed.

| ID | Question | Requirements already fixed | Options | Recommendation (ratified 2026-10-02) | Blocks |
|---|---|---|---|---|---|
| **OD-AF-11** | **Container mechanism** for the per-run Browser Use workspace | docs/29 §21 items 1, 4, 5: one disposable container per run; non-root; read-only root filesystem; no host mount except a per-run scratch volume; all capabilities dropped; `no-new-privileges`; deterministic kill (token revocation → `cancel_run` → kill after 10 s); a listing for reconciliation (§25.2). `14` §2: a container escape is a shared-kernel residual, `[FUTURE]` stronger sandbox. JARVIS must not gain root-equivalent authority by driving it. | **(A)** OCI container through a **rootless** Docker-API-compatible engine (rootless Podman or rootless Docker), driven by a launcher in `server/execution` over the engine's local API socket, OCI runtime `runc`. **(B)** As (A), with **gVisor `runsc`** as the OCI runtime (a user-space kernel between Chromium and the host kernel). **(C)** JARVIS-managed OCI bundles run directly by rootless `runc`/`crun`, no daemon (JARVIS owns lifecycle and listing itself). A rootful engine socket is not an option: it is root-equivalent for whoever holds it. | **(B)**, with (A) acceptable only for a disposable-data pilot when `runsc` is unavailable, recorded in BR-T2. A browser parses hostile content, so the kernel is the main residual; rootless means an escape lands as an unprivileged user. | 6C, 6D |
| **OD-AF-12** | **Network namespace design** for the workspace | `10` §3 NET-005 (enforced at the network boundary, not by a proxy variable the tool can ignore; `[REC]` netns + filtered egress, ratified in `14`); docs/29 §21 item 2: the only routes are the Agent Gateway and the egress proxy; item 8: the gateway listeners bound to the container network only; AGENT-T12 (egress probe). | **(A)** **No network interface**: the container has only loopback in its own namespace (`--network none`); the Model Gateway and the egress proxy are reached through **per-run Unix sockets** in a host directory mounted into the container, and a forwarder in the pinned image maps an in-container loopback port to each socket. **(B)** An internal bridge network (no masquerade) plus host nftables rules admitting the container subnet only to the two listener ports on the bridge address. **(C)** A JARVIS-created namespace per run with a veth pair and nftables rules. | **(A)**. There is no route at all, so nothing else on the host (even a service bound to `0.0.0.0`) is reachable; each run gets its own listener in addition to its token; DNS is impossible inside, so the proxy resolves (`10` §5); it needs no privileged firewall rules and is testable unprivileged. (B) and (C) depend on correct host firewall state. | 6B, 6C, the 6A listener's container binding |
| **OD-AF-13** | **Egress proxy architecture** | `10` §2 (exactly the declared destinations), §4 (metadata, loopback and private ranges blocked on the **resolved** IP), §5 (the boundary resolves DNS; **checked IP = connected IP**), §6 (no credential exfiltration); docs/29 §21 item 2 (the operator `EgressPolicy`), §29.2 (allowlist from operator policy, no stored credentials). The JARVIS egress policy is authoritative; Browser Use's `allowed_domains` is advisory. | **(A)** A JARVIS-owned **HTTP CONNECT proxy**, one per run on its socket, **no TLS interception**: the CONNECT host must be in the run's allowed hosts (operator `EgressPolicy` ∩ the spec's declared destinations; no wildcard internet); resolution and IP classification reuse `server/net`, and the proxy connects to exactly the checked IP; port 443 only; per-run connection, byte and rate limits; every decision audited and metered. **(B)** A TLS-intercepting proxy (a JARVIS CA in the container's trust store) enforcing per-request methods (GET/HEAD only) and paths. **(C)** An off-the-shelf proxy (Squid, Envoy) configured from JARVIS policy. | **(A)**. It reuses the one implementation of `10` §4–§5 already tested in `server/net`; (C) would duplicate it and could not prove checked-IP = connected-IP for JARVIS's own classification; (B) puts decrypted page content and a CA into the boundary. **Consequence the owner should accept explicitly:** under (A) the boundary decides *hosts*, not *methods* — a browser can submit a form (POST over TLS) on an allowed host. v1 limits this by explicit hosts only, no credentials and a disposable profile, and OD-AF-15 tiers the capability accordingly; (B) is the path if protocol-level read-only is required. | 6B |
| **OD-AF-14** | **Image and runtime pinning** | docs/29 §21 item 4 (exact version pin per runtime image; the image digest recorded in the runtime profile; upgrades by PR), §29.1 items 6–7 (the adapter refuses other versions; telemetry disabled and blocked anyway); §30 ("re-verify at implementation time": `browser-use` 0.13.10 as assessed 2026-09-30); the offline-after-provisioning precedent (`docs/RUNNING_MEMORY.md`: one sanctioned network step). | **(A)** A Dockerfile in this repository (base image pinned by digest; `browser-use` and every dependency from a hash-locked requirements file installed with `--require-hashes`; the pinned Playwright Chromium build; `ANONYMIZED_TELEMETRY=false`), **built and published by CI** to the repository's container registry; the profile pins the **published digest**; an explicit provisioning command pulls by digest only and the launcher refuses any other. **(B)** As (A), but built locally by the provisioning command, pinning only the base digest and the lock hash (the built digest is not reproducible, so it cannot be pinned in the repo). **(C)** The upstream Browser Use image, pinned by digest. | **(A)**. It is the only option where the repository records the exact digest that runs, and every upgrade is a reviewed PR. (B) cannot satisfy §21 item 4 literally; (C) ships code and defaults (telemetry, extra tools) JARVIS has not reviewed. Publishing uses CI's own token, not a stored secret. | 6C, 6D |
| **OD-AF-15** | **Capability and tier for browsing**, and the `browser_monitor` template | The capability registry is closed (`07`); tiers are the owner's (OD-TOOL-1, as OD-AF-3 was); the unattended ceiling is ≤ `low_write` (OD-AF-4); docs/29 §29.2: no stored credentials, `use_vision` only for non-sensitive domains. Nothing in the registry today authorizes "browse these hosts". | **(A)** A new capability `browser.session` with one operation `browse` at **`low_write`**, scope = an explicit host list (required, no wildcard), mapped to the template's single browsing ability. **(B)** The same at `low_read`. **(C)** Reuse `net.request` `get` with a browser platform. | **(A)**. Under OD-AF-13 (A) the boundary cannot prove read-only, so `low_read` would overstate it; `low_write` is still inside the unattended ceiling. (C) would let the browser's tier be read as a single GET, which it is not. `use_vision`: off in v1. | 6D |

**What stays as it is.** The agent proposes; deterministic JARVIS code
authorizes; tools execute; a person confirms where required. The external
runtime is never an authority: a compromised Browser Use container holds a
run token (bound to one run, revocable, no identity), reaches only its two
sockets, and has no path to a principal, a session, a device credential, a
secret or a provider key. Unattended runs keep the Phase 5 ceiling, and
consequential actions stay impossible in them (OD-AF-4).

**Phase 6 slices and their decision boundaries.** 6A (the HTTP Model
Gateway, docs/29 §12) needs none of OD-AF-11…15 and is built now, with its
listener restricted to the internal-only bindings every OD-AF-12 option can
use (a Unix socket, or loopback). 6B (the egress boundary) waits on OD-AF-12
and OD-AF-13; 6C (container and namespace) on OD-AF-11, 12 and 14; 6D (the
Browser Use adapter) on OD-AF-14 and 15; 6E/6F (integration, kill path,
reconciliation, BR-T2 container rows) on all of them.

### OD-AF-11…15 — ratified (owner, 2026-10-02)

**Owner instruction, 2026-10-02:** the recommended answers in the table
above are ratified for the Phase 6 implementation, as stated here. Nothing
else is decided by this; OD-AF-1, 9 and 10 stay open.

| ID | Decision (ratified) | Exact scope |
|---|---|---|
| **OD-AF-11** | **A rootless container engine with gVisor** is the external runtime's boundary. | One disposable container per run, started by the JARVIS server's own unprivileged user through a rootless OCI engine (Podman, driven by argv from `server/execution`), with **gVisor `runsc`** as the OCI runtime. Non-root inside; read-only root filesystem; all capabilities dropped; `no-new-privileges`; one per-run scratch directory as the only writable mount; deterministic kill (token revocation → `cancel_run` → stop, killed after 10 s); a label on every container for reconciliation (every 10 minutes, and at startup). A rootful engine socket is never used. Verified feasible on 2026-10-02: rootless Podman 4.9 + `runsc` release-20260928.0 runs a container with no capabilities, `NoNewPrivs`, a read-only root, a non-root uid and the host's home directories invisible. |
| **OD-AF-12** | **No general-purpose network interface.** | The container's only paths out are **per-run Unix sockets** — the Model Gateway's and the egress proxy's — in one host directory mounted into it. Podman `--network=none` plus gVisor's own `network=none` (loopback only, so an in-image forwarder can expose each socket on an in-container loopback port) and `host-uds=open` (the sandbox may open a host socket only if it was mounted in). Verified: every external, private, metadata and host address is unreachable (`ENETUNREACH`), DNS fails, and the mounted socket answers. |
| **OD-AF-13** | **JARVIS's own CONNECT proxy, reusing `server/net`; no TLS interception.** | One proxy per run, on its socket. **It is authoritative** for outbound hosts and IPs: the CONNECT host must be in the run's explicit host list ∩ the operator's `EgressPolicy`; the host is resolved by the proxy (never by the runtime), every resolved address is classified by `server/net/policy.py`, and the proxy connects to exactly the checked address (**checked IP = connected IP**). Port 443 only; default deny. Browser Use's `allowed_domains` and any similar runtime or library control are **advisory only**. |
| **OD-AF-14** | **Images built and published by CI, pinned by immutable digest.** | The Browser Use image is built from this repository (base image pinned by digest, dependencies hash-locked, telemetry off) by CI and published to the repository's registry; the runtime refuses any image reference that is not `name@sha256:<digest>` — **no floating tag is ever executed** — and the digest it runs is the one recorded in the repository. |
| **OD-AF-15** | **`browser.session`, one operation `browse`, at `low_write`, constrained to an explicit host allowlist.** | The scope is a required, non-empty list of exact host names (no wildcard, no IP literal). The capability is the closed registry's; the tier is the owner's. `use_vision` is off in v1; no stored credentials. |

**The accepted trade-off (OD-AF-13 → OD-AF-15).** Without TLS
interception the proxy decides **which allowed hosts** the browser may
reach, but it **cannot see HTTP methods or form submissions inside TLS**: a
browser can POST to a host it may reach. That is why `browse` is `low_write`,
not `low_read`. v1 limits it further with an explicit host list (no
wildcard), no stored credentials, and a fresh browser profile per run.

---

## 3. Genuinely unresolved owner decisions

| ID | Question | Why it is the owner's |
|---|---|---|
| (matrix §5.1) | Sensitive-app classification for UI primitives | A tap can complete a payment. Which apps are "sensitive" and how far their tiers rise is a product-risk call. Needed before `08` ships device control. *(Phase H: the mechanism is implemented (`android.app_classification`, server gate + device guard) and the lists ship **empty**, so no app is UI-controllable until the owner classifies one. The lists themselves are still the owner's.)* |
| OD-DP-9 | Ratify DecisionProvider at all | Unchanged; nothing in the runtime depends on it. |
| OD-MT-2 | Any cloud primary by default | Default local (ollama); unchanged. |
| OD-SCH-1 | Ratify `scheduler.create` / `low_write` | Implemented as proposed (§2D); the owner signs the tier table. |
| OD-SCH-2 | Fire-time `suggest` task with a sessionless principal | Not implemented (docs/22 recommendation); would introduce a principal with no session. |
| OD-VOI-1 | Whether any server STT provider is enabled at pilot | A cloud STT receives user audio; default is on-device only (§2E). |
| VOI-B3 | Opt-in raw-audio retention (LIFE-002) | Not built; where retained audio would live and for how long is a privacy call. |
| OD-JDG-1 | Ratify EvaluationProvider as separate from DecisionProvider | Built as recommended behind `evaluation.enabled` (§2G); not ratified. |
| OD-JDG-2 | Judge budget scope | Own budget always; `budget_scope` default `own_and_global` (§2G); not ratified. |
| OD-JDG-3 | Pilot Judge provider (model, cloud or local) | None chosen; the Judge is off by default. |
| OD-JDG-4 | `usage.kind` extension vs `model_call` + attribution | `model_call` + `evaluator:` attribution; the locked enum is unchanged. |
| OD-DASH-1 | Dashboard/control split vs amending DASH-002 | Split built as recommended; DASH-002 unchanged; not ratified. |
| OD-DASH-2 | Console UI | Not built; JSON API only (§2G). |
| OD-JDG-5 | How approved Judge guidance may carry user content (§2H) | Global guidance with secret screening only reaches every user with one user's content (BR-T2 row 38); which control fits is a product and privacy call. |
| OD-AF-1, 9, 10 | The Agent Factory's abstractions, attribution form and `01` entities (§2J) | Still open. OD-AF-2, 3, 4, 5, 7 and 8 were ratified on 2026-10-02 (§2K); OD-AF-6 (`browser_use`, P2), OD-TOOL-3 (no MCP) and OD-AF-11…15 (Browser Use's infrastructure) on 2026-10-02 (§2L). |

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
