# 23_ANDROID_CLIENT_PERCEPTION.md
## JARVIS / Hypermind Track B — Android Client: Transport, Execution & Perception

**Package:** next-build subsystem contract · **Depth:** deep · **Written:** 2026-09-24
**Numbering:** `23` is an unreferenced free slot (verified in `26` §1.1). `08` keeps the server-side contract (capability → operation → primitive, two-layer enforcement); this document specifies the client and the channel `08` assumes but never defines.
**Status:** formalizes the owner's **Android decisions** (owner-ratified 2026-09-24): Android is a UI, perception and execution endpoint; never a trust, authorization, capability, or identity root; donor functionality reused where it conforms; old HyperMind routing/authorization **not** restored.
**Authority:** below `00_CANONICAL_PRD.md` and `docs/DECISION_REGISTER.md`. Consumes `03` (OIDC, Ed25519 device credential, step-up), `04`, `07`, `08`, `docs/CAPABILITY_MATRIX.md`.
**Code this plugs into:** `server/execution/android.py` (`DeviceOperation`, `DeviceTransport` Protocol, `UnavailableDeviceTransport`, the primitive mapping table), `server/auth/device.py` (`DeviceService.register/verify_proof/rotate_credential/revoke`, `DeviceProof`), `server/gateway/routers/auth.py` (`/devices/*`), `shared/schemas/` (the only thing server and client share, `16` §4).

**Labels:** `[LOCKED]` · `[OWNER-RATIFIED]` · `[PROPOSED]` · `[IMPL]` · `[FUTURE]` · `[OPEN — OWNER]`.

---

## 0. The rule this document exists to enforce

> **The phone does what an already-authorized server decision tells it, checks that its own user still allows it, and reports what it saw. It decides nothing about authority.**

The JARVIS model, verbatim from the owner: *authenticated device/session → server-derived principal → task → capability authorization → Android adapter → device execution/perception.*

---

## 1. What the client is

| Role | Contents |
|---|---|
| **UI endpoint** | Jetpack Compose app; floating overlay; task input (text, and voice via `27`); task status; confirmation screens; per-app capability grid (PRD §13) |
| **Perception endpoint** | Accessibility service; structured app metadata; on-device ML Kit OCR; screenshot fallback (§6) |
| **Execution endpoint** | Accessibility actions; concrete Android APIs; Shizuku only where an operation requires it (§5) |

What it is **not** `[OWNER-RATIFIED]`: it does not choose models, route intents, hold server secrets, evaluate risk, or decide whether an action is allowed. The old HyperMind on-device router and raw-ADB-token allow-list are **not** restored.

---

## 2. Donor reuse

`[OWNER-RATIFIED]` reuse/adapt from the earlier `hypermind-android` codebase where useful: Compose UI, overlay, Accessibility service, perception ladder, structured semantics, app metadata, ML Kit OCR, screenshot/vision fallback, Shizuku wrapper, system TTS, gateway transport, device tests/conformance vectors.

`[PROPOSED]` adoption rule — donor code is admitted module by module only if it:
1. talks to the server exclusively through the `02` API and the §4 channel;
2. executes only operations present in the shared mapping table (§5.1);
3. contains no model routing, no local authorization logic beyond the device-side guard (§5.2), and no server secret (`[LOCKED]` REPO-T2);
4. passes the conformance vectors (§9).

Anything else is rewritten against this contract. `[OPEN]` the donor repository's Android folder was not readable during this documentation pass; the module-by-module inventory is the first implementation task.

---

## 3. Identity and sessions

- `[LOCKED]` (`03`) Google OIDC login; server derives the user from the verified subject. The device registers an **Ed25519** credential: private key in the **Android Keystore** (hardware-backed where available), public key on the server. Device proofs are signed with it; tokens are short-lived and refreshed with a proof.
- `[PROPOSED]` login handoff: the server's OIDC callback currently returns JSON. The client needs a browser → app return — an **Android App Link** on the server's stable HTTPS hostname carrying the one-time bootstrap token — plus a stable, named tunnel hostname registered as the Google redirect URI. This is a prerequisite, not optional.
- `[LOCKED]` the device never asserts user, graph, or capability facts; the server ignores any it sends (PHONE-003).
- `[LOCKED]` step-up (`03` §5.5) for `high_irreversible` confirmation. `[PROPOSED]` the client obtains step-up by re-authentication (biometric unlock of the Keystore key to sign a fresh re-attestation challenge, or re-login), never by a voice phrase (`27` §3).

---

## 4. The device channel

`[PROPOSED]` implements `DeviceTransport` (`server/execution/android.py`).

**Transport.** An authenticated **WebSocket** from the device to the gateway while the app's foreground service runs, plus a **push wake** (FCM high-priority) that tells a sleeping device to reconnect.

- Connect: TLS to the gateway hostname; authenticate with a current access token **and** a fresh device proof. The server binds the socket to exactly one `(user_id, device_id)`.
- `[LOCKED]` (OD-DEV-1) `send(operation)` delivers to **exactly `operation.device_id`**, never "any connected device of this user."
- `[PROPOSED]` push payloads carry **no operation data and no user content** — only "reconnect." Everything substantive travels over the authenticated socket.

**Message envelope** (server → device):
```
{ op_id, task_id, device_id, capability, operation, primitive, package_name,
  arguments, issued_at, expires_at }
```
- `[PROPOSED]` `expires_at` is short (default 30 s). An expired operation is refused on the device.
- `[PROPOSED]` **no queuing of operations.** If the device is not connected, `send` fails immediately with `device_unavailable` (explicit failure; `02` §13 gets this client state). Authority is not allowed to go stale in a queue. (Reminders *are* queued — they carry no authority, `22` §3.)

**Result envelope** (device → server): `{ op_id, status: ok | refused | failed, refusal_reason?, result, perception_level? }`, size-bounded. `[LOCKED]` results are untrusted data returned to the worker as observations (PRD §24).

**Cancellation.** `{ cancel: op_id | task_id }` from the server; the device aborts any in-flight gesture or read and discards the result. Results arriving after cancel are dropped server-side.

**Code delta** `[PROPOSED]`: `DeviceTransport.is_connected(*, user_id)` becomes `is_connected(*, device_id)`, matching OD-DEV-1's exact-device rule. The current user-scoped signature can report a user's *other* device as connected.

**Revocation.** Device revocation (`/devices/{id}` DELETE) closes the socket and invalidates tokens immediately; the device clears local grant state.

---

## 5. Execution

### 5.1 The shared mapping table

`[PROPOSED]` the capability → operation → primitive table in `server/execution/android.py` is exported as a versioned JSON artifact under `shared/` and bundled into the client build. The device executes an operation only if `(capability, operation, primitive)` is in the table **of the same version** the server used. Version mismatch → refused. One table, two consumers, no drift.

### 5.2 Device-side guard (`08` §4, two-layer enforcement)

Before executing, the device checks independently:
1. the envelope targets this device and is unexpired;
2. the operation is in the shared table;
3. the user's **local per-app grid** still has the matching toggle ON for `package_name` (PRD §13);
4. the arguments are well-formed for the primitive.

Any failure → refused, reported, audited server-side (`[LOCKED]` AND-T5/T6: a toggled-off or malformed operation is refused even if the server sent it). The guard can only refuse; it never widens what the server authorized.

### 5.3 Mechanisms

- `[LOCKED]` (`08` §1) Accessibility actions and concrete Android APIs are the default mechanisms.
- `[OWNER-RATIFIED]` Shizuku is kept for operations that genuinely need it. `[PROPOSED]` Accessibility-first: each Shizuku-backed primitive is listed explicitly in the table; `system.restricted` on Android (`shizuku.elevated_shell`) stays **unexposed** in the next build (`08` §6 `[REC]`). Shizuku requires wireless-debugging re-pairing after reboot — the client must detect a lost Shizuku binding and report `platform_unavailable`, not fail silently.

### 5.4 Confirmation on the device

- `[LOCKED]` confirmation is a server-issued, single-use, action-bound token (`07`, `server/capabilities/confirmation.py`).
- `[PROPOSED]` the confirmation screen renders the action from the **server's canonical action description** (capability, operation, target app, exact arguments) — never from worker prose. The user's approval posts `/agent/tasks/{id}/confirm`; tier 4 additionally requires step-up (§3).

### 5.5 Sensitive apps

`[OPEN — OWNER]` matrix §5.1 is **blocking for device control**: a single `tap` can complete a payment. Until the owner ratifies a sensitive-app classification (which raises every `app.interact`/`device.ui_control` operation on a classified package to at least `consequential`, payment apps to `high_irreversible`), the next build ships **perception and `device.read` first**, and enables UI control only for packages the owner has explicitly classified as non-sensitive.

---

## 6. Perception ladder

`[OWNER-RATIFIED]` order: Accessibility structured semantics → structured app metadata → OCR → screenshot/vision fallback. The goal is natural screen understanding without the user describing UI elements.

| Level | Where it runs | Output | Privacy / tier |
|---|---|---|---|
| 1. Accessibility tree | device | bounded node list: role, text, content-description, bounds, actionable flags | `low_read`. **Password fields and `isPassword` nodes always redacted on device.** |
| 2. App metadata | device | package, activity, window title, notification metadata where the capability covers it | `low_read` |
| 3. OCR (ML Kit, on-device) | device | text blocks with bounds | `low_read`; image never leaves the device |
| 4. Screenshot → vision model | device captures, server-side vision model interprets | description / elements | `[PROPOSED]` a **separate operation** `capture_screenshot` (matrix addition), refused for sensitive-classified packages and for `FLAG_SECURE` windows (Android blocks capture there anyway; the client reports it rather than retrying); image transient, never persisted, never written to memory |

Rules:
- `[PROPOSED]` the device climbs the ladder only as far as needed and reports `perception_level` in the result, so the server and user can see how the screen was read.
- `[LOCKED]` screen content is the principal's data under `04`, transient task context by default, never auto-stored (`08` §8). `[PROPOSED]` excluded from memory extraction like any raw observation (`21` §3).
- `[LOCKED]` screen text is untrusted data, never instructions (a message on screen saying "ignore your rules" is data).

---

## 7. Presentation layer (deferred)

`[OWNER-RATIFIED]` the final character (face, eyes, colours, motion, urgency and device presentation) is **deferred** until server, application and runtime work. It must not block this build.

`[PROPOSED]` the client exposes one internal interface the future character will consume: a stream of `PresentationState {task_status, risk_tier_pending, device_context, break_glass_active, error}` derived from server task status. The next build renders it as a plain status indicator. Replacing the indicator with the character later touches only the presentation module.

---

## 8. Build and CI

- `[LOCKED]` `android/` shares only `shared/schemas/` (and the §5.1 table artifact) with the server (`16` §4); no server secret in the APK (REPO-T2).
- `[PROPOSED]` Kotlin lint (ktlint/detekt) and unit tests in CI from the first commit — the repository currently has no lint or type checks for any language.
- `[PROPOSED]` a server-side **fake transport** in tests implementing `DeviceTransport` against the conformance vectors, so server tests stay device-free.

---

## 9. Testing

- **Conformance vectors** `[PROPOSED]`: a shared JSON suite of `(envelope, local grid state, expected outcome)` cases run by both the server's fake transport and the client's instrumentation tests.
- `[LOCKED]` `08`'s AND-T1..T8 on a real device.
- `[PROPOSED]` instrumentation tests on at least one physical budget-class device for overlay, Accessibility, OCR latency, and reconnection after doze.
- `[LOCKED]` BR-T2 re-run for the Android dimensions once the client exists; the real-data gate stays closed until `08`'s release-blocking set passes.

---

## 10. Open items

| ID | Question | Status |
|---|---|---|
| matrix §5.1 | Sensitive-app classification | `[OPEN — OWNER]`, **blocks UI control** |
| OD-AND-4 | Ratify `capture_screenshot` as a separate operation and its tier | `[OPEN — OWNER]`, rec `low_read` + sensitive/FLAG_SECURE refusal |
| OD-AND-5 | Push provider (FCM) acceptability — a Google dependency for wake-up only | `[OPEN — OWNER]`, rec accept with content-free payloads |
| OD-AND-6 | Donor module inventory | first implementation task |

---

## 11. Acceptance hooks (`[PROPOSED]` IDs, in addition to `08` AND-T*)

- **ANDC-T1** an operation is delivered only to its exact `device_id`; `is_connected` is device-scoped.
- **ANDC-T2** a disconnected device yields immediate `device_unavailable`; no operation is queued.
- **ANDC-T3** an expired or wrong-device envelope is refused on the device.
- **ANDC-T4** a table-version mismatch is refused.
- **ANDC-T5** a toggled-off app refuses on the device even when the server sends the operation (AND-T5).
- **ANDC-T6** password nodes never leave the device.
- **ANDC-T7** `capture_screenshot` is refused for sensitive packages and `FLAG_SECURE` windows; screenshots are never persisted.
- **ANDC-T8** cancel aborts an in-flight operation; late results are discarded.
- **ANDC-T9** push payloads contain no content.
- **ANDC-T10** the confirmation screen text comes from the server's canonical action description.
- **ANDC-T11** the APK contains no server secret (REPO-T2).

---

*End of 23. Next: `27_VOICE.md`.*
