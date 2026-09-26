# Running persistent memory and the Knowledge Vault

This extends `docs/RUNNING_RUNTIME.md` and `docs/RUNNING_EXECUTION.md`. What is new
is the **memory build** (`11`, `docs/21_MEMORY_PROVIDER_VAULT.md`): a
`MemoryProvider` abstraction, its self-hosted Mem0 OSS adapter, and a separate,
Git-backed Knowledge Vault.

Three context layers, never merged (MEM-001):

| Layer | Where | Lifetime | Visibility |
|---|---|---|---|
| Session memory | the runtime's task state | one task | the acting principal |
| Persistent memory | Mem0 over Chroma in `memory.mem0.path` (collection `hypermind_memories`) | until the owner deletes it | private, or graph-shared by its owner |
| Knowledge Vault | Git repository `vault.path`, indexed into `vault.index_path` (collection `hypermind_vault`) | curated | shared by construction |

**Real data is still gated.** See §8.

## 1. What is installed, and what it does *not* do

```bash
pip install -e '.[memory]'   # mem0ai==2.2.1, chromadb==1.5.9, fastembed==0.8.1
```

- **Mem0 is a library, not a service**, and not forked. One adapter
  (`server/memory/mem0_provider.py`) is the only module that imports it
  (import-linter contract), and it refuses to start on any other Mem0 version.
- **Mem0 never calls a model.** Every write uses Mem0's no-inference mode
  (docs/21 §2.2 option (a)). Mem0's LLM slot holds a stub that refuses any call,
  so its own extraction path cannot reach OpenAI or anything else. There is no
  provider key to configure for memory.
- **The embedder runs locally and offline**: `BAAI/bge-small-en-v1.5` on ONNX
  Runtime (fastembed). The server loads it with `local_files_only`; it never
  downloads a model.
- **Telemetry is off.** Mem0's PostHog telemetry and remote "notices" fetch are
  disabled before Mem0 is first imported; Chroma's telemetry is disabled on both
  clients. `~/.mem0` is never created — Mem0's home is inside `memory.mem0.path`.
- **spaCy must not be installed** in the server's environment. With it present,
  Mem0 would download a spaCy model at runtime and keep a content-derived entity
  store; the adapter refuses to start instead.

The egress suites (`tests/memory/`) run the whole stack under an audit hook and
assert no socket, DNS lookup, URL fetch, subprocess, or out-of-place file write.

## 2. Turning memory on

```bash
python -m server.memory provision      # the one network step: downloads the embedding model
```

then in `config.yaml`:

```yaml
memory:
  enabled: true
  writes_enabled: true     # memory formation; listing/correcting/deleting stay available when false
  auto_extract: false      # see §4
  mem0:
    path: "./data/mem0_storage"
    embedder_cache: "./data/models"
```

`memory.enabled: true` with a missing stack, a different Mem0 version, spaCy
present, or an unprovisioned model **stops the server at startup** with the
reason. It never silently starts without memory.

With memory disabled (the default) the server runs as before: hydration adds the
explicit note "long-term memory temporarily unavailable" (FAIL-008), and the
memory endpoints answer `503 dependency_unavailable` with `dependency: mem0`.
If the store fails at runtime, the same happens per request — degraded, never
fabricated, never unfiltered.

`python -m server.memory status` opens the store and reports whether it answers.

## 3. The API (02 §7)

| Call | Who | Notes |
|---|---|---|
| `GET /api/v1/memory?query=&limit=` | any user | Own facts + graph-shared facts of graphs they are an **active** member of. Filtered in the store query *and* re-checked with the engine's `readable()`. |
| `POST /api/v1/memory` `{fact_type, content, graph_id?}` | any user | Stored **private**, owned by the caller, in `graph_id` (or the session's active graph) — the engine checks membership (D1). Every write passes the write gate (§5). |
| `PATCH /api/v1/memory/{id}` `{content?, visibility?}` | owner only | Correction is automatic; sharing/un-sharing is `consequential`: the first call returns `403 confirmation_required` with a single-use token, the second carries it in `X-Confirmation-Token`. Audited (`memory.corrected`, `resource.visibility.shared/unshared`). |
| `DELETE /api/v1/memory/{id}` | owner only | Also confirmation-gated. Audited (`memory.deleted`). |
| `GET /api/v1/memory/status` | any user | `{enabled, available, ...}` — no content, no counts. |

A fact the caller cannot see answers `404`, indistinguishable from none (04 §7).

## 4. Automatic memory formation (`auto_extract`)

With `memory.auto_extract: true`, when a task **completes** in `execute` mode the
runtime makes one extra call to the task's own model, bounded by the task's
`max_model_calls`, budget-checked, and metered as a `model_call` UsageEvent
(MP-T4). Its only inputs are the user's request and the final answer — never tool
output, hydrated memory, or vault text (MP-T6). Each proposed fact still passes
the write gate; a failure here never fails the task (the task's notes say memory
was not updated). Draft, suggest and observe tasks form no memory.

## 5. What may be stored — the write gate

Only `preference`, `past_request`, `stated_goal`, as one short plain statement.
Rejected, with the reason audited and the content never logged:

- secret-shaped text (keys, tokens, private keys, `password is …`, credentials in
  URLs, high-entropy strings) — `secret_detected:<pattern>`;
- emotional or relationship content (EMO-002/004) — the classifier can only
  reject, and its failure rejects;
- tool observations, hydrated-context blocks, JSON payloads, code, multi-line text.

**At rest.** The memory store is plaintext on disk, protected by owner-only
(0700) directory permissions only — per docs/21 §7, per-user encryption at rest is
future hardening. Deleting or correcting a fact removes its text from every
store file before the call returns (Mem0's history DB is disabled; Chroma's
write-ahead log is flushed and its SQLite file vacuumed). The **embedding
vector** of a deleted fact can remain in Chroma's HNSW index file until the
index is rebuilt. Protect the data directory and its backups accordingly.

## 6. The Knowledge Vault

Curated markdown in a Git repository. The first directory level is the
*domain* (`product/units.md` → `product`; files at the root → `general`).

```bash
git -C ./data/vault commit -am "reviewed change"
python -m server.vault reindex     # indexes the committed tree at HEAD
python -m server.vault status      # indexed commit vs HEAD
```

- Only **committed** content is indexed — straight from Git's object store, so an
  uncommitted edit never reaches the index.
- Refused and reported (by path and reason, never content): the `finance`,
  `health`, `education` domains (VAULT-005), files with secret-shaped text,
  symlinks, non-UTF-8 or oversized files.
- **No HTTP write path** (OD-VLT-1): `POST /api/v1/vault/documents` does not
  exist. `GET /api/v1/vault/query?domain=&question=&top_k=` reads.
- With `vault.enabled: true`, each task's context gets up to
  `vault.hydration_top_k` relevant chunks, marked as untrusted reference data.
  A down vault adds the FAIL-009 note and the task continues.

The vault has its own Chroma client and directory. Configuration fails to load
if `vault.index_path`, `vault.path` and `memory.mem0.path` are equal or nested, or
if the two collection names match (VAULT-003).

## 7. Tests and checks

```bash
pip install -e '.[dev,memory]'
python -m server.memory provision --config config.example.yaml   # once
HYPERMIND_REQUIRE_MEMORY_STACK=1 python -m pytest tests/memory -q
python -m pytest tests/integration/test_br_t2_memory_rows.py -q -s  # BR-T2 memory rows
lint-imports --config pyproject.toml
```

Without the stack or the model the memory suites **skip locally** with the command
to run; with `HYPERMIND_REQUIRE_MEMORY_STACK=1` (set in CI) they **fail** instead.

## 8. The real-data gate stays closed

17 §5 and docs/21 require, before any non-disposable user data: MEM-T1 on a real
Mem0 store, the memory authorization/isolation suite, provider validation, the
release-blocking memory suite, and a BR-T2 re-run. All of those now run on the
real store in CI. They are necessary, not sufficient:

- the rest of 17 §5's release-blocking set still includes the Android suite on a
  real device, which does not exist yet;
- BR-T2 rows 27–28 (`docs/OD_A1_BR_T2.md` §3c) — memory readable at rest, deleted
  facts' vectors retained — are outside OD-A1's accepted class and need an owner
  decision;
- OD-MEM-A, OD-MEM-B, OD-AUTHZ-1 and OD-VLT-1 (docs/21 §8) remain `[OPEN — OWNER]`;
  this build implements their recommended readings.
