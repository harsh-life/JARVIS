# Data at rest — inventory, protection, and what is not protected

**Date:** 2026-11-27 (security hardening pass, `docs/DECISION_REGISTER.md` §2P)
**Status:** inventory and design. **Real user data remains prohibited**
(`docs/PROJECT_STATUS.md`, `docs/RELEASE_VALIDATION.md` — the real-data gate is
CLOSED). Nothing in this document is a claim that memory or the database is
encrypted: **only SecretStore values are encrypted at rest.**

## 1. The one thing that is encrypted

The **SecretStore** (`server/secrets/`) encrypts each secret value with
AES-256-GCM under a data key wrapped by an **external KEK**
(`HYPERMIND_KEK`, from the environment / systemd credential, never on disk in
the data directory). Values are resolved by handle only, per secret class, and
every resolution is audited. Key resolution fails closed: no KEK, or the wrong
KEK, and the server refuses to start (12 §8).

That protection covers **secret values only** — model API keys, OAuth tokens,
the OIDC client secret when stored by handle. It does **not** extend to the
relational database, the memory store, task responses, audit rows, backups or
logs. Encrypting the SecretStore does not encrypt Mem0, Chroma or anything
else.

## 2. Inventory

| Where | What (classification) | Encrypted at rest? | Key | Retention / deletion | Backup / recovery | Remaining risk |
|---|---|---|---|---|---|---|
| SecretStore rows (`secret_references`, ciphertext) | credentials (**secret**) | **yes** — AES-256-GCM | per-store data key wrapped by the external KEK | until deleted/rotated; per-secret rotation implemented | the ciphertext is backed up with the DB; useless without the KEK (by design) | KEK loss = every secret unrecoverable (§4). KEK rotation (re-wrap) is **not implemented** |
| Relational DB — users, devices, grants, graphs | identity, authorization state (**personal**) | **no** | — (disk/FS permissions; PostgreSQL role) | until deleted; account deletion **not built** | `pg_dump` (F7 not yet written up) | stolen disk/backup reads it |
| Relational DB — `agent_tasks.response`, inbox items, agent notebooks | user content (**private user data**) | **no** | — | task responses kept; notebooks until cleared/agent deleted | with the DB | stolen disk/backup reads it (BR-T2 row 2 class) |
| Relational DB — audit events | ids, actions, results, reason codes; **no values** by construction | **no** | — | append-only, no purge | with the DB | metadata leak (who did what when) |
| Relational DB — usage ledger | costs, counts (**operational**) | **no** | — | append-only | with the DB | low |
| Relational DB — `evaluations` | rules-evaluator findings (step refs, categories); historical Judge rows may contain model-written notes (**private user data**) | **no** | — | kept; cleanup `docs/RUNNING_EVALUATION.md` §5 | with the DB | historical notes readable at rest |
| Relational DB — `improvement_candidates`, `config_versions` (inert) | historical Judge text derived from one user's trace (**private user data**, possibly cross-user) | **no** | — | inert; manual backed-up cleanup `docs/RUNNING_EVALUATION.md` §5 | with the DB | readable at rest until cleaned |
| Relational DB — `automation_invocations` | n8n idempotency record; completed results (bounded, untrusted workflow output — **private user data**) | **no** | — | pruned after 30 days on next use | with the DB | result content readable at rest; never a credential |
| Relational DB — `devices.push_token` | FCM registration token (**device identifier**) | **no** (BR-T2 row 36) | — | cleared on revoke | with the DB | owner decision H-2 |
| Relational DB — access/bootstrap/confirmation tokens, device credentials | **hashes only** (`token_hash`), public keys only | n/a — no raw value stored (`tests/security_core/test_credentials_at_rest.py`) | — | expire / revoked | with the DB | none for the raw values |
| Persistent memory (Mem0 text + metadata, `data/mem0`) | user facts (**private user data**) | **no** — 0700 directory permissions only (docs/21 §7) | — | fact deletion removes the text from every store file | file copy of the directory | BR-T2 row 27: readable at rest |
| Vector index (Chroma / HNSW, `data/mem0`) | embeddings of user facts (**private user data**, partially invertible) | **no** | — | **deleted facts' vectors remain** until an index rebuild (BR-T2 row 28) | with the directory | deleted content partially recoverable |
| Knowledge Vault (Git repo + index) | curated notes (**user/operator content**) | **no** | — | Git history keeps every version | Git remote / copy | history retains deleted content |
| Temporary execution files (per-task temp root) | whatever a task wrote (**private user data**) | **no** | — | removed when the task ends (crash: left until next cleanup) | not backed up | crash leftovers |
| Logs (journald / stdout) | ids, decisions, reason codes; designed to carry no values; access logs off/query-stripped | **no** | — | journald policy | per host | a future log line that includes content |
| Backups | everything above that is backed up | **no** (except SecretStore ciphertext) | — | operator's | operator's | a backup is as sensitive as the live host |

## 3. Persistent memory: protection design (NOT implemented)

Encrypting "one file" would not protect memory: Mem0 writes text and metadata
into Chroma's SQLite file and the HNSW binary index, and embeddings are
themselves sensitive. A real design needs all of the following; until it is
built, **memory must not hold real or sensitive data** (OD-MB-4 stays open and
blocks the real-data gate).

1. **What is encrypted.** Fact text and metadata encrypted *before* they reach
   Mem0/Chroma (an `EncryptingMemoryStore` around the provider: AES-256-GCM per
   fact, associated data = owner id + fact id). Search then works only on
   embeddings, so:
2. **Embeddings.** Either (a) keep the vector index on an encrypted volume
   (LUKS/fscrypt with a key released at unlock), accepting that a live
   process sees them; or (b) per-owner indexes in separate encrypted
   directories. Application-level encryption of vectors defeats similarity
   search, so (a)/(b) are the realistic options.
3. **Key separation.** A memory data key distinct from the SecretStore data
   key, wrapped by the same external KEK (or a separate memory KEK); per-owner
   (or per-graph) subkeys derived with HKDF so one owner's key never decrypts
   another's facts. Crypto-shredding: deleting an owner's subkey makes their
   facts unreadable even where the vector store has not compacted.
4. **Rotation.** Re-wrap data keys under a new KEK (cheap); re-encrypt facts
   lazily on read/write, with a version tag per fact.
5. **Deletion.** Delete the ciphertext *and* schedule an index rebuild so the
   deleted fact's vector is gone too (closes BR-T2 row 28); per-owner
   crypto-shredding as the guarantee for backups.
6. **Recovery after key loss.** Memory is derived, user-entered content: losing
   its key loses the memory, never the account. Document that explicitly and
   keep the memory key under the same custody as the KEK (§4).
7. **Migration.** A one-way migration that reads each plaintext fact, writes
   it encrypted, rebuilds the index, then securely deletes the plaintext store
   directory (after a verified backup).

This is a design for a separate change with its own tests (MEM-T*, BR-T2 rows
27–28 re-measured). It is recorded here so that no document claims memory is
protected by the SecretStore.

## 4. SecretStore keys: custody, backup, recovery

* **KEK custody.** Generated once by `deploy/bootstrap.sh` and printed once.
  Store it in a password manager or on paper, **off the host and out of every
  backup** (`docs/DEPLOYMENT.md`). On the host it lives only in
  `/etc/jarvis/credentials/kek` (root 0700 dir, 0600 file), passed to the
  service as a systemd credential, never in `config.yaml` or the data
  directory.
* **Backups.** A database backup contains SecretStore ciphertext and is
  unreadable without the KEK — by design. Back up the DB and the data
  directory together; keep the KEK separately.
* **Restore.** Restore the DB (and data directory), install the KEK credential,
  start the service; the store unlocks with the KEK or the server refuses to
  start.
* **KEK loss is a real recoverability event.** Every stored secret becomes
  unrecoverable: model keys, OAuth tokens and the OIDC client secret must be
  re-entered (new handles). Nothing else in the DB is lost — it was never
  encrypted. Plan for it: keep two offline copies of the KEK.
* **Not implemented:** KEK rotation (re-wrapping the data key under a new KEK)
  and split/escrowed key custody. Rotating a *secret* is implemented
  (`SecretStore.rotate`).

## 5. Release gate

The real-data gate stays **CLOSED**. Open items this inventory makes explicit:
memory at rest (OD-MB-4, §3), push token at rest (H-2), no account deletion,
no KEK rotation, plaintext user content in the relational DB and backups, and
the application-level RCE blast radius (`docs/ADR_MULTI_USER_ISOLATION.md`).
