# 12_SECRETSTORE.md
## Hypermind Track B — SecretStore

**Package:** subsystem doc 12 of 17 · **Depth:** DEEPEST · **Status:** implementation contract
**Authority:** subordinate to `00_CANONICAL_PRD`. Realizes SECRET-001..005, SUPER-001, and resolves **OD-D1** (key custody). Uses `SecretReference` (`01` §8) — the handle; the *value* lives only here. Referenced by model adapters (`06`), tools (`07`), device credentials (`03`).
**Consumed by:** everything that needs a credential resolves it here by handle; `14` validates its blast-radius properties; `17` tests it.

**Why DEEPEST:** the SecretStore is where a mistake becomes total credential compromise. Its whole job is to ensure that neither the agent, nor a tool, nor a compromised application process, nor a leaked DB trivially yields working secrets. Every rule `[LOCKED]` unless marked.

---

## 0. The three rules this document exists to enforce

> **1. The value never leaves by any path except a scoped, authorized resolution** — never in a schema, log, dashboard, usage record, git, or the APK. (SECRET-004)
> **2. The agent never sees raw secret material** — it holds a handle; the deterministic layer resolves it at the boundary. (SECRET-002)
> **3. Master keys / superuser secrets are a separate principal** — application compromise does not yield them. (SUPER-001)

---

## 1. Interface (SECRET-001)

```
interface SecretStore:
    set(scope, class, value) -> secret_ref        # store a value, return only a handle
    get(secret_ref, requester) -> value | denied  # resolve — authorization-checked, boundary-only
    delete(secret_ref, requester) -> ok | denied
    rotate(secret_ref, requester) -> new_ref | denied
```
`[LOCKED]`
- `set` returns a **handle** (`SecretReference.secret_ref`, `01` §8); the value is stored encrypted (§3) and never returned again except via authorized `get`.
- `get` is **authorization-checked** (§2) and only ever called by the **deterministic resolution boundary** — never handed to the agent or a tool directly. The resolved value is used at the boundary (e.g. injected into a provider call's auth header, `06`) and not persisted or logged.
- The interface is **replaceable** (SECRET-001): MVP = encrypted-local (§3); future = OS keychain / Vault / cloud KMS — swapping the backend does not change callers (they only ever hold handles).

---

## 2. Access mediation (SECRET-002, who may resolve what)

`[LOCKED]`
- **The agent may reference a secret only by `secret_ref`.** It cannot call `get`. When the agent proposes an action needing a credential (e.g. "call the Gemini tool"), the *runtime/adapter* — deterministic code — resolves the handle at the boundary; the raw value never enters the agent's context, prompt, or any observation returned to it.
- **Per-secret ownership/scope** (`01` §8): a `SecretReference` has `owner_scope_type ∈ {user, graph, server}` + `owner_scope_id`. Resolution is authorized against this: a user-scoped secret resolves only for that user's authorized operations; a server-scoped secret (e.g. the shared pilot API key, SECRET-003) resolves only for server-owned operations.
- **Tools** get a secret only if their `ToolConfiguration.secret_ref` grants it and the operation is authorized (`07`); a tool never receives a secret outside its declared need, and never one belonging to another scope.
- `[LOCKED]` **Graph sharing never shares a secret** (GRAPH-009): a `SecretReference` is never made graph-visible by any share operation; secrets are scoped independently of graph membership.

---

## 3. MVP implementation — encrypted local store (resolves OD-D1)

`[LOCKED for the guarantees; [IMPL] for exact primitives]` The MVP backend is an **encrypted-at-rest local store**. OD-D1 (key custody) is resolved as follows:

- **Encryption:** each secret value is encrypted with authenticated encryption (AEAD, e.g. AES-GCM or equivalent) under a data-encryption key (DEK). `[IMPL]` exact cipher, but AEAD (confidentiality + integrity) is required — a tampered ciphertext must fail, not silently decrypt to garbage.
- **Key hierarchy:** DEK(s) are wrapped by a **master key (KEK)**. The KEK is **not** stored in the application DB alongside the ciphertext (that would make a DB leak == plaintext). The KEK is held in the **superuser/bootstrap domain** (§4), separate from the application runtime.
- **Unlock / bootstrap:** at server start the store is **unlocked** by providing the KEK (or a passphrase that derives it via a strong KDF) through an operator/bootstrap step — `[IMPL]` (env-injected-at-launch, an operator-entered passphrase, or an OS-keychain-provided key). `[LOCKED]`: the KEK is **never** committed to git, baked into the APK, or logged; it is provided out-of-band at unlock time. Until unlocked, secrets cannot be resolved (fail-closed).
- **Key generation:** DEKs and any store-generated secrets use a CSPRNG. `[IMPL]` details; `[LOCKED]` no weak/predictable key generation.
- **Crash recovery:** the store recovers to a consistent state after a crash; a partially-written secret is either fully committed or fully absent (atomic write), never a corrupt half-secret. On restart it requires re-unlock (§ unlock) — a crashed-and-restarted server does not auto-unlock from disk.
- **Backup/restore:** `[IMPL]`; `[LOCKED]` a backup contains only *encrypted* values (never plaintext), and restore still requires the KEK — a stolen backup without the KEK yields nothing.

`[REC]` Interaction with `03` OD-D1 (device credential): prefer an **asymmetric device credential** (device holds a private key; server stores only the public key/verifier here) so that even a full SecretStore leak does not yield working device credentials — the server never held the impersonating secret. This is the recommended resolution of OD-D1 jointly across `03` and `12`.

---

## 4. Superuser separation (SUPER-001)

`[LOCKED]`
- **Superuser/admin identity, the KEK/master key, and the application runtime are separate principals.** The application process, the agent, and tool processes **cannot** obtain: the KEK/master keys, superuser credentials, or the ability to resolve a `class = master_key` reference.
- **Master-key references are never resolvable by the agent or user tools** (`01` §8 validation) — resolution is superuser/bootstrap-only.
- **Blast-radius objective** (BLAST-002): application compromise (even RCE, threat SEC-I) must not automatically yield the master key / all API keys / superuser creds. The KEK-outside-the-app-DB design (§3) is what makes "DB leak ≠ plaintext secrets" true; the superuser-separate-principal design is what makes "app RCE ≠ master key" *aimed for* — with the honest caveat that a live-process RCE could read secrets currently unlocked in memory (that residual is the OD-A1 territory, `14`).

`[LOCKED, honest bound]` This document does **not** claim RCE-proof secret isolation on a single process. It claims: DB-leak-proof (encrypted + KEK external), git/log/APK-leak-proof (never there), agent/tool-leak-proof (handle-only), and master-key-separation from the app. The residual — a compromised *running* process reading in-memory unlocked secrets — is a real limit, flagged to `14`/OD-A1, not hidden.

---

## 5. Rotation & revocation (SECRET-002)

`[LOCKED]`
- **Rotation** (`rotate`): issues a new value + new/updated handle, invalidates the old, atomically (no dual-valid window). Callers holding the handle transparently resolve the new value; callers holding a cached raw value (there should be none — handle-only) are irrelevant.
- **Revocation** (`delete` / mark revoked): immediate; the next resolution fails. Used for a compromised secret or a departed integration.
- Rotation/revocation are audited (`01` §11.1) — with the secret **value never in the audit record** (SECRET-004).

---

## 6. Never (SECRET-004) — the absolute prohibitions

`[LOCKED]` A secret value **never** appears in:
- source control (git) — config references a `secret_ref` or an env var name, never a literal key;
- the Android APK — no production key is ever shipped in the client (SECRET-004);
- logs, error messages, stack traces, or debug output (ties `02` error-envelope rule — errors are secret-free);
- the dashboard (DASH-005 — shows a secret *exists* + metadata, never its value);
- a `UsageEvent` or any research/audit record (USAGE-003);
- the agent's context, prompt, or any observation returned to it (SECRET-002);
- a `Mem0Fact` or any shared/graph-visible resource (GRAPH-009).

A single leak of a secret into any of these is a release-blocking defect.

---

## 7. Pilot secret model (SECRET-003)

`[LOCKED]` The ten pilot devices may use the **server owner's** model/API credentials — a deliberate pilot simplification; no enterprise secrets infra required. These are **server-scoped** `SecretReference`s (§2), resolved only for server-owned operations, never exposed to a user, a user's tool, or the agent's context. Self-hosted future: each owner uses their own credentials in their own SecretStore; secrets stay with them (P5). No part of the open repo requires the owner's secrets (HOST-001).

---

## 8. Failure behavior (fail-closed)

`[LOCKED]`
- Store locked/unavailable → resolution **fails closed** (the operation needing the secret is denied, explicit failure FAIL-012); the agent never receives a raw secret as a fallback, and no operation proceeds "without the credential."
- Decryption/integrity failure (tampered ciphertext) → hard fail, alert, never proceed with suspect material.
- Unlock not performed → secrets unresolvable; the server surfaces an explicit "locked" state rather than running in a degraded, silently-secretless mode that fabricates success.

---

## 9. Open items

| ID | Question | Status |
|---|---|---|
| **OD-D1** (PRD) | key custody / crypto specifics | **resolved here**: AEAD + external KEK + out-of-band unlock + asymmetric device cred `[REC]`; exact primitives `[IMPL]`, chosen before real credentials stored |
| OD-SEC-1 | KEK provisioning method (env vs passphrase vs OS-keychain) | `[IMPL]`; never-in-git/APK/log constraint locked |
| OD-A1 (PRD) | in-memory secret exposure under live-process RCE | → `14`; residual acknowledged (§4) |
| OD-SEC-2 | backup encryption/rotation cadence | `[IMPL]`; encrypted-only + KEK-required locked |

---

## 10. Acceptance hooks (for `17`)

- **SS-T1** a secret value never appears in git, the APK, logs, dashboard, usage/audit records, agent context, or a Mem0 fact (SECRET-004). *(release-blocking; the leak test)*
- **SS-T2** the agent can reference a secret only by handle and cannot call `get` (SECRET-002). *(release-blocking)*
- **SS-T3** a DB/backup leak without the KEK yields no usable plaintext (§3). *(release-blocking)*
- **SS-T4** a `class=master_key` reference is unresolvable by the agent or a user tool (SUPER-001).
- **SS-T5** graph sharing never exposes a `SecretReference` (GRAPH-009).
- **SS-T6** rotation invalidates the old value atomically (no dual-valid window).
- **SS-T7** revocation makes the next resolution fail immediately.
- **SS-T8** store locked/unavailable → operation fails closed, no raw-secret fallback (FAIL-012).
- **SS-T9** a tampered ciphertext fails integrity, never silently decrypts (AEAD).
- **SS-T10** a tool receives only its declared secret, never another scope's.

---

*End of 12_SECRETSTORE (DEEPEST). Continues to 13.*
