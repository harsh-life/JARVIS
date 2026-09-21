# 03_AUTH_IDENTITY_SESSION.md
## Hypermind Track B — Auth / Identity / Session Implementation Spec

**Package:** subsystem doc 03 of 17 · **Depth:** deep · **Status:** implementation contract
**Authority:** subordinate to `00_CANONICAL_PRD`. Realizes PRD AUTH-001..005, DEVICE-001, SESSION-001..005, and the `02` auth endpoints. Uses `01` entities (User, Device, Session, SecretReference) as-is.
**Consumed by:** every authenticated endpoint (`02`); the authorization engine (`04`) starts from the principal this doc establishes; the SecretStore (`12`) holds the credentials this doc references by handle.

**Label legend:** `[LOCKED]` fixed · `[IMPL]` engineer chooses within the constraint · `[FUTURE]` deferred · `[OPEN — OWNER]` unresolved.

---

## 0. The one distinction this document exists to enforce

**Authentication ("who are you") and authorization ("what may you access") are separate systems** (PRD AUTH-004). This document owns *authentication and session establishment only*. It produces a trustworthy **authenticated principal** (a resolved User + Device + Session). It makes **no** resource-access decisions — those belong to `04`. The moment a valid principal exists, this document's job is done.

Second load-bearing rule (PRD PHONE-003): **the client is never trusted to assert who it is.** Every identity fact the server acts on is derived from a validated credential the server itself issued or a token it can cryptographically verify — never from a `user_id`/`device_id`/`session_id` in a request body.

---

## 1. The three credentials (do not confuse them)

| Credential | Issued by | Lifetime | Stored where | Purpose | Schema |
|---|---|---|---|---|---|
| **OIDC tokens** (Google id_token/access) | Google | short (Google's) | never persisted by Hypermind | prove the human's Google identity *once*, at login | — (validated, discarded) |
| **Device credential** | Hypermind | long-lived, until revoked/rotated (SESSION-001) | Android secure storage (PHONE-004); value in SecretStore server-side by handle | let a known device get access tokens without re-login | `SecretReference` (class `device_credential`), referenced by `Device.credential_ref` |
| **Access token** | Hypermind | short (SESSION-001) | client memory / short store | authorize each API call | `Session` |

`[LOCKED]` These are three distinct things. Google tokens never authorize a Hypermind API call (AUTH-004 — Google is IdP, not Hypermind's authZ). The device credential is not sent on every call — only to refresh an access token. The access token is what rides `Authorization: Bearer` on normal calls (`02` §1.1).

---

## 2. Google OIDC login flow (AUTH-001/002/005)

### 2.1 Full sequence
```
Phone                         Hypermind server                     Google
  │  GET /auth/oidc/start          │                                  │
  │ ─────────────────────────────►│ generate state, nonce, PKCE      │
  │                                │  verifier+challenge; store        │
  │                                │  {state→(nonce, code_verifier)}   │
  │ ◄─── redirect_url ─────────────│  (short TTL, single-use)          │
  │  open browser → Google ────────┼─────────────────────────────────►│
  │                                │                          user consents (identity scopes only)
  │  ◄──────────── redirect w/ code + state ──────────────────────────│
  │  GET /auth/oidc/callback?code&state                                │
  │ ─────────────────────────────►│ validate state (exists,unused)    │
  │                                │ exchange code+code_verifier ─────►│
  │                                │  ◄──── id_token + access_token ───│
  │                                │ VALIDATE id_token (§2.3)          │
  │                                │ map sub → User (§2.4)             │
  │ ◄── {needs_device_registration,│                                  │
  │       bootstrap_token} ────────│  (Google tokens discarded here)   │
```

### 2.2 Scopes `[LOCKED]` (AUTH-002/003)
Request **only** `openid`, `email`, `profile` (or the minimal identity set). **Never** request Gmail/Drive/Calendar/Contacts/Photos scopes — Google login grants zero access to Google APIs (AUTH-003). Any future Google-data access is a *separate* consent flow, not bundled into login.

### 2.3 id_token validation `[LOCKED]` (AUTH-005) — all of these, every login, or reject
1. **Signature** verified against Google's published JWKS (keys fetched + cached, rotated).
2. **Issuer (`iss`)** equals Google's issuer exactly.
3. **Audience (`aud`)** equals Hypermind's OIDC client_id exactly — rejects a token minted for a different app.
4. **Expiry (`exp`)** not passed; **`iat`/`nbf`** sane.
5. **Nonce** in the id_token equals the nonce bound to this `state` (replay defense — an id_token can't be reused across logins).
6. **State** existed, was unused, not expired (CSRF defense); marked used immediately (single-use).
7. **PKCE**: the `code_verifier` stored under this `state` matches the `code_challenge` sent to Google (auth-code interception defense).

`[LOCKED]` Any check failing → `401 unauthenticated` (`02` §1.7), AuditEvent, no session, no user mutation.

### 2.4 Subject → User mapping `[LOCKED]` (AUTH-004/005)
- The stable key is the **`(iss, sub)` pair** — Google's `sub` under Google's issuer. **Never email** (email is mutable; a user can change their Google email and must remain the same Hypermind user).
- Lookup `User` by `(oidc_issuer, oidc_subject)`:
  - **found** → existing user, proceed.
  - **not found** → create a new `User` (`user_id` fresh UUID, `status:active`) — this is account creation.
- `email`/`display_name` from the token are stored as **non-authoritative** profile labels only; they never key anything and can change freely.

### 2.5 Account linking `[FUTURE / OPEN — OWNER]`
Multi-provider linking (same human via Google *and* future Microsoft/Apple → one Hypermind user) is **out of MVP**. The identity-provider interface is kept replaceable (AUTH-001) so a second provider can be added, but MVP is Google-only and one `(iss, sub)` = one user. Cross-provider linking is `OD-AUTH-1` (§9).

---

## 3. Device registration (DEVICE-001)

### 3.1 Flow
```
callback returns bootstrap_token (short-lived, single-use, scope = "register one device")
  │
Phone → POST /api/v1/devices  (Authorization: bootstrap_token)  { platform:"android" }
  │
Server:
  1. validate bootstrap_token (unused, unexpired, bound to the just-authenticated user)
  2. create Device row (device_id, user_id, platform, registered_at)
  3. generate a long-lived device credential (§4)
  4. store credential VALUE in SecretStore → get secret_ref; set Device.credential_ref = secret_ref
  5. return { device_id, device_credential }   ← the raw value, EXACTLY ONCE
  6. mark bootstrap_token used
```
`[LOCKED]` The raw device credential is returned **once** and never again (`02` §3). The phone stores it in Android secure storage (Keystore-backed, PHONE-004). The server keeps only the value-in-SecretStore + the handle; it never returns or logs the raw value again (SECRET-004).

### 3.2 bootstrap_token `[IMPL, constrained]`
A short-lived, single-use token issued by the OIDC callback whose *only* capability is registering one device for the authenticated user. It exists so device registration is authenticated without yet having a device credential. `[IMPL]` its exact form (signed JWT vs. opaque + server store); the constraints (single-use, short TTL, bound to that user, register-only scope) are locked.

---

## 4. Device credential lifecycle (SESSION-001/002)

### 4.1 Properties `[LOCKED]`
The device credential is: **device-specific** (bound to one `device_id`+`user_id`); **long-lived** (no arbitrary expiry — active until revoked/rotated, so users aren't forced to re-login for being idle, SESSION-001); **revocable**; **rotatable**; stored client-side only in Android secure storage; **never in source, never logged** (SESSION-002, SECRET-004).

### 4.2 Form `[IMPL, constrained]`
`[IMPL]` a high-entropy opaque secret (server stores a hash + metadata; compares on presentation) **or** an asymmetric device keypair (device holds private key, server holds public key, device signs a challenge). The asymmetric option is recommended `[REC]` because the server then never stores anything that alone lets it impersonate the device, shrinking blast radius if the server DB leaks (BLAST-002). The *guarantee* — the server can validate the credential and revoke it, and a DB leak doesn't trivially yield working device credentials — is locked; the mechanism is the engineer's choice, resolved alongside `12` (SecretStore).

### 4.3 Rotation `[LOCKED]`
- The device may rotate its credential (proactively, or after a suspected exposure) via an authenticated rotate call.
- Rotation issues a new credential, sets the new `SecretReference` on the Device (`rotated_at` stamped), and invalidates the old one atomically — no window where both work.
- Rotation does **not** require a full Google re-login (it's an authenticated device action).

### 4.4 Revocation `[LOCKED]` (lost/stolen phone — SESSION-005, `02` §3)
- `DELETE /api/v1/devices/{device_id}` by the owning user sets `Device.revoked=true`, `revoked_at`, and invalidates the credential in the SecretStore.
- Effect is immediate: the next refresh attempt with that credential fails (§5.4); existing short access tokens for that device expire on their own short timer (bounded exposure window = access-token TTL).
- **Revocation only helps after the user knows** — this is an accepted, documented residual risk (see §7 stolen-device; and PRD's honest posture on long-lived credentials). Mitigation: short access-token TTL bounds the post-theft window before the stolen credential is even needed for refresh.

---

## 5. Access token lifecycle (SESSION-001)

### 5.1 Issue / refresh
```
Phone → POST /api/v1/sessions/token  { device_credential }
Server:
  1. resolve Device by credential (validate against SecretStore; reject if Device.revoked)
  2. create Session (session_id, device_id, user_id, expires_at = now + short TTL)
  3. issue access_token bound to that session
  4. return { access_token, expires_at }
```
`[LOCKED]` `Session.user_id` MUST equal `Device.user_id` (integrity, `01` §2.3) — never client-asserted.

### 5.2 Access token form `[IMPL, constrained]`
`[IMPL]` signed JWT (stateless, fast) **or** opaque + server session lookup (revocable instantly). Trade-off: a stateless JWT can't be revoked before its (short) expiry; an opaque token can be killed immediately but needs a lookup per call. Given short TTL, either is acceptable; `[REC]` opaque-with-lookup if instant revocation matters more than per-call latency, since the pilot is small. The *guarantee* — the token carries a verifiable, short-lived binding to a valid session/device/user — is locked.

### 5.3 Using it
Every API call (`02` §1.1) sends `Authorization: Bearer <access_token>`. The server validates it and resolves the principal (User+Device+Session) **from the token**, ignoring any identity fields in the body (PHONE-003). Expired → `401 token_expired` → client refreshes (§5.1) → retries.

### 5.4 Refresh failure `[LOCKED]` (SESSION-005, FAIL-013)
Refresh fails if: device revoked, credential rotated (old one presented), credential invalid, or user `status != active`. On failure → `401 unauthorized` with an explicit client state ("please sign in again") — **never a silent hang or a fabricated success** (FAIL-CORE-001). The phone falls back to the full Google login (§2).

### 5.5 Sensitive-operation step-up `[LOCKED]` (SESSION-003)
Some operations require **fresh authentication or re-attestation** even with a valid access token (SESSION-003) — e.g. account deletion (`02` §3), rotating a credential, or (future) a high-risk action. "Fresh" = a recent successful auth event within a short window, or a re-attestation challenge the device signs. `[IMPL]` the exact freshness window and which operations require it (ties `07` risk tiers); the requirement that *some* operations demand step-up is locked.

---

## 6. Logout & session end

- `POST /api/v1/sessions/logout` ends the current session (invalidates the access token / marks the session ended). It does **not** revoke the device credential — the user can get a new token without re-login unless they also revoke the device.
- Full sign-out = logout **+** device revocation (§4.4), used on a shared or retired phone.

---

## 7. Threat handling (SESSION-005 — every case the PRD requires)

| Threat | Handling | Residual exposure |
|---|---|---|
| **Stolen device credential** (SEC-J) | Owner revokes device (§4.4) → immediate invalidation | Until the owner knows + revokes, the thief can refresh tokens. Bounded partly by requiring the credential + a working device; step-up (§5.5) protects sensitive ops. **Accepted, documented residual risk** — the honest cost of "logged in until revoked" UX (SESSION-001). |
| **Stolen access token** (SEC-K) | Short TTL → self-expires; opaque-token option allows instant revoke | ≤ access-token TTL window |
| **Replay of OIDC code/token** | state (single-use) + nonce (bound) + PKCE (§2.3) | Effectively closed for the login flow |
| **Auth-code interception** | PKCE (§2.3) | Closed |
| **CSRF on callback** | state validation (§2.3) | Closed |
| **id_token minted for another app** | audience check (§2.3) | Closed |
| **Forged id_token** | JWKS signature + issuer check (§2.3) | Closed |
| **Server DB leak → impersonation** | Recommended asymmetric device credential (§4.2): server stores only public keys/hashes, not usable secrets | Reduced; interacts with OD-A1 (`14`) |
| **Cross-user session on a device** | `Session.user_id == Device.user_id` enforced (§5.1) | Closed |
| **Idle user forced to re-login** | long-lived device credential refreshes silently (SESSION-001) — this is the UX feature, its cost is the stolen-credential row above | — |

`[LOCKED]` The stolen-device residual risk is not hidden — it is the deliberate, documented trade-off of the SESSION-001 UX choice, mitigated (short access TTL, step-up on sensitive ops, immediate revocation) but not eliminated. Do not present it as fully solved.

---

## 8. What this document produces (the handoff to `04`)

On success, an authenticated request arrives at `04` (authorization) with a **verified principal**:
```
Principal = {
  user_id,          # from token, validated — never client-asserted
  device_id,        # from token; device confirmed not revoked
  session_id,       # from token; session not expired
  active_graph_id?  # session's current graph (nullable; re-checked for membership by 04)
}
```
`[LOCKED]` `04` treats every field here as trustworthy *identity* (this doc validated it) but re-checks *authorization* (membership/visibility/capability) itself — establishing identity is not granting access (§0).

---

## 9. Open items

| ID | Question | Status |
|---|---|---|
| **OD-AUTH-1** | Cross-provider account linking (Google + future Microsoft/Apple → one user) | `[FUTURE]`, non-blocking; interface kept replaceable (AUTH-001) |
| **OD-D1** (from PRD) | Device-credential crypto/key custody (symmetric-hash vs asymmetric-keypair) | resolved jointly with `12_SECRETSTORE.md`; `[REC]` asymmetric (§4.2) |
| OD-AUTH-2 | Access-token form (stateless JWT vs opaque+lookup) | `[IMPL]`, non-blocking; `[REC]` opaque for instant revocation at pilot scale |
| OD-AUTH-3 | Step-up freshness window + exact operation set | `[IMPL]`, ties `07` risk tiers |

---

## 10. Acceptance hooks (for `17`)

- **AUTH-T1** login rejects a tampered id_token (bad signature/issuer/audience/nonce) → `401`, no user created (§2.3).
- **AUTH-T2** replaying a used `state` or a used bootstrap_token fails.
- **AUTH-T3** a user who changes their Google email keeps the same `user_id` (subject-keyed, not email-keyed) (§2.4).
- **AUTH-T4** the raw device credential is returned exactly once and never appears in any later response, log, audit, or usage record (SECRET-004).
- **AUTH-T5** a revoked device cannot refresh an access token (§4.4).
- **AUTH-T6** a rotated credential invalidates the old one with no dual-valid window (§4.3).
- **AUTH-T7** a request whose body asserts a different `user_id` than the token is served as the token's user; the body claim is ignored (PHONE-003).
- **AUTH-T8** an expired access token yields `401 token_expired` and the client refreshes without a full Google login (§5.3/5.4).
- **AUTH-T9** a sensitive operation (e.g. account deletion) requires step-up even with a valid token (§5.5).
- **AUTH-T10** `Session.user_id != Device.user_id` is impossible to create (§5.1).

---

*End of 03_AUTH_IDENTITY_SESSION. Next: `04_AUTHORIZATION_GRAPH_RESOURCE.md` (DEEPEST) — the five-dimension resource-authorization engine that consumes the principal this document produces. Changes here propagate to 02, 04, 12, 17.*
