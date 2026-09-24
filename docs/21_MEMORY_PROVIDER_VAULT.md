# 21_MEMORY_PROVIDER_VAULT.md
## JARVIS / Hypermind Track B — MemoryProvider, Mem0 Adapter & Knowledge Vault

**Package:** next-build subsystem contract · **Depth:** deep · **Written:** 2026-09-24
**Numbering:** slot `21` is the vault reference `05` §7 already cites ("relevant vault (`21`)"). This document fills it; `11` keeps ownership of memory *semantics*.
**Status:** formalizes the owner's **memory/knowledge decisions** (owner-ratified 2026-09-24): a MemoryProvider abstraction; default provider self-hosted **Mem0 OSS, not forked**; Mem0 is an implementation, never an authorization layer; Vault stays separate.
**Authority:** below `00_CANONICAL_PRD.md` (notably §41 Appendix A, `[LOCKED]`) and `docs/DECISION_REGISTER.md`. Implements `11` (semantics: three layers, visibility triplet, write policy, owner-only correction) — does not restate or change it.
**Code this plugs into:** `server/memory/hydration.py` (`MemoryStore` Protocol, `AuthorizedContextHydrator` — already filters in-query and re-checks with the engine's `readable()`), `shared/schemas/memory.py` (`Mem0Fact`, `VaultDocument`, `VaultQuery*`), `server/config/schema.py` (`MemoryConfig`, `VaultConfig`), `server/vault/` (placeholder today), `02` §7–§8 memory/vault endpoints, import contract "Memory/vault never resolve secrets".

**Labels:** `[LOCKED]` · `[OWNER-RATIFIED]` · `[PROPOSED]` · `[IMPL]` · `[FUTURE]` · `[OPEN — OWNER]`.

---

## 0. The rules this document exists to enforce

> **1. Authorization happens in JARVIS, not in the memory store.** The engine decides; the provider stores and retrieves. The provider also filters by visibility in its own query as defence in depth, never as the only check.
> **2. Nothing reaches persistent memory without passing a deterministic gate.** No secret, no emotional/relationship content, no type outside the `fact_type` enum, no raw tool observation.
> **3. Every model call Mem0 makes is metered and egress-declared.** Mem0 gets no exception for being "just memory."

This is also the **real-data critical path**: `17` §5 requires MEM-T1 on a real Mem0 store and a BR-T2 re-run before any non-disposable data.

---

## 1. MemoryProvider interface

`[PROPOSED]` extends the existing `MemoryStore.search` Protocol into the full provider contract:

```
MemoryProvider (Protocol)
  search(query, owner_user_id, readable_graph_ids, limit) -> [MemoryCandidate]   # exists today
  add(fact: Mem0Fact) -> fact_id                    # fact already gated (§4) and authorized
  get(fact_id) -> Mem0Fact | None                   # caller authorizes the result via the engine
  update_content(fact_id, content)                  # caller has verified owner-only (D3)
  set_visibility(fact_id, visibility, graph_id)     # owner-only, audited by the caller (RAUTH V2)
  delete(fact_id)
  delete_all_for_user(user_id)                      # account deletion (LIFE-003)
  delete_graph_shared(graph_id)                     # graph deletion: shared facts only, never members' private facts
  health() -> bool
```

Rules:
- `[LOCKED]` (`11` §2) `search` applies the visibility predicate **inside the query**; the hydrator then re-checks each result with the engine predicate (already implemented).
- `[PROPOSED]` every other method is called **only after** the engine has authorized the operation for the principal. The provider exposes no "search across users" method; no JARVIS code path can ask it for another user's private facts.
- `[LOCKED]` provider unavailable → degrade with an explicit note, continue on available context (FAIL-008, `05` §2).
- `[OWNER-RATIFIED]` replacing Mem0 later is a new `MemoryProvider` adapter plus config; nothing in authorization, runtime, or the API changes.

---

## 2. Mem0 adapter

### 2.1 Deployment

- `[OWNER-RATIFIED]` self-hosted Mem0 OSS, pinned version, used as a library dependency. **Not forked.** Behaviour JARVIS needs beyond Mem0's defaults is wrapped in the adapter, never patched into Mem0.
- `[LOCKED]` (PRD §41) never call Mem0 with its default configuration (it defaults to OpenAI). Vector store Chroma, collection `hypermind_memories`; embedder `BAAI/bge-small-en-v1.5` locally.
- `[PROPOSED]` storage paths live under the configured data directory (`memory.mem0.path`), git-ignored (`15` §4). Mem0's own history database lives beside it.
- `[PROPOSED]` **no hidden egress** (`10`):
  - disable Mem0's product telemetry `[IMPL: verify the current mechanism for the pinned version — historically an environment variable]`;
  - pre-download the embedding model during bootstrap (`15` §3 step 5), then run the embedder offline so the server makes no Hugging Face calls at runtime.

### 2.2 Mem0's internal LLM calls

PRD §41 configures Mem0's LLM as `litellm` → `deepseek/deepseek-chat` with an environment-variable key. Used as-is, those calls would be **unmetered** (violates USAGE-001/US-T1), **undeclared egress** (`10`), and outside the gate in §4.

`[PROPOSED]` two acceptable mechanisms; the adapter must use one:

| Option | How | Keeps Mem0's own dedupe/merge | Metered / gated |
|---|---|---|---|
| **(a) Extraction in JARVIS, Mem0 as store** `[REC for pilot]` | JARVIS extracts candidate facts with its own `ModelProvider` call (metered, budget-checked, egress-bound), runs the §4 gate, then writes with Mem0's no-inference add mode `[IMPL: verify parameter name in the pinned version]`. Dedupe = adapter does search-before-write within the owner's scope. | no (adapter does simple dedupe) | yes, fully |
| **(b) Mem0 inference through a metering proxy** | Mem0's LLM config points at a loopback OpenAI-compatible endpoint inside JARVIS that meters, budget-checks, and forwards through the `06` adapter. Every write is re-checked by the §4 gate **after** Mem0 returns, and a failing fact is deleted. | yes | yes, but the post-write gate is required because Mem0 authors the stored text |

Either way the PRD §41 intent (never default to OpenAI; key never literal) is preserved. The literal `litellm/deepseek` provider choice becomes an operator config value rather than code. `[OPEN — OWNER]` OD-MEM-A: confirm this reading of §41, which is `[LOCKED]` and cannot be edited here.

### 2.3 Scope mapping — the leak-prevention detail

Mem0 scopes memories by an entity id (e.g. `user_id`). JARVIS needs owner-private facts plus graph-shared facts from other members.

`[PROPOSED]` mapping:
- Every fact is stored under Mem0 scope `owner:{owner_user_id}` with metadata `{fact_id, owner_user_id, source_user_id, graph_id, visibility, fact_type}`.
- Sharing (`private → graph`) additionally writes a **mirror** under scope `graph:{graph_id}` with the same `fact_id`, written without inference. Un-sharing deletes the mirror.
- `search` = one search in `owner:{caller}` ∪ one search per `graph:{id}` for `id ∈ readable_graph_ids`, then merge, then the hydrator's engine re-check.
- `[PROPOSED, security-critical]` **merging, updating or deduplicating happens only within one owner scope.** Mem0 must never merge text from two owners into one fact. Graph mirrors are never written with inference.

This makes cross-user isolation structural (different scopes) *and* logical (the predicate), matching `11` §8.

---

## 3. What gets written, and when

- `[LOCKED]` (`11` §4) only `fact_type ∈ {preference, past_request, stated_goal}`; default `visibility: private`; provenance recorded.
- `[PROPOSED]` **source material for extraction:** the user's own input and the task's final answer only. **Raw tool observations are excluded** — a file the user asked JARVIS to read may contain a credential or someone else's data, and memory must not learn it.
- `[PROPOSED]` **trigger:** runtime-owned extraction at task completion (`05` §9 "persists memory the task legitimately produced"). Explicit user adds via `POST /api/v1/memory` go through the same gate.
- `[PROPOSED]` `graph_id` of an extracted fact = the task's active graph (the schema requires one); visibility stays `private`.
- `[PROPOSED]` per-user switch `memory.writes_enabled` (default true) so a user can stop memory formation; listing/correcting/deleting stays available (`02` §7).
- `[FUTURE]` agent-proposed writes (`memory.write` capability, matrix §3.2) remain future.

---

## 4. The write gate (deterministic)

Every candidate passes, in order, before `add()`:

1. **Type:** `fact_type` in the enum; otherwise rejected (EMO-002, DM-T3).
2. **Emotional/relationship content:** `[PROPOSED]` a classifier step — model-assisted is allowed — whose result can only **reject**, never admit something the other checks rejected. Its failure rejects the candidate (fail-closed for writes). The enum alone cannot catch emotional text inside `content`.
3. **Secrets:** secret-pattern detector (same patterns as the repository scan: API-key formats, private-key blocks, high-entropy tokens). A match rejects the candidate and audits `memory.write.blocked_secret` without the value.
4. **Size and emptiness:** bounded length, non-empty (existing validator).
5. **Duplicate:** exact or near-duplicate within the owner scope → update-in-place or skip.

A rejected candidate is dropped, never coerced into a storable form.

---

## 5. Knowledge Vault

- `[OWNER-RATIFIED]` separate from Mem0 in storage and retrieval: its own Chroma **client object** and collection `hypermind_vault` (`[LOCKED]` VAULT-003; a shared client or collection is merge-blocking; `15` §6 already fails startup on a same-name config).
- `[PROPOSED]` **source of truth is Git**: curated markdown in the vault repository path. A reindex job chunks and embeds changed files after a commit. No vault text lives only in the vector store.
- `[PROPOSED]` **writes:** pilot vault content changes only through Git (PR review). `02` §8's `POST /api/v1/vault/documents` stays unimplemented for the pilot; if later enabled, it is superuser-only and writes by making a Git commit, never by writing the index directly. `[OPEN — OWNER]` OD-VLT-1.
- **Reads:** runtime hydration retrieves relevant vault chunks (`05` §7, `11` §3). `[PROPOSED]` an agent-proposed `vault.query` operation, if wanted, is a `low_read` capability added to the matrix — a follow-up, not assumed here.
- `[LOCKED]` vault content has no per-user data and no visibility triplet; a curator must never put private data in it (`11` §7). `[LOCKED]` vault text is untrusted data, never instructions (PRD §24), even though it is curated.

---

## 6. Self-hosting

```yaml
memory:
  provider: mem0                      # [PROPOSED] new key; mem0 is the only adapter now
  mem0:
    collection: hypermind_memories    # exists
    path: ./data/mem0_storage         # exists
    embedder: bge-small-en-v1.5       # exists
    mode: jarvis_extraction           # [PROPOSED] a | b per §2.2 (metering_proxy)
    extraction_model: null            # [PROPOSED] entry shaped like agent.primary; null = task's primary
  writes_enabled_default: true
vault:
  collection: hypermind_vault         # exists
  git_backed: true                    # exists
  path: ./data/vault                  # exists
```

`[LOCKED]` no literal key anywhere (`15` §2); a provider key is a `secret_ref`.

---

## 7. Encryption at rest

`[LOCKED]` (DECISION_REGISTER §4) per-user/per-graph key separation for memory at rest is **future hardening**, separate from OD-D1. It protects backups and stolen disks, not a compromised live process. The next build stores Mem0 data under the data directory's filesystem permissions only, and must say so to anyone deploying with real data.

---

## 8. Open items

| ID | Question | Status |
|---|---|---|
| OD-MEM-A | Reading of PRD §41 as "never default to OpenAI; provider is config" (§2.2) | `[OPEN — OWNER]` |
| OD-MEM-B | Pilot mechanism (a) or (b) | `[OPEN — OWNER]`, rec (a) |
| OD-MEM-2 | Embedding model | `[LOCKED]` rec already bge-small; unchanged |
| OD-AUTHZ-1 | Shared facts on graph-leave: stay or auto-unshare | `[OPEN — OWNER]` (from `11`), rec stay |
| OD-VLT-1 | Any API write path to the vault | `[OPEN — OWNER]`, rec Git-only for pilot |

---

## 9. Acceptance hooks (`[PROPOSED]` IDs; MEM-T* from `11` must pass on the real store)

- **MP-T1** MEM-T1 passes against a real Mem0 store: user B never retrieves user A's private fact via search, hydration, or the agent. *(release-blocking; real-data gate)*
- **MP-T2** a graph mirror exists iff the fact is graph-visible; un-sharing removes it.
- **MP-T3** no fact's text ever combines content from two owners.
- **MP-T4** every model call made for memory extraction (or through the metering proxy) emits a `UsageEvent`; a planted direct Mem0→cloud call is impossible in the configured mode.
- **MP-T5** a candidate containing a secret pattern is rejected and audited without the value.
- **MP-T6** raw tool observations are never extraction input.
- **MP-T7** emotional/relationship candidates are rejected; the classifier's failure rejects.
- **MP-T8** Mem0 telemetry is off and the embedder makes no network call at runtime (egress test).
- **MP-T9** `hypermind_memories` and `hypermind_vault` use distinct clients and collections (MEM-T6).
- **MP-T10** vault content changes only through Git + reindex in the pilot configuration.
- **MP-T11** account deletion removes the user's facts and mirrors; graph deletion removes only graph-shared facts.
- **MP-T12** BR-T2 is re-run with the memory store present and its rows recorded.

---

*End of 21. Next: `22_SCHEDULER.md`.*
