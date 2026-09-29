# Desktop Conformance Plan

**Status:** `PREPARATION — PROPOSAL`. This plan lists the conformance cases a desktop
endpoint must pass *if* the owner ratifies docs/24. It does not ratify docs/24,
choose a framework (OD-EP-1), or add any code. Test IDs `DCT-*` are provisional.
Companion to `docs/DESKTOP_ENDPOINT_PREPARATION.md`.

**The rule every case serves** (docs/24 §1, docs/23 §0): the endpoint does what an
already-authorized server decision tells it, and reports what it saw. It never
decides authority, holds a server secret, or asserts identity.

---

## 1. Server-side cases: already in the repository

These run in CI today. A desktop build inherits them. Its job is to not need any of
them relaxed.

| Case | Test |
|---|---|
| Identity derived from the token, never the body | `tests/security_core/test_auth_identity_session.py::test_auth_t7_*`; `test_endpoint_preparation.py::test_client_frames_cannot_carry_identity_authority_or_profile` |
| Engine never reads an endpoint's self-description | `test_endpoint_preparation.py::test_the_authorization_engine_never_reads_what_an_endpoint_says_about_itself` |
| No open redirect; caller cannot steer the login result | `test_endpoint_preparation.py::test_the_caller_cannot_choose_where_the_login_result_goes` |
| Bootstrap token is register-only, single-use | `test_endpoint_preparation.py::test_a_bootstrap_token_is_not_an_access_token`; `test_auth_identity_session.py::test_auth_t2_replaying_a_used_bootstrap_token_fails` |
| Device-held key; server never holds the private half | `tests/security_core/test_device_public_key.py` |
| Proofs: replay, staleness, forgery, another user's device | `test_auth_identity_session.py::test_device_proof_cannot_be_replayed`, `::test_stale_device_proof_is_rejected`, `::test_forged_device_proof_is_rejected`, `::test_a_proof_for_another_users_device_does_not_grant_their_identity` |
| Operation only to the authorizing endpoint | `tests/runtime/test_endpoint_boundaries.py::test_an_operation_never_goes_to_the_users_other_connected_endpoint`, `::test_each_endpoint_receives_only_the_operations_it_authorized` |
| Tier 4 needs the approving endpoint's own step-up | `test_endpoint_boundaries.py::test_a_tier4_approval_from_another_endpoint_needs_that_endpoints_own_step_up` |
| Another user cannot see or confirm my task | `tests/runtime/test_authorization_and_capabilities.py::test_another_user_cannot_see_or_confirm_my_task` |
| Channel binds token + proof to one device; revocation closes it | `tests/security_core/test_device_channel.py` |

**Pins that change on ratification.** These will fail by design and are updated in
the same commit as the ratified change: `test_the_registry_has_exactly_one_device_platform`,
`test_an_unregistered_platform_is_refused_without_spending_the_login[desktop_*]`,
`test_registration_carries_no_profile_or_identity_claim[endpoint_class|credential_alg]`,
`test_the_caller_cannot_choose_where_the_login_result_goes` (once a loopback
`return_to` exists, split it into "allowlisted loopback accepted" and "everything
else refused").

---

## 2. Client-side cases (to write with the desktop client)

Cases marked † apply only to a web-UI framework (Tauri, Electron). Under Compose
Multiplatform the UI and the credential share one process, so the equivalent
check becomes "no UI code path reads the key store".

### 2.1 Credential and key custody

| ID | Case | Pass condition |
|---|---|---|
| DCT-1 | The device key is generated on the endpoint and never exported | registration sends only `public_key` + `key_proof`; no private-key bytes in any request, log, crash report or file outside the OS store |
| DCT-2 | Access tokens are memory-only | after a clean exit, no token is found on disk (scan app data, logs, caches) |
| DCT-3 | No Secret Service on Linux → nothing persisted (docs/24 §8) | the client runs session-only with a visible notice; no key file, no plaintext fallback |
| DCT-4† | The webview never receives a token, proof, key or bootstrap value | instrument IPC: no message to the UI contains one; the UI's JS heap snapshot contains none after login |
| DCT-5 | The client binary contains no server secret (REPO-T2) | a scanner like `tests/tools/check_apk_secrets.py`, run on every built artifact |

### 2.2 IPC and local surface

| ID | Case | Pass condition |
|---|---|---|
| DCT-6† | IPC is a closed allowlist of named commands | enumerating the registered commands gives exactly the documented set; there is no `fetch`, `http`, `run`, `exec`, `shell`, `open_url` with an arbitrary target, or `eval` |
| DCT-7† | The webview has no network route to the server | CSP `connect-src` excludes the server origin; a request from the UI context fails |
| DCT-8† | Rendered task output is inert | a task response containing `<script>`, `<img src=http…>`, `javascript:` links or markdown with remote images renders as text; no network request results |
| DCT-9 | No listening port except the one-shot login listener | during steady state, `netstat`/`lsof` shows no listening socket owned by the client |
| DCT-10 | The login listener is one-shot and loopback-only | it binds `127.0.0.1` (not `0.0.0.0`), accepts exactly one request on `/cb`, then closes; a second request is refused |
| DCT-11 | Single instance via the OS per-user mechanism, not a TCP port | a second launch hands off to the first without opening a socket |

### 2.3 Login handoff (after §4.6 of the report is ratified and built)

| ID | Case | Pass condition |
|---|---|---|
| DCT-12 | Login opens the **system** browser, never an embedded webview (RFC 8252 §8.12) | the OIDC URL is passed to the OS URL handler |
| DCT-13 | The client accepts only the code for the login it started | a `/cb` hit with a mismatched client `state` is ignored and audited locally |
| DCT-14 | The verifier never leaves the client except in the redeem call | not in the start URL, the listener response or any log |

### 2.4 Tasks, confirmation and step-up

| ID | Case | Pass condition |
|---|---|---|
| DCT-15 | Confirmation UI is built only from `PendingAction` | the card's text is a pure function of `capability`, `operation`, `arguments`, `resource_ref`, `risk_category`, `requires_step_up`, `expires_at`; `response` and any model text never appear on it (the ANDC-T10 analogue) |
| DCT-16 | A notification never approves | notification actions open the confirmation screen; there is no approve action on a notification, tray menu or global hotkey |
| DCT-17 | The floating panel never approves | the panel shows state only; approval exists only on the confirmation screen |
| DCT-18 | Tier 4 triggers step-up before `/confirm` | with `requires_step_up`, the client performs the OS user-presence prompt and `/sessions/step-up` first; a voice input never satisfies it (docs/27) |
| DCT-19 | Retries reuse the Idempotency-Key | a resubmitted unanswered task carries the same key; a new task never does |
| DCT-20 | Unknown server values never render as success | an unrecognised `status` maps to an error state (the `Presenter` rule, docs/23 §7) |

### 2.5 Updates and configuration

| ID | Case | Pass condition |
|---|---|---|
| DCT-21 | An unsigned or wrongly signed update is refused | a tampered artifact is rejected; the public key is compiled in |
| DCT-22 | A release build has no "ignore certificate" path | no configuration or flag disables TLS verification; a self-signed server fails |
| DCT-23 | Switching servers wipes the local credential (docs/24 §15) | after a base-URL change, no key or token for the old server remains |

### 2.6 Execution (release 3+ only; do not build for release 1)

| ID | Case | Pass condition |
|---|---|---|
| DCT-24 | Release 1 has zero adapters | the channel hello (once generalized) declares no adapter table; any operation frame is refused `not_in_mapping` |
| DCT-25 | A non-`android` device is never sent an Android operation | server-side, added with step 4 of the report's §11 |
| DCT-26 | Device-side folder containment (docs/24 §10.2) | traversal, symlinks, Windows junctions and macOS aliases escaping a granted folder are refused, mirroring `09`'s tests |

---

## 3. Shared vectors a desktop client should consume

`shared/android/proof_vectors.json` (proof and registration signatures),
`task_samples.json` (the `AgentResult` shapes), `reminder_samples.json`, and
`voice_samples.json`. They are platform-neutral despite the directory name. Moving
them is a repo-layout change (report §5). The Android-only vectors
(`device_mapping.json`, `conformance_vectors.json`, `grant_samples.json`,
`perception_samples.json`, `push_samples.json`) are not for release 1.

---

*Proposal only. No case here is a release requirement until the owner ratifies
docs/24.*
