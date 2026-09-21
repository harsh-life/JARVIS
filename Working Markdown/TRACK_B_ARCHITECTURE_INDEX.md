# Hypermind Track B — Architecture Documentation Package (Index & Scaffold)

**Purpose:** the map for the Track B implementation-documentation set. The canonical PRD (`Track_B_PRD_Refined.md`, referenced here as `00_CANONICAL_PRD`) holds every requirement, decision, contract, and acceptance criterion. The subsystem documents (`01`–`17`) are the deeper *implementation contracts* derived from it — the level an engineer or Claude Code builds from without inventing anything.

**Source-of-truth rule `[LOCKED]`:** `00_CANONICAL_PRD` is authoritative. Subsystem docs may not silently override it. Any contradiction between a subsystem doc and the PRD is raised to the owner (Harsh), not resolved by a coding AI. Every requirement referenced in a subsystem doc keeps its PRD ID (e.g. `RAUTH-003`, `NET-005`), so traceability never breaks.

**Per-task workflow `[LOCKED]`:** an engineer/Claude Code working a task loads `00_CANONICAL_PRD` + the one subsystem doc for the task + the relevant acceptance/security sections — not all 18 documents. The repository holds the complete spec; each task reads its slice.

---

## Package structure

```
TRACK_B/
├── 00_CANONICAL_PRD.md                        [EXISTS — Track_B_PRD_Refined.md]
├── 01_DATA_MODEL_SCHEMA.md                     [PENDING]
├── 02_API_PROTOCOL.md                          [PENDING]
├── 03_AUTH_IDENTITY_SESSION.md                 [PENDING · deep]
├── 04_AUTHORIZATION_GRAPH_RESOURCE.md          [PENDING · DEEPEST]
├── 05_AGENT_RUNTIME.md                         [PENDING · deep]
├── 06_MODEL_PROVIDER_LLM_TOOL.md               [PENDING · compact]
├── 07_TOOL_CAPABILITY_EXECUTION.md             [PENDING · deep]
├── 08_ANDROID_SHIZUKU.md                        [PENDING · deep]
├── 09_FILESYSTEM_SANDBOX.md                     [PENDING · DEEPEST]
├── 10_NETWORK_EGRESS.md                         [PENDING · DEEPEST]
├── 11_MEMORY_CONTEXT_VISIBILITY.md             [PENDING · deep]
├── 12_SECRETSTORE.md                            [PENDING · DEEPEST]
├── 13_USAGE_RATE_BUDGET.md                      [PENDING]
├── 14_SECURITY_BLAST_RADIUS.md                 [PENDING · DEEPEST]
├── 15_CONFIGURATION_SELF_HOSTING.md            [PENDING · deep]
├── 16_REPOSITORY_MODULE_BOUNDARIES.md          [PENDING]
└── 17_TEST_ACCEPTANCE_VALIDATION.md            [PENDING · deep]
```

Depth tiers: **DEEPEST** (04, 09, 10, 12, 14) get the most contract detail — they are where a shortcut is most dangerous. **deep** get full contracts. **compact** (06) is thin because the PRD already covers it well.

---

## What each subsystem document owns

| Doc | Owns (implementation contract for) | Primary PRD requirements | Depth |
|---|---|---|---|
| **01 Data Model & Schema** | Canonical schemas for every first-class entity; field types, constraints, relationships, indexes, visibility fields | §26 all contracts; ENT-001; RAUTH-*; Mem0Fact/AuditEvent/UsageEvent/etc. | deep |
| **02 API & Protocol** | Every endpoint: request/response/auth/authZ/errors/timeout/retry/streaming/idempotency/versioning/request_id | §7,§8 topology; all `[IMPL]` interface points | deep |
| **03 Auth / Identity / Session** | OIDC flow (issuer/audience/state/nonce/PKCE); subject→user mapping; device registration; credential + token lifecycle; revocation/rotation/replay | AUTH-001..005; DEVICE-001; SESSION-001..005; FAIL-013 | deep |
| **04 Authorization / Graph / Resource** | The five-dimension resource-authorization engine; membership vs role vs ownership vs visibility vs capability; "can User B see *this*?" | RAUTH-001..005; GRAPH-001..009; §11A | **DEEPEST** |
| **05 Agent Runtime** | The loop: request→context→model→proposal→authorize→tool→result→continue→finish; max-iterations; nesting; cancellation; fallback; runaway detection | AGENT-001..004; §12; RATE-001 | deep |
| **06 Model Provider & LLM-as-Tool** | ModelProvider + model-tool contracts; registration/discovery; normalization; timeout/retry/cost; provider isolation; loop prevention | MODEL-001..005; MODELTOOL-001..004 | compact |
| **07 Tool / Capability / Execution** | user grant → capability → resource scope → tool → operation → primitive; automatic/confirm/never per operation class | TOOL-001..004; PERM-001..007 | deep |
| **08 Android / Shizuku** | capability → concrete Android/Shizuku/Accessibility operation mapping; validation; failure; not-a-Unix-CRUD-model | AND-001..006 | deep |
| **09 Filesystem Sandbox** | roots; path normalization; traversal/symlink defenses; archive/temp/size; cross-user prevention | FS-001..003; RAUTH (file visibility) | **DEEPEST** |
| **10 Network / Egress** | the enforcement mechanism (what physically stops a compromised tool); destination resolution; localhost/private/metadata/DNS-rebinding/exfil/reverse-shell | NET-001..005 | **DEEPEST** |
| **11 Memory / Context / Visibility** | session/Mem0/Vault layers; hydration; **private vs graph-shared memory** visibility model | MEM-001..006; VAULT-001..005; RAUTH-003 | deep |
| **12 SecretStore** | key generation/custody/unlock/bootstrap; encryption; rotation/backup/restore/revocation; crash recovery; access mediation | SECRET-001..005; SUPER-001; resolves OD-D1 | **DEEPEST** |
| **13 Usage / Rate / Budget** | UsageEvent ledger; counters; quotas; concurrency; budget evaluation; alerts | RATE-001..003; USAGE-001..003 | deep |
| **14 Security / Blast-Radius Validation** | threats → attack experiments → expected boundary → block → max blast radius → evidence → recovery; the OD-A1 experiment | §30 SEC-A..V; §31 BLAST-*; INV-1..20; §44 | **DEEPEST** |
| **15 Configuration / Self-Hosting** | config schema for every configurable surface; bootstrap flow; config-only-vs-code boundary | HOST-001..002; §33; OSS-001; §39 #28 | deep |
| **16 Repository / Module Boundaries** | can-import / cannot-import rules; android-never-owns-server-secrets; agent-cannot-directly-access-secrets | §45 repo structure; blast-radius module rules | standard |
| **17 Test / Acceptance / Validation** | requirement → test → fixture → expected → evidence → pass/fail; positive/negative/abuse/concurrency/isolation/failure/recovery | §39 all; §44; INV-1..20 | deep |

---

## Generation order (dependency-first)

Matches the canonical PRD's build order (§46) and the gap-spec's ordering. Foundations first because later docs reference their contracts:

1. `01` Data Model — everything else references these schemas
2. `02` API & Protocol — the interface surface
3. `03` Auth / Identity / Session
4. `04` Authorization / Graph / Resource **(DEEPEST)**
5. `05` Agent Runtime
6. `06` Model Provider & LLM-as-Tool (compact)
7. `07` Tool / Capability / Execution
8. `08` Android / Shizuku
9. `09` Filesystem Sandbox **(DEEPEST)**
10. `10` Network / Egress **(DEEPEST)**
11. `11` Memory / Context / Visibility
12. `12` SecretStore **(DEEPEST)**
13. `13` Usage / Rate / Budget
14. `14` Security / Blast-Radius **(DEEPEST)**
15. `15` Configuration / Self-Hosting
16. `16` Repository / Module Boundaries
17. `17` Test / Acceptance / Validation

You chose **all 17, sequential over the next turns.** I'll generate them in this order, marking each `[DONE]` in this index as we go, so we always know where we are.

---

## Status ledger

| Doc | Status |
|---|---|
| 00 Canonical PRD | **DONE** (corrected — all 12 review errors resolved, missing schemas + invariants added) |
| 01 Data Model & Schema | **DONE** — all canonical entities at implementation grade; enum registry; visibility triplet; ER map; storage-scoping (STORE-003 defers physical isolation to 14) |
| 02 API & Protocol | **DONE** — cross-cutting protocol rules (auth/request_id/idempotency/versioning/error envelope/anti-enumeration/metering); standard request lifecycle; all MVP endpoints across auth/graph/agent/tools/memory/vault/scheduler/voice/intelligence/dashboard; failure-state client contract |
| 03 Auth / Identity / Session | **DONE** — full Google OIDC flow (issuer/audience/state/nonce/PKCE); subject-keyed user mapping; device registration + long-lived credential lifecycle (rotate/revoke); short access-token issue/refresh/step-up; complete threat handling incl. documented stolen-device residual risk |
| 04 Authorization / Graph / Resource | **DONE (DEEPEST)** — five-dimension authorization (membership/role/ownership/visibility/capability); canonical fail-closed decision algorithm; private-vs-graph-shared enforcement (RAUTH-003); graph lifecycle (create/approve/leave/delete/transfer); sharing≠secret-sharing; anti-enumeration 404 rule; confused-deputy prevention for the agent; 12 release-blocking acceptance hooks |
| 05 Agent Runtime | **DONE** — propose→authorize→execute loop; hard bounds (iterations/tool-calls/model-calls/nesting/timeout/budget) so runaway is impossible; confirmation + absolute-floor gating; model fallback (explicit-fail, never fabricate); context compaction; confused-deputy prevention; deterministic-vs-model boundary table |
| 06 Model Provider & LLM-as-Tool | **DONE (compact)** — normalized ModelProvider interface + adapters; local-first; secrets-by-handle; LLM-as-tool via native abstraction (no MCP-per-provider); discovery; provider isolation; model-tool output treated as untrusted; nesting cap |
| 07 Tool / Capability / Execution | **DONE** — grant→capability→scope→tool→operation→primitive chain; deterministic risk-tier confirmation policy; automatic/confirm/never taxonomy; absolute-floor by absence; MCP-adapter trust boundary; per-tool fs/net/resource/secret boundary enforcement |
| 08 Android / Shizuku | **DONE** — capability→enumerated-operation→concrete-primitive mapping (not universal CRUD); per-app permission UI; two-layer (server+device) enforcement; consequential-action confirmation; shell as separate isolated high-risk capability; malformed-op rejection |
| 09 Filesystem Sandbox | **DONE (DEEPEST)** — permitted-roots model; path-normalization + traversal defense; symlink/zip-slip safety; sensitive-path exclusion; cross-user isolation wired to 04 visibility; enforcement at a boundary the tool can't bypass; untrusted file content = data |
| 10 Network / Egress | **DONE (DEEPEST)** — default-deny egress; per-tool declared destinations; non-bypass enforcement (NET-005); SSRF/metadata/localhost/private-net/DNS-rebinding defenses; credential-exfil + reverse-shell prevention; provider-adapter egress bound |
| 11 Memory / Context / Visibility | **DONE** — three-layer separation (session/Mem0/Vault, distinct collections); private-vs-graph-shared visibility filter (RAUTH-003); relevance-bounded hydration; owner-only correct/delete/share; emotional-content exclusion; cross-user isolation as testable property |
| 12 SecretStore | **DONE (DEEPEST)** — set/get/delete/rotate; encrypted-local impl with AEAD + external KEK + out-of-band unlock + crash recovery (resolves OD-D1); handle-only agent access; superuser master-key separation; no-secrets-in-logs/git/APK; honest in-memory-RCE residual flagged to 14 |
| 13 Usage / Rate / Budget | **DONE** — UsageEvent metering ledger; all per-scope + budget limits; budget enforced against ledger not guesses; explicit non-fail-open breach behavior; per-principal fairness; no secrets in usage records |
| 14 Security / Blast-Radius | **DONE (DEEPEST)** — 22 threat experiments (SEC-A..V); consolidated INV-1..20; dedicated OD-A1 adjudication with (a)/(b)/(c) options + real-data gate + measured-blast-radius experiment; honest residual table |
| 15 Configuration / Self-Hosting | **DONE** — full config surface; bootstrap flow; config-only-vs-code boundary; no-owner-secrets-in-repo; what-belongs-in-git-vs-env-vs-secretstore; fail-closed config validation |
| 16 Repository / Module Boundaries | **DONE** — layout + dependency-direction layering; the secrets boundary (agent can't import raw-secret access; android never holds server secrets); server/phone split; CI-enforced boundary lint |
| 17 Test / Acceptance / Validation | **DONE (final)** — consolidated requirement→test→evidence→pass matrix across all 16 docs; test taxonomy; mapping to PRD's 32 criteria; release-blocking set; the OD-A1 go/no-go experiment as the real-data gate; definition of done |

**PACKAGE COMPLETE — 17/17 subsystem docs done, plus 00 (PRD) + this index. Track B is architecture-complete and implementation-ready, subject to the open decisions (notably OD-A1, resolved via the BR-T2 experiment before real user data).**

---

## What is deliberately NOT a separate document

Per the reviewer's guidance, these are already sufficiently covered in `00_CANONICAL_PRD` and do **not** get their own docs: Product Vision; General Architecture narrative; Intelligence Philosophy (§25/§36); LoRA (correctly future/deferred, §21); future Darwin; business model; general MCP explanation; general voice philosophy. Generating separate docs for these would be duplication, not clarity.
