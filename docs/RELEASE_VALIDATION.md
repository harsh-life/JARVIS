# Release validation — Phase H (final hardening)

**Date:** 2026-09-27 · **Base:** `main` @ `0eebbaf` (Phase G merged) · **Branch:** `claude/optimistic-cray-g5yepm`
**Authority:** `17_TEST_ACCEPTANCE_VALIDATION.md` §0/§5 ("a requirement is met only when its test passes
with recorded evidence"), `14_SECURITY_BLAST_RADIUS.md`, PRD §39 (the 32 criteria, RB subset) and §31/§38
(the OD-A1 gate). This document is the evidence record. It is **not** a claim that the system is safe
for real data. §11 states the release-gate status from the evidence, and the gate is not open.

Phase H added no product feature. It audited the integrated tree, added the evidence the release
definition asks for and did not have, fixed one defect, and measured the Android BR-T2 dimension.

---

## 1. Starting check (H0)

| Check | Result |
|---|---|
| Branch based on latest `main` | yes — reset to `0eebbaf`; no uncommitted work |
| `main` CI | **green** (run 63: `checks` and `android`) |
| Migration head | single head `b6d4f8a2c1e7`; 11 revisions; `upgrade → downgrade base → upgrade` clean; `alembic check` reports no drift |
| Baseline tests | server 1468 passed / 7 skipped; memory 192 passed / 0 skipped; import contracts 21/21; Android `:contract` 54, `:app` 206 |
| Skips | all 7 are one parametrized conformance case: vectors that describe envelopes the server cannot mint (wrong device, expired, …). Those vectors are exercised by the Python reference guard and the Kotlin `DeviceGuard`. They are not silent gaps. |

## 2. Test matrix (final, exact)

From the clean-environment run (§12) on `45612a6`. The code tree is identical to the PR head
`0d63065`, a merge commit whose only change is `main`'s merge of H1. CI results are in §13.

| Suite | Result |
|---|---|
| Server (`pytest tests/ --ignore=tests/memory`, `HYPERMIND_REQUIRE_MEMORY_STACK=1`) | **1533 passed, 7 skipped**, 0 failed. The 7 skips are the conformance vectors of §1. |
| Memory release-blocking (`pytest tests/memory`) | **196 passed**, 0 skipped |
| BR-T2 (4 modules, `-s`) | **8 passed**; 41 rows printed (11 + 10 + 11 + 9), 14 REACHABLE, each as documented in `docs/OD_A1_BR_T2.md` |
| Import contracts (`lint-imports`) | **21 kept, 0 broken** |
| Migrations | `upgrade head → downgrade base → upgrade head` clean; `alembic check`: no drift |
| Android `:contract` | **54 tests**, 0 failures, 0 skipped |
| Android `:app` (Robolectric) | **206 tests**, 0 failures, 0 skipped |
| ktlint + detekt | pass |
| Android lint | **0 errors** (warnings only) |
| `assembleDebug` + `assembleDebugAndroidTest` | pass |
| APK secret scan (ANDC-T11) | `app-debug.apk: no server secret found` |
| Repository secret scan | tracked files: 0 findings (CI test); git history: 1,085 blobs, 0 real credentials (§7) |
| Guard mutation check | **27/27 killed** (§6) |

Phase H's additions to these counts:
- server tests: +65 over the H0 baseline of 1468. These are the platform-wait, fs-race, hub-routing,
  secret-scan, mutation-meta and Android BR-T2 tests, plus the strengthened SS-T2.
- memory tests: +4 (pilot concurrency).

## 3. The core invariant, traced

`worker/model proposes → deterministic authorization → bounded execution → observation returned as
untrusted data → human confirmation where required`. Each component that could have become an
authority, and why it is not one:

| Could it authorize? | Why not (evidence) |
|---|---|
| The model | Proposals are parsed strictly (`extra="forbid"`): no tier, disposition, confirmed flag, principal, graph or confinement field (`server/agent/proposals.py`; BG-T4). The runtime cannot import the engine (import contract; INV-8). |
| Android | The phone is a second *refusing* layer, never a granting one. It holds no server secret (APK scan, repo scan). Identity is server-derived; a platform report cannot carry a `device_id`. Mutation M24–M26 prove the device guard's refusals are tested. |
| Voice | A transcript only fills the task box. `SpeakerContext.is_authorization_signal` is `Literal[False]` plus a DB CHECK. No voice field on submit or confirm (strict bodies). Mutation M22. |
| Scheduler | Cannot import runtime, tools, devices, authority, secrets, memory or judge (two import contracts). Firing delivers a message only. Mutation M23. |
| Judge | J1–J2 only (PR #23, merged during Phase H): contract, TaskTrace, providers, metering and target registry; `evaluation.enabled: false` by default; **not wired** into the runtime or the composition root. The import contracts "The Judge is never an authority", "The runtime never depends on the Judge" and "The Judge opens no process or socket" hold. Phase H audited its boundary only; the Judge's behaviour is its own phase's work. |
| Memory | Hydrated facts are data in context; memory cannot import control or device code. Hydration re-checks `readable()` (mutation M19). |
| Push receipt | The payload is exactly `{"type":"wake"}` (ANDC-T9, mutation M21). A wake resumes nothing; only an authenticated channel connect does (`test_a_failed_authentication_resumes_nothing`). |
| Client / local UI state | The server re-derives the principal on every request (AUTH-T7). Presentation code cannot import privileged, execution or credential code (`PresentationBoundaryTest`). The overlay has no approve path. |

## 4. Area audits

Every area of the brief (§3 A–Z) was audited against its contract. Existing evidence is cited; what
Phase H added is marked **H**.

- **Auth / identity (03):** OIDC claim checks, state/nonce/PKCE and bootstrap replay (AUTH-T1/T2), subject-keyed identity (T3), credential shown once (T4), revocation incl. live tokens (T5), rotation with no dual-valid window (T6), body-asserted identity ignored (T7), token expiry (T8), step-up for rotation (T9), `session.user == device.user` (T10), proof replay / staleness / forgery / cross-user proof. The device channel refuses another user's token carrying this device and another device's proof, and re-auth cannot rebind. Step-up: single-use challenge, expiry, wrong key or message, device-bound challenge, key planting only at enrolment. Mutation M06/M07.
- **Authorization / capability chain (04/07):** AZ-T1..T12, the floor enforced four times, capability activation per task, the sensitive-app gate (M15), mode ceiling (M18), recovery preserving authority (worker-chain tests). Mutation M01–M03, M09.
- **Confirmation / step-up:** token bound to principal, session, task, capability, operation, resource type, resource and argument hash. Single use under a conditional UPDATE. Expiry never approves. Only the hash is stored, and the idempotency replay copy drops it. Mutation M04/M05. **H:** an approval spent before a platform wait is refused after it (409) — `test_an_approval_spent_before_the_wait_is_refused_after_it`.
- **Break-glass (20, OD-EXEC-2 ratified form):** BG-T1..T10 all present, incl. superuser-only activation (M17), separate executable list, invocation/time/task/revoke/breaker end, confirmation + step-up still required, cancel/stop kill the unconfined group, failed audit ⇒ never live. No generic shell: `system.restricted` is argv-only with an empty allow-list by default.
- **Confinement / process:** Landlock + seccomp per child, fail-closed where unavailable (local host: ABI 7).
- **Memory / Mem0 / Vault:** §5.
- **Android:** §8. **Shizuku:** one typed AIDL call (`forceStopPackage`, re-validated, protected-package denylist), on demand, no shell or argv. **H:** repeated availability reports resume exactly once; a device revoked during a wait never resumes (fails `principal_revoked`); stale approval refused after the wait (above).
- **Push / scheduler:** push is wake-only (ANDC-T9); an operation to an offline device fails immediately and is never queued (ANDC-T2); duplicate wakes coalesce; a revoked device has its token cleared and is never woken; reminders go to the owner's devices only, are re-checked at send, and are queued for offline devices (they carry no authority); firing never reaches runtime/tools/devices (SCH-T2 static + runtime).
- **Voice:** device STT/TTS by default; server providers declared-origin, no redirects, key by handle (class `model_api_key` only), metered; audio never written or logged (VOI-T2 audit hook); VOI-T1..T5 present.
- **Configuration / migrations / CI:** §9.

## 5. Memory release-blocking validation

MEM-T1 and MP-T1..T12 re-run on the real Mem0 + Chroma store (192 tests, 0 skipped with
`HYPERMIND_REQUIRE_MEMORY_STACK=1`). **H:** PRD #22 requires isolation "verified by a
concurrency/multi-tenant test, not assumed". The existing suites drove users one at a time.
`tests/memory/test_pilot_concurrency.py` now drives **ten users on ten devices in one shared graph
concurrently**. Concurrent writes, shares, listings, targeted searches and agent hydration never
surface another user's private fact, and task ids are unreadable across users. At-rest residuals are
not over-claimed: live facts are plaintext at rest, and deleted facts' embedding vectors persist until
an index rebuild (BR-T2 §3c rows 27–28, OD-MB-4, still open). Deletion removes fact *text* from the
store files (row 26); nothing more is claimed.

## 6. Guard mutation check (§21)

`tests/tools/guard_mutations.py` disables one deterministic guard at a time with one exact source edit,
runs that guard's defending tests, and requires them to **fail**. "Killed" means the defending tests
failed. A collection or compile error never counts as a kill. The file is restored byte-for-byte.
`tests/foundation/test_guard_mutations_meta.py` keeps every mutant anchored in CI.

**Last full run: 27 / 27 killed.**

| Mutant | Guard disabled |
|---|---|
| M01, M02, M03 | AuthZ D4 visibility · D4 graph scope · D1 membership |
| M04, M05 | Confirmation token binding: arguments · session |
| M06, M07 | Step-up: signature · challenge expiry |
| M08 | Exact-device routing |
| M09 | Absolute floor |
| M10 | SecretStore unconditional agent denial |
| M11 | Secret filter |
| M12, M13 | Egress: metadata · loopback |
| M14, M27 | Sandbox leaf: read/listing TOCTOU · write |
| M15 | Sensitive-app gate (resource scope) |
| M16 | Principal freshness (revoked device) |
| M17 | Break-glass superuser |
| M18 | Task-mode ceiling |
| M19 | Hydration `readable()` re-check |
| M20 | Per-user idempotency namespace |
| M21 | Push payload content-free |
| M22 | Voice never an authorization signal |
| M23 | Scheduler cannot reach the runtime |
| M24, M25, M26 | Kotlin `DeviceGuard`: grid toggle · expiry · wrong device |

**What the first run found.** Three guards survived:
- **M08:** routing to "any connected device" passed because the target device happened to be
  attached first.
- **M10:** a later scope check also refused the agent, so the unconditional first denial was never
  pinned.
- **M14:** the "swapped symlink" test swapped *before* the call, where earlier checks catch it.

Each now has a test that fails without its guard. M14's investigation found the defect in §7.

## 7. Defect fixed, and secrets / egress / supply chain

**Defect (fixed, `server/fs/sandbox.py`).** A symlink swapped in at a sandbox leaf between the last
type check and the `open` was refused by `O_NOFOLLOW`, so containment held. But the refusal escaped as
a raw `OSError` (ELOOP), which failed the whole task as an internal error instead of returning a typed
`FORBIDDEN_PATH` observation. The write path already mapped ELOOP correctly. Every leaf open now goes
through `_open_leaf`. Race tests inject the swap *inside* the window; they fail on the old code and
pass on the fix.

**Secrets.**
- **H:** `tests/foundation/test_repo_secret_scan.py` scans every git-tracked file (516) with the
  server's secret-pattern set plus Groq and FCM formats. Only lines marked `TEST-ONLY` and two named
  placeholder lines are allowed, and a planted-key self-test proves the scan detects.
- All **1,085 blobs in git history** were also scanned. The only hits were two self-described
  fixtures (`…MIIE-TEST-ONLY`, and a placeholder replaced in `97b7060`). No real credential.
- The APK scan (ANDC-T11) runs in CI on the built APK.
- Secrets never enter audit, usage, idempotency storage (the confirmation token is nulled), memory
  (write gate, MP-T5) or model context (handle-only; the agent is refused, M10).

**Outbound network paths.**

| Path | Destination | Controls |
|---|---|---|
| OIDC | Google's fixed endpoints | httpx, redirects off (default), 10 s timeout |
| Model providers | the operator-configured base URL only | httpx, redirects off, per-call timeout, budget-checked, metered |
| `net.request` tool | operator-declared destinations | `mediated_proxy`: resolve → classify → connect to the checked IP; metadata/loopback always blocked; redirects re-validated; size/time caps |
| Voice providers | declared origin | `follow_redirects=False`, key by handle, metered |
| FCM wake | Google's fixed token endpoint + FCM send | `follow_redirects=False`; a credential naming another token endpoint is never followed |
| Mem0 / embedder | none at runtime | telemetry forced off (refuses to start otherwise); model provisioned once, then offline (MP-T8 egress audit hook) |
| `system.restricted` children | none | Landlock TCP rules + seccomp `socket()` |
| Android | the user's server; Google sign-in in the browser; FCM only if the server offers it *and* the user opts in (Firebase otherwise never initialized); ML Kit OCR bundled on-device | ML Kit / Firebase SDK data-transport telemetry at runtime is **not validated** (needs a device network capture, §9) |

In-process code is not bound by `mediated_proxy` (BR-T2 row 14, inside OD-A1 (a)).

## 8. Android final audit

- **Layers:**
  - Auth: Keystore Ed25519 device key, P-256 step-up key.
  - Channel: exact-device, no queue, expiry, cancel, revocation.
  - Execution: typed primitives only, with Accessibility selectors and never raw coordinates.
  - Perception: password nodes redacted on device; OCR on device; screenshot refused for FLAG_SECURE
    and sensitive apps, never persisted.
  - Presentation: shows, never decides.
- Every item on §11's list is backed by the shared conformance vectors (both sides), the Robolectric/
  JVM suites (`:contract` 54, `:app` 206), the server contract tests, and the source-scanning boundary
  tests: no old Groq keys or bearer tokens, no old allowlist, no generic ADB or Shizuku shell,
  mapping-version parity, content-free FCM, push cannot execute, no queued operations, fresh
  authorization after platform recovery, voice cannot confirm, overlay cannot approve, UI cannot reach
  privileged APIs.
- **BR-T2 Android dimension (H):** measured, `docs/OD_A1_BR_T2.md` §3d rows 29–37.
- **Hardware:** §9.

## 9. Hardware validation outstanding

**No physical Android phone and no emulator was available at any point.** Everything Android was
compiled, unit-tested on the JVM or under Robolectric, and exercised through fakes and the shared
conformance vectors. None of the following has been validated on hardware:

| Not validated on a device | Where it is tested instead |
|---|---|
| Real FCM delivery and background wake from doze | `FcmWakeSender` at the HTTP transport; `WakeHandler`/`PushRegistrar` unit tests |
| Microphone input and on-device STT (`SpeechRecognizer`, API 31+ on-device) | `SpeechInput` state machine against a fake recognizer |
| TTS output | `Speaker` against a fake engine |
| Accessibility execution (tree read, redaction, tap/scroll/type) | Robolectric + fakes; perception samples shared with the server |
| ML Kit OCR latency and accuracy | fake OCR engine |
| Shizuku binding, re-pairing after reboot, `forceStopPackage` | fake binder; the AIDL surface itself |
| Screenshot capture, FLAG_SECURE refusal | fake capture |
| Keystore hardware backing, biometric prompt for step-up | JVM keys; the server verifies real P-256 signatures |
| Floating overlay (`SYSTEM_ALERT_WINDOW`, never focusable), tapjacking filter | unit tests of policy and flags |
| App Link login return, foreground-service lifetime | unit tests, manifest checks |
| SDK network behaviour (ML Kit / Firebase data-transport) | not tested |
| BR-T2 §3d rows 30–32 against the Kotlin guard on a phone | reference guard on the JVM + Kotlin guard unit and mutation tests |

docs/23 §9 `[LOCKED]` requires AND-T1..T8 on a real device and instrumentation tests on a budget-class
phone. **That requirement is unmet.**

## 10. Owner-open decisions (consolidated)

Labels: **LOCKED** (canonical) · **OWNER-RATIFIED** · **PROPOSED** · **IMPLEMENTED** (in code) ·
**OPEN** (owner decides) · **NOT VALIDATED**. "Implemented recommendation" never means "owner
decision ratified".

| ID | Question | In code | Status |
|---|---|---|---|
| OD-A1 | Cross-user isolation under app RCE | logical isolation; residual measured (§3–§3d) | **OWNER-RATIFIED** (a) for the pilot — new at-rest rows below are *outside* that acceptance |
| OD-MB-4 | Memory at rest: plaintext live facts; deleted vectors kept until rebuild (BR-T2 27–28) | as described | **OPEN** |
| H-2 | Push registration token at rest (BR-T2 36) | plaintext; cleared on revoke | **OPEN** (new, low severity) |
| H-1 | PRD #32 fairness on a single-writer store | SQLite, 503 on contention | **OPEN** (new; multi-writer store or runtime transaction redesign) |
| matrix §5.1 | Sensitive-app classification lists | mechanism IMPLEMENTED; lists empty ⇒ no UI control | **OPEN** |
| OD-AND-4 | `capture_screenshot` as its own op, tier `low_read` | implemented recommendation | **OPEN** |
| OD-AND-5 | FCM as a wake provider | optional, off by default | **OPEN** |
| OD-AND-2/3 | Shizuku required? `system.restricted` on Android? | Accessibility-first; one typed Shizuku call; not exposed | implemented recommendation · **OPEN** |
| OD-VOI-1 | Any server STT at pilot | none by default | **OPEN** |
| OD-VOI-2 | Wake word / always listening | not built | FUTURE |
| VOI-B3 | Opt-in raw-audio retention (LIFE-002) | not built (`audio_retained` always false) | **OPEN** |
| OD-SCH-1 | `scheduler.create` name and `low_write` | implemented as proposed | **OPEN** |
| OD-SCH-2 | Fire-time `suggest` task | not built | **OPEN** |
| OD-SCH-3 | Scheduled unattended execution | not expressible | FUTURE |
| OD-EXEC-2 / OD-BG-1 | Break-glass (task-bound form); transcribe into the register | code implements the ratified form | **OWNER-RATIFIED** (docs/20); transcription **OPEN** (owner to authorize) |
| OD-BG-2 / OD-BG-3 | Disposable-host dev switch · break-glass beyond `system.restricted` | neither built | **OPEN** |
| OD-SUP-1 / OD-SUP-3 | Emergency-stop split · escalate to a paid worker | implemented recommendation · default no | **OPEN** |
| OD-JDG-1..4 | Judge: abstraction, budget, provider, usage kind | J1–J2 contract merged (PR #23), off, not wired | **OPEN** |
| OD-DASH-1 | Read-only dashboard + separate control endpoints | control endpoints exist; dashboard not built | **OPEN** |
| OD-DP-9 | Ratify DecisionProvider | not built | **OPEN** |
| OD-MEM-A / OD-MEM-B | PRD §41 reading · extraction mechanism (a) | implemented recommendation | **OPEN** |
| OD-VLT-1 / OD-AUTHZ-1 | Vault write path (Git only) · shared facts on leave (stay) | implemented recommendation | **OPEN** |
| OD-TOOL-1 | Per-operation tier table signature | semantic level ratified; tiers PROPOSED | **OPEN** (owner signs) |
| OD-TOOL-3 | MCP servers at pilot | none | **OPEN** (default none) |
| OD-MT-2 / C1 | Remote vs local default model | local by default in config; budgets 0.0 refuse paid calls | **OPEN** (conflict with LOCKED PRD §19 recorded in NEXT_BUILD_INDEX §4) |
| OD-02, OD-USE-1..3, OD-RT-1..3 | Numeric bounds, limits, precedence | PROPOSED values in config | **OPEN** (owner sets) |

## 11. Release-gate status (17 §5, PRD §31/§38)

17 §5: "safe to run with real user data only when **all RB tests pass** AND the OD-A1 gate is decided."
Status from the evidence, not a judgement:

| Gate element | Evidence | Status |
|---|---|---|
| RB tests of 17 §2 (auth, authz, runtime, secrets, fs/net, memory, config/repo) | present and green in CI; mutation-checked (§6) | **green**, with the notes below |
| FS-T9 / NET-T2 "compromised tool contained" | holds for out-of-process tool code (`system.restricted` children: Landlock + seccomp). In-process adapters are `mediated` / `mediated_proxy` (BR-T2 13–14, inside OD-A1 (a)). No third-party tool code runs in-process (no MCP) | green for the tools that exist; kernel-level fs/net isolation for in-process code stays future hardening |
| MEM-T1 on the real store, incl. concurrency (#22) | §5 | **green** |
| Android release-blocking set (AND-T1..T8 on a device, docs/23 §9) | JVM / Robolectric / fakes only | **not met** (no hardware, §9) |
| PRD #29 dashboard secret-free / PII-redacted | no dashboard exists | **no evidence** (not built) |
| PRD #32 ~10-device fairness | measured: isolation holds; writers serialize | **not met** on SQLite (H-1) |
| PRD #28 fresh clone on the cloner's own credentials | §12: a fresh clone, venv and model provisioning pass every suite with no owner credential | **green** for the test/build path. End-to-end self-host (real OIDC client, tunnel, phone pairing) was not exercised |
| BR-T2 run and reviewed | every software dimension measured, incl. Android (§3d); OD-A1 (a) decided | measured. New at-rest rows 27–28 and 36 await owner review |
| OD-A1 | owner decision (a), 2026-09-22 | **decided** for the in-process class |

**The gate is not open.** Real user data is blocked, independently, by:
1. the Android release-blocking set not validated on hardware;
2. RB #29 without evidence (no dashboard);
3. RB #32 not met on the single-writer store;
4. at-rest BR-T2 rows awaiting owner decisions (OD-MB-4, H-2).

Disposable or test data only.

## 12. Clean-environment reproduction

Done on this host in a separate directory, with nothing shared from the working tree:

1. `git clone` of the pushed branch (`45612a6`) into an empty directory. The clone has no `data/`, no
   models and no database.
2. A new `python3 -m venv`, and `pip install -e ".[dev,memory]"` from `pyproject.toml` only
   (122 packages).
3. `python -m server.memory provision --config config.example.yaml`: the embedding model downloaded
   fresh from the Hugging Face Hub (65 MB). This is the one sanctioned download (15 §3 step 5).
4. `lint-imports`, the migrations round-trip plus `alembic check`, the server suite, the memory suite
   and the four BR-T2 modules. Results in §2.
5. The Android CI sequence in the clone, online (no `--offline`):
   - ktlint/detekt, `:contract:test`, `:app:testDebugUnitTest`,
   - `assembleDebug`, `assembleDebugAndroidTest`, `lintDebug`,
   - the APK scan.

What was **not** clean, stated rather than hidden:
- The Android build shared this host's Gradle cache (`~/.gradle`). Some tasks were restored from the
  build cache: 19 of 35 in the test run, 23 of 80 in the build run. CI's fresh runner (§13) is the
  cold-build evidence.
- The Android SDK was the host's (`/opt/android-sdk`).
- No test used any owner credential. The suites generate their own KEK and throwaway config (17 §6).
  PRD #28 ("a fresh clone runs on the cloner's own credentials") therefore holds for the test and
  build path.
- An end-to-end self-host was **not** exercised: a real Google OIDC client, a tunnel, and a real
  phone pairing. It needs external accounts and hardware that were not available.

## 13. CI

CI (`.github/workflows/ci.yml`: `checks` and `android`) runs on every push to the PR. The result on
the final commit is reported in the PR and in the Phase H final report. Earlier heads:
- `main` @ `0eebbaf`: green (run 63).
- PR harsh-life/JARVIS#24 head `9a0165c` (H1, merged): green (run 65).
