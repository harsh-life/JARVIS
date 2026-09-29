# Desktop Endpoint Preparation Report

**Status:** `PREPARATION — NOT CANONICAL`. This report audits the repository against
`docs/24_ENDPOINT_ARCHITECTURE_DESKTOP_ASSESSMENT.md`, which is itself
`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]`. It decides nothing, ratifies nothing,
and does not rank below or above any document. The authority order is unchanged
(`docs/DECISION_REGISTER.md`): PRD > decision register > subsystem contracts >
implementation > proposals.

**Audited:** `harsh-life/JARVIS` `main` @ `ddbb038` (2026-09-29). docs/24 was written
against `4223c5f`, 97 commits earlier; §2.1 lists where that shows.

**Not changed by this work:** the runtime, authorization, identity, confirmation,
secrets, Android authority, execution, and the Real-Data Gate. No file under
`server/`, `shared/` or `android/` was modified. **REAL-DATA GATE: CLOSED**
(`docs/RELEASE_VALIDATION.md` §P), unchanged; desktop preparation clears none of its
blockers. Darwin / Black Box: not started.

**Labels**

| Label | Meaning |
|---|---|
| **SAFE NOW** | doable today without any open decision; changes no semantics |
| **OWNER DECISION REQUIRED** | blocked on an `OD-*` item only the owner can close |
| **IMPLEMENT AFTER RATIFICATION** | the design is clear, but it changes a contract, schema, API or boundary |
| **FUTURE** | considered, not a target |

The client-side conformance cases a desktop build must pass are in
`docs/DESKTOP_CONFORMANCE_PLAN.md`.

---

## 1. Current repository state

**Built and merged** (see `README.md`): the security core (Google OIDC as a public
PKCE client, Ed25519 device credentials, opaque access tokens, step-up by
re-attestation), the agent runtime with task modes and the breaker, constrained
execution, memory and vault, the Android client (docs/23 Phases B–G: device-held
keys, the App Link login return, the authenticated device channel, the device-side
guard, perception, the presentation layer), the scheduler, voice, the Judge and the
read-only console, and the PostgreSQL runtime store (H-1).

**Not present:** any desktop code; a `desktop/` directory; a Rust or TypeScript
toolchain, lockfile or CI job; any desktop value in any enum; any `endpoint_class`,
`credential_alg`, `return_to` or endpoint-profile field anywhere.

**Gate:** CLOSED. Blockers (`RELEASE_VALIDATION.md` §P): Android on physical
hardware, OD-MB-4, H-2, OD-JDG-5, and the real-integration smoke. Desktop is not on
that list and adding it would not shorten it.

---

## 2. Document-24 compliance audit

### 2.1 Where docs/24's premises no longer match `main`

| docs/24 says | `main` actually does | Consequence |
|---|---|---|
| §2.2.1: the OIDC callback returns JSON; "the same gap `23` §3 found for Android" | Solved for Android. `/auth/oidc/start?app_state=…` makes the callback `302` to the verified App Link with `bootstrap_token` and `app_state` in the **fragment** (`server/gateway/routers/auth.py:136-215`, migration `e4f7a2c9b1d3`, `tests/security_core/test_app_link_handoff.py`). Without `app_state` the JSON contract is kept. | The shared server change of §9.2 is not "build once before either client". Android is done; desktop needs an *additional* return type (§4). |
| §2.2.5 / §10.5: a phone-started action "can only be confirmed from the phone's session"; OD-EP-5 recommends "defer; confirm on the originating endpoint" | **Same-user approval from another device already works.** The token row binds to the *task's* session (`server/capabilities/confirmation.py`), and `AgentRuntime.confirm` accepts any caller whose `user_id` owns the task — "same-user continuity" (`server/agent/runtime.py:355-372`), from OD-A1's owner decision ("one user's multiple devices share that user's authorized state"). Tested: `tests/runtime/test_confirmation_boundary.py::test_the_same_user_may_approve_from_another_device`. Tier-4 step-up is judged on the **approving** device (new test, §8). | **Conflict, documented, not resolved.** Approval is already cross-device; what does not exist is *delivering* the prompt to the other device (no task listing, no push of a pending action). "Confirm on the originating endpoint only" would **restrict** current confirmation semantics — a stop condition. See §10, OD-EP-5. |
| §9.3 row B: hardware-bound keys "need a server change" (P-256) | True for the **device credential** (Ed25519 only, `server/auth/device.py`). But the **step-up key** is already ECDSA P-256, SPKI, platform-neutral on the server (`server/auth/step_up.py`), enrolled in the 15-minute window after registration. | Desktop step-up via Secure Enclave / Windows Hello–TPM needs **no** server change and does not depend on OD-EP-2. |
| §3: "live task status over the endpoint channel" | Android reads task status by **polling** `GET /agent/tasks/{id}` every 3 s (`android/…/tasks/TaskTracker.kt`); the channel carries only operations, cancels, reminders and platform status. | A zero-adapter desktop release 1 needs the channel only for reminders. Text tasks, status, confirmation and step-up are plain HTTPS. |
| §12: the panel "consumes the same `PresentationState` stream defined in `23` §7" | `PresentationState` is a **client-side Kotlin type** (`android/app/…/presentation/`), derived by `Presenter` from `AgentResult` + device state. It is not on the wire and not in `shared/`. | A non-Kotlin desktop re-implements the mapping from the shared `AgentResult`; a Compose Multiplatform desktop could reuse it. Relevant to OD-EP-1. |
| §13: "lint and type checks … (the repository has none today)" | Kotlin: ktlint + detekt in CI. Python: import-linter only; no ruff/mypy. | Only the Rust/TS part of that note still stands. |
| §2.2.3: `ExecutionPlatform.LINUX` is the server host | Confirmed (`shared/schemas/agent.py:30-38`; `server/tools/platforms.py:292` maps `system.restricted` to it). | Unchanged: `desktop_*` must never reuse `linux`. |
| §2.2.4: device operations go only to the authorizing device | Confirmed (`server/execution/android.py:382`, `DeviceHub.send`). | New runtime tests pin it for a same-user second endpoint (§8). |

**Findings docs/24 does not mention:**

1. **`Device.platform` is stored and never read.** No routing, channel, dispatch or
   authorization code consults it (`grep DevicePlatform server/` → only the model,
   the registration request and `DeviceService.register`). Harmless today, since
   `android` is the only value. But any second device kind that joins the channel
   today would be dispatched Android operations its own tasks authorize.
2. **The channel is Android-shaped.** `WS /api/v1/devices/channel` is gated by
   `android.enabled`, and `DeviceHello.mapping_version` must equal the Android
   mapping's version, otherwise the socket is closed (`server/gateway/routers/device_channel.py`).
   A zero-adapter endpoint has no honest value to send.
3. **Reminders fan out to every non-revoked device of the owner**
   (`server/composition/scheduler.py:377`). A registered desktop would be queued
   reminders (72 h expiry) whether or not it can receive them.
4. **No user-facing device list.** Only `GET /api/v1/admin/devices` (superuser)
   exists. A user cannot see or revoke "my laptop" from their phone without already
   knowing its id. More endpoints per user make this a real gap.
5. **Platform-neutral conformance vectors live under `shared/android/`.**
   `proof_vectors.json`, `task_samples.json`, `reminder_samples.json` and
   `voice_samples.json` describe the server contract, not Android.

### 2.2 Area-by-area matrix

| Area | Current implementation | Desktop requirement (docs/24) | Gap | Safe preparation? | Owner decision? |
|---|---|---|---|---|---|
| Authentication (OIDC) | Google OIDC, server is a public PKCE client, no client secret exists (`server/auth/oidc.py`, `login.py`) | same chain | none | pinned (§8) | — |
| OIDC callback / handoff | JSON, or `302` to the Android App Link with the token in the fragment when started with `app_state` | system browser → loopback `return_to` → one-time code + verifier | loopback return type and code redemption do not exist; the fragment design cannot serve loopback (§4.3) | plan only (§4.6); "no open redirect today" pinned | IMPLEMENT AFTER RATIFICATION (docs/24 §9.2); Android migration optional (NEW-4) |
| Device registration | `POST /devices`, bootstrap bearer, `platform ∈ {android}`, optional device-held Ed25519 public key + proof | a desktop platform value; device-held key | no desktop platform value | pinned: unknown platform refused without spending the login | OD-EP-9 |
| Device credentials | Ed25519 only; server never holds the private key when the device supplies its own | Ed25519 in the OS store (A) or hardware P-256 (B) | B needs `credential_alg` | — | OD-EP-2 |
| Sessions / access tokens | opaque, 15 min, refreshed with a proof, instant revocation; session ≡ device's user | same | none | — | — |
| Endpoint / device schema | `devices(platform, credential_ref, step_up_*, push_*)` | `endpoint_class`, desktop platform | columns/values absent; `platform` never read | — | OD-EP-9 |
| Execution platform enum | `server`, `linux` (server host), `android` | `desktop_windows/macos/linux` | absent | — | IMPLEMENT AFTER RATIFICATION (§10.3); release 3+ only |
| Gateway channel | Android device channel, gated by `android.enabled`, Android mapping version required | generalized "endpoint channel" | Android-only hello and gating | — | IMPLEMENT AFTER RATIFICATION (docs/24 §19 row 4) |
| WebSocket transport | hello with token + fresh proof; one socket per device; re-validated every 60 s; revocation closes it | same | none for transport itself | — | — |
| Capability routing | tool → `ExecutionPlatform` adapter; device ops to exact `device_id`; no platform check on the target device | never route an Android op to a desktop | missing target-platform check (finding 1) | pinned: exact-device routing | IMPLEMENT AFTER RATIFICATION (before any desktop joins the channel) |
| Confirmation | single-use, action-bound token; same-user approval from any device | docs/24 assumes session-bound | premise conflict | pinned: step-up on the approving device | **OD-EP-5 (conflict)** |
| Step-up | re-attestation with a P-256 user-presence key, enrolment window 15 min, 60 s challenge, 5 min validity | Touch ID / Windows Hello | none server-side; Linux has no uniform hardware user-presence key | — | NEW-2 (Linux tier-4 approval) |
| Task lifecycle | `POST /agent/tasks` (modes `execute/draft/suggest/observe`, Idempotency-Key), `GET` by id, `/confirm`, `/cancel` | same | no "list my tasks" (discovery across endpoints) | — | only if OD-EP-5 wants prompt delivery |
| Notifications | none server-pushed except reminders over the channel and a content-free FCM wake | native OS notifications of own task events | desktop learns of its own task results from the HTTP response or polling; nothing pushes events | — | FUTURE (event stream is part of the channel generalization) |
| Reminders | per-device queue, channel `features: ["reminders"]` | notifications | fan-out includes any registered device (finding 3) | — | IMPLEMENT AFTER RATIFICATION (fan-out by endpoint capability) |
| Presentation state | client-side Kotlin `PresentationState` | same signals on desktop | not shared on the wire | documented (§6) | follows OD-EP-1 |
| Voice | `POST /voice/transcribe`, `/voice/synthesize`; server providers **off** by default (OD-VOI-1); voice can never confirm or step up | release 2: server STT recommended | server STT is disabled by default; enabling it is OD-VOI-1 | — | OD-EP-3 **and** OD-VOI-1 (NEW-3) |
| Configuration | `server.base_url`; `android.*` (channel, App Links, classification, push, vision) | `auth.return_to_allowlist`; desktop client config | no desktop/native-return config | — | IMPLEMENT AFTER RATIFICATION (docs/24 §19 row 6) |
| Deployment | Cloudflare tunnel config field; stable hostname still an open item (`NEXT_BUILD_INDEX.md` §6 item 3) | same public hostname | shared with Android | — | existing open item |
| CI | `checks` (SQLite), `postgres`, `android` jobs; no secrets | Rust/TS lint, tests, audit, SBOM; signed builds | nothing for desktop | plan (App. A) | OD-EP-1, OD-EP-7 |
| Signing / updater | Android debug builds only in CI; no release signing | Developer ID + notarization; Authenticode OV; Tauri updater key | all absent | plan (App. A) | OD-EP-7, OD-EP-8 |
| Shared schemas | `shared/schemas/*` pydantic, strict; Kotlin mirrors by hand, held to `shared/android/*.json` | language-neutral types | no JSON-Schema/codegen export; neutral vectors filed under `android/` | deliberately deferred (§5) | follows OD-EP-1 |
| Android interoperability | Android authoritative for its own adapters; OD-DEV-1 | unchanged | none | pinned | OD-EP-6 (keep) |
| Filesystem / execution adapters | server-side `fs` sandbox (`dir_fd` + `O_NOFOLLOW`); no client-side fs adapter | release 3+: per-folder grants, device-side containment | all absent — by design for release 1 | none built | IMPLEMENT AFTER RATIFICATION, per capability |
| Security tests | AUTH-T*, AND-T*/ANDC-T*, BR-T2 rows for Android | endpoint tests + BR-T2 desktop dimension | desktop dimension absent | 2 files added (§8); client plan in `DESKTOP_CONFORMANCE_PLAN.md` | — |
| Import boundaries | 24 import-linter contracts; `shared` never imports `server` | `desktop/` shares only `shared/` | no contract for a `desktop/` peer (it would not be Python) | — | IMPLEMENT AFTER RATIFICATION (a repo-boundary test like `test_android_repo_boundary.py`) |
| Documentation | docs/24 committed as proposal; not in `NEXT_BUILD_INDEX.md` or `TRACK_B_ARCHITECTURE_INDEX.md` | docs/24 §19 follow-ups | index entries, 01/02/03/15/16/17 edits | this report | owner authorizes edits to existing docs |

---

## 3. Existing reusable infrastructure

Reusable by a desktop endpoint **as is**, with no server change:

| Piece | Where | Why it already fits |
|---|---|---|
| Identity derivation | `server/auth/sessions.py::resolve_principal` | takes a bearer token and nothing else (PHONE-003) |
| Device-held key registration | `POST /devices` with `public_key` + `key_proof` | the server never holds the private key |
| Proof format and refresh | `server/auth/device.py` (`v1.<device>.<nonce>.<ts>.<sig>`), `POST /sessions/token` | platform-neutral; replay-proof via single-use nonce + 120 s freshness |
| Rotation / revocation | `POST /devices/{id}/rotate` (step-up gated), `DELETE /devices/{id}` (deliberately not step-up gated) | per device; closes that device's socket |
| Step-up | `POST /devices/me/step-up-key`, `/sessions/step-up/challenge`, `/sessions/step-up` | P-256 SPKI; judged on the approving device |
| Tasks | `POST /agent/tasks` (+ `mode`), `GET /agent/tasks/{id}`, `/confirm`, `/cancel` | owner-only; same-user continuity |
| Canonical confirmation payload | `PendingAction` in `shared/schemas/agent.py` | capability, operation, arguments, tier, `requires_step_up`, expiry — never model prose |
| Voice | `/voice/config`, `/voice/transcribe`, `/voice/synthesize` | endpoint-neutral; providers off by default |
| Reminders on a socket | `DeviceReminder` / `DeviceReminderAck` | message-only, device-scoped ack |
| Conformance vectors | `shared/android/proof_vectors.json`, `task_samples.json`, `reminder_samples.json`, `voice_samples.json` | describe the server's formats, not Android behaviour |

---

## 4. Shared server changes identified

### 4.1 What exists

`/auth/oidc/start` (public) stores `{state, nonce, code_verifier, redirect_uri,
app_state?}` in `oidc_login_states` (10 min TTL, single-use) and returns the provider
URL. The **server** is the PKCE client with Google; the verifier never leaves it.
`/auth/oidc/callback` spends the state, exchanges the code, validates the id_token,
maps `sub` → user, and mints a **bootstrap token** (10 min, single-use, stored hashed,
accepted only by `POST /devices`). Then:

- no `app_state`: `200` JSON `{needs_device_registration, bootstrap_token}`;
- with `app_state` (only if `android.app_links` is configured): `302` to
  `{android.app_links.base_url}/app/login#bootstrap_token=…&app_state=…`, `no-store`.
  Android verifies ownership of that link through `/.well-known/assetlinks.json`,
  and the app accepts only the `app_state` it generated.

### 4.2 What Android depends on

`app_state` (pattern `^[A-Za-z0-9_-]{32,128}$`), `APP_LINK_PATH = /app/login`,
the fragment encoding, `assetlinks.json`, and the fallback page. Consumers:
`android/…/auth/LoginCoordinator.kt`, `LoginLinkActivity.kt`. **None of this may
change for desktop.** Every desktop addition below is additive.

### 4.3 What desktop requires, and why the App Link design does not transfer

- **The fragment never reaches a loopback listener.** Browsers do not send the
  fragment in a request, so a `302` to `http://127.0.0.1:PORT/cb#bootstrap_token=…`
  delivers nothing to the desktop's one-shot listener. The value must travel in the
  **query**.
- **A loopback URL is not a verified owner.** An App Link is verified by the OS
  against `assetlinks.json`; a loopback port is merely whatever process bound it.
  RFC 8252 §8.3 accepts loopback but relies on PKCE-style binding for this reason.
- So the query must carry a **one-time code that is useless alone**. It is redeemed
  with a **verifier** only the endpoint that started the login holds.
- The redemption must return the **same** bootstrap token as today. Nothing
  downstream changes: registration, proofs, sessions and step-up are untouched.

### 4.4 Shared by both, or not

| Change | Android | Desktop |
|---|---|---|
| `return_to` loopback + `bootstrap_code` + `POST /auth/oidc/redeem` | optional (App Link works; migrating is NEW-4) | required |
| Config allowlist of return kinds (`auth.native_return`) | no | yes |
| Desktop platform value / endpoint class | no | yes (OD-EP-9) |
| Target-platform check before dispatching a device operation | yes: a safety net once a second kind exists | yes |
| Channel hello without the Android mapping version | no | yes (for reminders) |
| User-facing `GET /devices/me/…` list (finding 4) | useful | useful |

### 4.5 Is any of it authorized now?

No. docs/23's handoff was implemented as the App Link form. docs/24 §9.2 is a
proposal, and adding a public-endpoint parameter plus a new public redemption
endpoint changes `02`, `03` §2 and `15` (docs/24 §19 rows 1, 6). Per the stop
conditions, **plan only**.

### 4.6 Exact implementation plan (IMPLEMENT AFTER RATIFICATION)

Tests first; each line of the plan is one commit.

1. **Config** (`server/config/schema.py`): `auth.native_return: {loopback: bool =
   false}`. Loopback means literally `http://127.0.0.1:{1024..65535}/cb` or
   `http://[::1]:{port}/cb`. Never `localhost` (DNS), any other path, a query, or
   userinfo. Off by default.
2. **Migration**: `oidc_login_states` gains `return_to` (nullable) and
   `client_challenge` (nullable, S256 base64url, 43 chars). A new table,
   `login_handoff_codes`, holds `code_hash` (PK), `bootstrap_token_hash`,
   `client_challenge`, `expires_at` (≤ 60 s) and `used_at`. The bootstrap token
   itself is never stored in plaintext; see step 4. Round-trip on SQLite and
   PostgreSQL.
3. **`/auth/oidc/start`**: accept `return_to` + `client_challenge` together or not at
   all; mutually exclusive with `app_state`; refuse (`422`) unless loopback is enabled
   and the value matches exactly. `client_challenge` is **separate** from the
   server's own Google PKCE verifier and never replaces it.
4. **`/auth/oidc/callback`**: for a loopback login, mint the bootstrap token as
   today. Store the **raw** token sealed under the SecretStore, or mint the token at
   redemption instead. **Recommended: mint at redemption.** The code row then
   records only `user_id` + challenge, so no bootstrap token exists before
   redemption. Respond `302` to `return_to?code=…&state=<client state>` with
   `no-store` and `Referrer-Policy: no-referrer`.
5. **`POST /auth/oidc/redeem`** (public, body `{code, code_verifier}`): single-use
   conditional UPDATE, expiry, then `S256(verifier) == challenge`. Only then is a
   bootstrap token minted and returned as JSON. Every failure is one generic
   `401`, audited.
6. **Tests**: code replay; wrong verifier; expired code; code for user A with B's
   verifier; `return_to` variants (`localhost`, a path, a query, `https`, a
   non-loopback IP, `127.0.0.2`, port 80); `return_to` without a challenge;
   `return_to` + `app_state`; the Android App Link tests unchanged and green; audit
   rows; no token in any log line; loopback disabled by default.
7. **Docs**: `02` §3, `03` §2/§3.2, `15`, `docs/23` §3 (a cross-reference only),
   `RUNNING_FOUNDATION.md`.

---

## 5. Shared protocol / schema gaps

**What a desktop can consume today:** `shared/schemas/agent.py` (`AgentResult`,
`PendingAction`, `PlatformWait`, `TaskMode`, `AgentTaskStatus`), `voice.py`,
`scheduler.py`, the reminder frames in `device_channel.py`, and the platform-neutral
vectors listed in §3.

**Gaps (none closed here):**

| Gap | Why it is not done now | Label |
|---|---|---|
| Endpoint class / desktop platform identity | OD-EP-9 | OWNER DECISION REQUIRED |
| Endpoint capability profile ("what I can do", routing only) | OD-EP-9; nothing routes on it yet | OWNER DECISION REQUIRED |
| Channel hello for a zero-adapter endpoint (no Android `mapping_version`) | changes the docs/23 §4 wire contract, which Android parses strictly | IMPLEMENT AFTER RATIFICATION |
| Language-neutral type export (JSON Schema from the pydantic models, or codegen) | safe in itself, but its form depends on OD-EP-1: Compose MP reuses the Kotlin mirrors, Tauri needs TS/Rust. Generating now would add an artifact to keep current with no consumer. | SAFE NOW, deliberately deferred to OD-EP-1 |
| Neutral vectors moved out of `shared/android/` | a repo-layout change touching the Android build (`test_the_build_reaches_outside_android_only_for_shared_android`) | IMPLEMENT AFTER RATIFICATION (docs/24 §19 row 7) |

**Provisional sketch (PROVISIONAL, not code, not ratified).** Recorded so the
decision has something concrete to accept or reject:

```
DevicePlatform:  android | desktop_windows | desktop_macos | desktop_linux      (never "linux")
Device:          + endpoint_class ∈ {android, desktop, …}  + credential_alg ∈ {ed25519, ecdsa_p256}
EndpointHello:   {access_token, device_proof, client_version, features[],
                  adapter_table: {name, version} | null}        # null = zero adapters
Rule:            nothing in server/graph or server/capabilities reads any of these
                 (pinned today: tests/security_core/test_endpoint_preparation.py)
```

---

## 6. Android reuse map

| Concept | Android location | Reuse for desktop | Note |
|---|---|---|---|
| Login + registration | `auth/LoginCoordinator.kt`, `ApiClient.kt` | conceptual; the return path differs (§4.3) | same bootstrap → `POST /devices` with a device-held key |
| Device key custody | `auth/KeystoreDeviceKeyStore.kt` | pattern | Keystore Ed25519 where possible; otherwise a software Ed25519 key **sealed under a hardware AES key**. A desktop analogue (seal under a TPM or Secure Enclave–wrapped key) is option C for OD-EP-2 and needs no server change. `[verify at implementation]` |
| Proof signing | `contract/DeviceProof.kt` + `shared/android/proof_vectors.json` | direct (vectors), logic ported | format is server-normative (`build_device_proof`) |
| Session manager | `auth/SessionManager.kt` | conceptual | tokens in memory only |
| Step-up | `auth/StepUpFlow.kt`, `StepUpKey.kt`, `BiometricPresence.kt` | conceptual | server endpoints unchanged; key registered in the enrolment window |
| Channel client | `channel/DeviceChannel.kt`, `ChannelService.kt` | conceptual, after the hello is generalized | exact-device binding, reauth, close codes |
| Task tracking | `tasks/TaskTracker.kt`, `TaskController.kt`, `contract/Tasks.kt` + `task_samples.json` | direct (vectors), logic ported | one task at a time, 3 s poll, retry only with the same Idempotency-Key |
| Presentation | `presentation/*` (`Presenter`, `PresentationState`) | direct if Compose MP; otherwise port the mapping table | signals only, no content; never approves |
| Confirmation card | `ui/TaskPanel.kt`, `ui/SecureTouch.kt` | conceptual | built only from `PendingAction`; drops obscured touches. The desktop equivalent is in `DESKTOP_CONFORMANCE_PLAN.md` |
| Reminders | `reminders/*`, `contract/Reminders.kt` + `reminder_samples.json` | direct (vectors) | "Start task" only pre-fills the input |
| Voice | `voice/*`, `contract/Voice.kt` + `voice_samples.json` | conceptual | transcript fills the input box only |
| Device guard, mapping, perception, grid, overlay, Shizuku, push | `contract/DeviceGuard.kt`, `perception/*`, `permissions/*`, `overlay/*`, `privileged/*`, `push/*` | **not for release 1** | release 3+ reuses the *pattern* of the guard, not the Android table |

**Must not be ported** (docs/23 §1; `tests/execution/test_android_repo_boundary.py::test_donor_architecture_is_absent`
pins the Android side):

- the old HyperMind auth architecture and any client-held server secret (REPO-T2);
- on-device model routing (Groq / OpenAI / Anthropic endpoints called from the client);
- the raw-ADB allow-list gate, and any generic ADB, shell or `ProcessBuilder` path;
- the old token storage. Access tokens stay memory-only and the device key stays
  in the OS store, as on Android (`auth/EnrollmentStore.kt`);
- any client-side authorization: the client may **refuse more**, never permit more.

---

## 7. Safe preparation completed

- `docs/24_ENDPOINT_ARCHITECTURE_DESKTOP_ASSESSMENT.md` committed unchanged, as a proposal.
- Two characterization test files, pinning current behaviour at every point
  docs/24 proposes to change (§8).
- This report and `docs/DESKTOP_CONFORMANCE_PLAN.md`.

Deliberately **not** done: any schema, enum, config, migration, endpoint or channel
change; any framework dependency; any `desktop/` scaffold; index or register edits;
any change to docs/24.

---

## 8. Tests added and results

These are characterization tests. They pass on the current code by design; each was
**mutation-checked** to prove it is not vacuous.

**`tests/runtime/test_endpoint_boundaries.py`** (3 tests)

| Test | Pins | Records the state of | Mutation that fails it |
|---|---|---|---|
| `test_an_operation_never_goes_to_the_users_other_connected_endpoint` | a task on endpoint B never dispatches to endpoint A, even with A online and a user-wide grant; nothing queued | OD-DEV-1 / OD-EP-6 | `DeviceHub.send` falling back to "any session of this user" |
| `test_each_endpoint_receives_only_the_operations_it_authorized` | with both online, B's operation reaches only B's socket, addressed to B | OD-DEV-1 | — (companion) |
| `test_a_tier4_approval_from_another_endpoint_needs_that_endpoints_own_step_up` | A's re-attestation does not satisfy B's tier-4 approval; B's own does | OD-EP-5 | treating step-up as always fresh in `/confirm` |

**`tests/security_core/test_endpoint_preparation.py`** (70 cases)

| Test | Pins | Records the state of |
|---|---|---|
| `test_the_registry_has_exactly_one_device_platform` | `DevicePlatform == [android]` | OD-EP-9 (fails when `desktop_*` is added: update in the same commit) |
| `test_an_unregistered_platform_is_refused_without_spending_the_login` ×7 | `desktop*`, `linux`, `server`, `browser` → `422`; bootstrap token still usable | OD-EP-9 |
| `test_registration_carries_no_profile_or_identity_claim` ×5 | `endpoint_class`, `credential_alg`, `profile`, `capabilities`, `user_id` → `422` | OD-EP-2, OD-EP-9 |
| `test_the_caller_cannot_choose_where_the_login_result_goes` | `return_to`/`redirect_uri` ignored; provider gets the server's callback; callback is JSON, no `Location` | docs/24 §9.2 unbuilt; no open redirect |
| `test_a_bootstrap_token_is_not_an_access_token` | bootstrap token → `401` on tasks and graphs, and is not spent by that | 03 §3.2 |
| `test_client_frames_cannot_carry_identity_authority_or_profile` ×53 | `DeviceHello`, `DeviceReauth`, `SubmitTaskRequest`, `ConfirmRequest`, `DeviceRegistrationRequest`, `DeviceRotationRequest` refuse `user_id`, `device_id`, `session_id`, `endpoint_class`, `platform`, `capabilities`, `risk_category`, `authorized`, `step_up_fresh` | PHONE-003 |
| `test_the_authorization_engine_never_reads_what_an_endpoint_says_about_itself` ×2 | `server/graph`, `server/capabilities` reference no platform, class, version or feature name (AST) | docs/24 §16 "profile is an advertisement, not a grant" |

Mutations checked: adding `DESKTOP_WINDOWS` to `DevicePlatform` fails 2 cases;
adding a `device.platform` read to `server/capabilities/risk.py` fails the engine
test.

**Runs:** new files 3 + 70 passed; `tests/security_core` 475 passed; the runtime
confirmation, push-wake and platform-wait suites plus `tests/execution/test_device_hub.py`
52 passed; `lint-imports` 24 contracts kept; full suite in §12.

---

## 9. Security review

"Existing" means in the code now. "Proposed" means docs/24. "Missing" means neither.

| Threat | Existing protection | Proposed (docs/24) | Missing | Owner decision? |
|---|---|---|---|---|
| Compromised webview | n/a (no desktop) | no token, no key, no network in the webview; allowlisted IPC; sanitized output | CSP and IPC allowlist tests (conformance plan §2) | OD-EP-1 (the split is Tauri-specific; Compose MP has no webview) |
| Modified desktop binary | server derives identity (PHONE-003); engine never reads client self-description (pinned); client guard can only refuse | same | — | — |
| Stolen device credential | revocation, 15 min tokens, single-use nonces, step-up for tier 4 and rotation; the step-up key cannot be planted after the enrolment window | hardware P-256 (B) | no user-facing device list for "revoke my laptop" (finding 4) | OD-EP-2; API addition → owner |
| Malicious local process (same user) | none client-side (no client) | no local port except the one-shot login listener; single-instance lock | the loopback code+verifier (§4.6) | IMPLEMENT AFTER RATIFICATION |
| Hostile task output | `AgentResult.response` is data; PRD §24 | sanitized markdown, no remote resources, no script | client rendering tests | — |
| Hostile perceived content | Android: untrusted, bounded, never memory | same ladder, release 4+ | everything desktop-side | FUTURE |
| Update tampering | n/a | signed updater, pinned public key, never the invalid-certificate options | the whole pipeline | OD-EP-7 |
| Credential persistence | Android: key in Keystore; tokens in memory | OS store; on Linux without Secret Service, persist nothing | — | OD-EP-4 |
| IPC abuse | n/a | named-command allowlist, no `fetch`/`run` | conformance plan §2 | OD-EP-1 |
| Fake endpoint profile | no profile exists; the engine reads no self-description (pinned) | profile = routing only | a target-platform check at dispatch (finding 1) | OD-EP-9 |
| Replayed bootstrap code | bootstrap token single-use (conditional UPDATE), 10 min, hashed | one-time code + verifier | the code flow itself | IMPLEMENT AFTER RATIFICATION |
| Wrong-user registration | bootstrap token bound to the `user_id` from a validated id_token; the registration signature binds the token's digest | — | — | — |
| Wrong-device operation | exact `device_id` from the principal (OD-DEV-1); the channel binds token+proof to one device; results accepted only from the addressed device | same | a platform check (finding 1) | — |
| Attempted cross-device execution | refused structurally (pinned §8) | keep (OD-EP-6 recommendation) | — | OD-EP-6 |
| Attempted cross-device confirmation | **allowed for the same user** (OD-A1 continuity); tier 4 needs the approving device's own step-up (pinned); other users get `404` | "defer" | prompt delivery; the owner must reconcile OD-EP-5 with current behaviour | **OD-EP-5 (conflict)** |

No mitigation above alters the canonical security model. Where docs/24 and the code
disagree, the code's current behaviour stands until the owner rules.

---

## 10. Owner decisions still required

All open. None is resolved, ranked or pre-empted by this work.

| ID | Decision | docs/24 recommendation | What the audit adds |
|---|---|---|---|
| OD-EP-1 | Desktop framework | Tauri 2 (alt. Compose MP) | Compose MP could reuse `PresentationState`/`Presenter`, the Kotlin contract mirrors and the task tracker directly; Tauri reuses vectors only (§6). The webview/IPC threats exist only under Tauri/Electron. |
| OD-EP-2 | P-256 device credentials | yes | Option C: a software Ed25519 key sealed under a hardware-held key, as Android already does. It needs no server change. The step-up key is already P-256, so step-up does not depend on this. |
| OD-EP-3 | Desktop STT placement | server STT for release 2 | Server STT is **off** by default under OD-VOI-1; recommending it for desktop means deciding OD-VOI-1 (NEW-3). |
| OD-EP-4 | Linux tier | tier 2, Ubuntu LTS | — |
| OD-EP-5 | Cross-device confirmation | defer; confirm on the originating endpoint | **Conflict:** approval is already cross-device for the same user (§2.1). The choices are (a) keep the current behaviour and leave prompt delivery unbuilt, (b) build prompt delivery, or (c) restrict to the originating session. (c) is a confirmation-semantics change. |
| OD-EP-6 | Cross-device execution | not now | The current refusal is pinned by tests. |
| OD-EP-7 | Signing budget / identity | buy both before the first public release | Appendix A. |
| OD-EP-8 | Windows ARM64 in release 1 | no | — |
| OD-EP-9 | Generic Endpoint model / `endpoint_class` | yes | Needed before any desktop registers. Until then, pinned as absent. |
| NEW-2 | Tier-4 approval from an endpoint with no hardware user-presence key (typically Linux) | — | The existing rule already answers it: no step-up key, no tier-4 approval. Confirm that this is acceptable for desktop. |
| NEW-3 | OD-VOI-1 for desktop | — | See OD-EP-3. |
| NEW-4 | Android handoff | — | Keep the App Link + `app_state` flow (works, tested), or migrate Android to code + verifier for one flow. |
| NEW-5 | Reminder fan-out to desktop endpoints | — | Today every registered device is queued a reminder. Should a desktop get them? |
| NEW-6 | A user-facing list of own devices (`02` addition) | — | Needed for "revoke my other device" (finding 4). |

---

## 11. Exact post-ratification implementation sequence

Each step names its gate. The Real-Data Gate is unchanged throughout.

| # | Step | Gate | Label |
|---|---|---|---|
| 0 | Owner rules on OD-EP-1, -2, -5, -9 at minimum (release 1), and NEW-2/-4/-5 | — | OWNER DECISION REQUIRED |
| 1 | Doc edits authorized by the owner: register rows, `NEXT_BUILD_INDEX.md`, `01`/`02`/`03`/`15`/`16`/`17` per docs/24 §19 | owner | IMPLEMENT AFTER RATIFICATION |
| 2 | Loopback login handoff (§4.6) | docs/24 §9.2 ratified | IMPLEMENT AFTER RATIFICATION |
| 3 | `DevicePlatform` desktop values + `endpoint_class`. Needs a migration: the column is `sa.Enum('android', native_enum=False)`, i.e. `VARCHAR(7)`, too short for `desktop_windows`. Update the §8 pins in the same commit. | OD-EP-9 | IMPLEMENT AFTER RATIFICATION |
| 4 | Target-platform check in Android dispatch (`AndroidDeviceAdapter` / `build_operation` refuses a non-`android` device, `unsupported_platform`), test first | step 3 | IMPLEMENT AFTER RATIFICATION |
| 5 | Channel hello for zero-adapter endpoints; gate by endpoint class, not `android.enabled` alone; reminder fan-out per NEW-5 | step 3, NEW-5 | IMPLEMENT AFTER RATIFICATION |
| 6 | `credential_alg` (if OD-EP-2 = B) | OD-EP-2 | IMPLEMENT AFTER RATIFICATION |
| 7 | User device list (if NEW-6) | NEW-6 | IMPLEMENT AFTER RATIFICATION |
| 8 | `desktop/` project, a repo-boundary test mirroring `test_android_repo_boundary.py`, a CI job with lint/type/test/audit, client conformance (`DESKTOP_CONFORMANCE_PLAN.md`) | OD-EP-1 | IMPLEMENT AFTER RATIFICATION |
| 9 | Desktop release 1 (Windows + macOS): tray, hotkey, panel, text tasks, status (poll), notifications of own results, canonical confirmation, step-up; **zero adapters** | steps 2–5, 8 | IMPLEMENT AFTER RATIFICATION |
| 10 | Signing + updater pipeline (Appendix A) | OD-EP-7 | IMPLEMENT AFTER RATIFICATION |
| 11 | Linux tier-2 build | OD-EP-4 | IMPLEMENT AFTER RATIFICATION |
| 12 | Voice (release 2) | OD-EP-3 + OD-VOI-1 | IMPLEMENT AFTER RATIFICATION |
| 13 | Release 3+: `device.read`, `file.read` in granted folders, then one capability at a time; BR-T2 desktop dimension | separate ratification per capability | FUTURE |
| — | Input injection, desktop shell | not planned (docs/24 §10.2) | FUTURE / never |

---

## 12. Files / commits pushed

Branch `claude/sleepy-carson-l7nc1e`, draft PR harsh-life/JARVIS#31.

| Commit | Files |
|---|---|
| `c5010ec` | `docs/24_ENDPOINT_ARCHITECTURE_DESKTOP_ASSESSMENT.md` |
| `0cf2b1b` | `tests/runtime/test_endpoint_boundaries.py` |
| `11d487c` | `tests/security_core/test_endpoint_preparation.py` |
| (this commit) | `docs/DESKTOP_ENDPOINT_PREPARATION.md`, `docs/DESKTOP_CONFORMANCE_PLAN.md` |

Full suite on the branch with both test files, SQLite, run locally:
`python -m pytest tests/ -q --ignore=tests/memory --ignore=tests/integration/test_br_t2_memory_rows.py`
→ **1859 passed, 7 skipped**. The one excluded module needs the `memory` extra
(numpy), which this environment lacked; CI installs `.[dev,memory]` and runs it,
along with the PostgreSQL job. CI on `c5010ec` (docs only) was green.

---

## 13. Risks / blockers

- **The OD-EP-5 premise conflict** is the most consequential finding. Ratifying
  docs/24 §10.5 as written would silently narrow a behaviour that OD-A1's owner
  decision made intentional.
- **Finding 1 (`Device.platform` unread)** must be closed by step 4 *before* any
  non-Android device can join the channel. Until then no desktop may be
  registered, and none can be: the platform enum admits only `android`, pinned.
- **Characterization pins will fail on ratification.** That is intended: each
  names its decision, and is updated in the same commit as the change.
- **docs/24 is stale in places** (§2.1). The owner may want it revised before
  ratification. This report does not edit it.
- **The Real-Data Gate stays CLOSED.** Desktop work neither opens it nor shortens
  its blocker list. Physical Android validation, the real-integration smoke,
  OD-MB-4, H-2 and OD-JDG-5 remain.
- **The stable public hostname** (`NEXT_BUILD_INDEX.md` §6 item 3) is shared with
  Android and still open.

---

## Appendix A — Distribution, signing and CI plan (plan only; OD-EP-7 open)

Nothing here is configured. No certificate is bought, no key is created, and no
release pipeline is set up.

| Platform | Build targets | Signing | Updater | CI secrets (later) |
|---|---|---|---|---|
| macOS | universal binary (arm64 + x86_64); `.dmg` / `.app` | Developer ID Application certificate; Hardened Runtime; notarization (`notarytool`) + stapling | framework updater, signed artifacts, public key compiled in | Developer ID cert + password; App Store Connect API key for notarization |
| Windows | x64 (ARM64 per OD-EP-8); MSI or NSIS | Authenticode with **one** consistent OV certificate; since 2023 the key is on an HSM/token, so use cloud-HSM signing from CI (per docs/24 §13, Microsoft's managed service is unavailable from India) | same | cloud-HSM credentials (scoped to signing) |
| Linux | `.deb` for Ubuntu LTS first; AppImage optional | updater-artifact signatures; optional distro package signing (GPG) | same (Deb/AppImage) | updater signing key |
| All | from tagged commits in CI only; lockfiles committed; `cargo audit` / `npm audit` (or the Gradle equivalent under Compose MP); SBOM per release | — | never enable "accept invalid certificates"; HTTPS only | updater private key |

**Where secrets may live:** GitHub Actions encrypted secrets on a protected
environment (release tags only, required reviewer), or a cloud HSM/KMS the workflow
reaches via OIDC federation. Never on a developer laptop for release signing.

**Must never be in the repository:** any signing key, certificate password or
`.p12`/`.pfx`; the updater private key; notarization/API credentials; any server
secret (REPO-T2) including the SecretStore KEK, model API keys, the FCM service
account or tunnel credentials; a Google client secret (none exists: the server is a
public PKCE client). A desktop repo-boundary test (step 8) should scan for these the
way `tests/execution/test_android_repo_boundary.py` and
`tests/tools/check_apk_secrets.py` do for Android.

**Existing CI** needs no secrets and should stay that way: a desktop job would
build and test unsigned artifacts, and signing belongs only to a separate,
protected release workflow.

---

*End of report. Preparation only — the desktop client is not implemented, and
docs/24 is not canonical.*
