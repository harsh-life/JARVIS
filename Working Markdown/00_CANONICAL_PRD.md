# 00_CANONICAL_PRD.md
## Hypermind Track B — Canonical Product & Requirements Specification

**Package:** the root document of the Track B architecture package · **Status:** ratified — reconciled against `01`–`17`, the index, and `26` · **Authority:** this document is the top of the hierarchy. `01`–`17` are subordinate implementation contracts derived from it; `26_DECISION_PROVIDER.md` is a future-band proposal pending ratification (§25A). No subsystem document may silently override anything here; a contradiction is raised to the owner (Harsh), never resolved by a coding AI (per `TRACK_B_ARCHITECTURE_INDEX.md`'s source-of-truth rule).

**Relationship to `Track B PRD.md` (repo root):** that document is the earlier, business-facing Phase-2b product brief (Knowledge Vault, dual-memory, dashboard, LoRA, deployment gates, budget, timeline). It is real prior product decision-making and is **preserved, not discarded** — its content is folded into this document (§1–4, §10, §21, §23, §34–35, §38, §40–43) wherever it does not conflict with the deeper technical/security contracts `01`–`17` already assume. Where the two disagree (none found during reconciliation), this document controls.

**Why this document exists now:** every one of `01`–`17` and `26` cites `00_CANONICAL_PRD` — by name, by section number (e.g. `§11A`, `§29A`, `§39`), and by ~150 requirement IDs (`RAUTH-*`, `GRAPH-*`, `SEC-A..V`, `INV-1..20`, etc.) — as their authority. That file did not exist anywhere in this repository or its git history. This document **is** that file, reconstructed to match every external citation exactly, so `01`–`17`/`26` require **zero edits**. Section numbers below were chosen to land precisely on every number any subsystem doc already cites; unclaimed numbers were filled with the narrative/product content those docs' surrounding prose implies.

**Label legend (same convention used throughout the package):** `[LOCKED]` fixed, canonical · `[IMPL]` engineer chooses within the stated constraint · `[FUTURE]` deferred, not MVP · `[OPEN — OWNER]` unresolved, owner decides.

---

# PART I — PRODUCT

## 1. One-Paragraph Summary

`[LOCKED]` Track B is the consumer-facing product: a JARVIS-like assistant running on the same 4GB-RAM-budget Android phones Phase 1 proved works, that sees the screen, takes actions through the existing Accessibility/Shizuku execution layer, and gets more useful to each individual user over time through a personalization architecture built on cheap per-user LoRA adapters (§21) rather than expensive full fine-tunes. Development happens in parallel with Track A, but nothing here ships to real public end-users until Track A proves it can generate consistent revenue (§38). Track B is deliberately built *without* domain intelligence (finance, health, education) — that is Track A's research, arriving later through the IntelligenceProvider socket (§25). The deeper architectural discipline layered on top of that product brief — deterministic authorization, capability-scoped execution, secret-by-handle isolation, egress control, and an honest threat model — exists because Track B is a **multi-user, on-device-acting agent runtime**, not a single-user chatbot: the failure modes that matter most are one user's data leaking to another and an agent taking an action nobody authorized.

## 2. Problem Statement

`[LOCKED]` A working on-device execution layer (Phase 1) is a capability, not a product. Track B turns "the phone can see and act" into "the phone remembers what I care about, adapts to how I'm feeling right now, and gets better at helping me specifically the more I use it" — without crossing into simulating a relationship (§23), and without one user's private context ever becoming visible to another user who happens to share a workspace with them (§11A). The problem is building genuine, auditable, contained utility-driven personalization for a shared multi-tenant pilot, staying strictly on the utility side of the emotional-simulation line and strictly inside the authorization boundary on the multi-user line.

## 3. Goals

`[LOCKED]`
1. Build the Knowledge Vault (static, shared RAG knowledge base) and wire it to the Gateway (§25/§26, VAULT-001..005).
2. Build the dual-memory architecture (session-volatile + long-term Mem0), keeping the layers strictly separated (§26, MEM-001).
3. Build a deterministic authorization engine (§11A) so that graph membership never implies resource visibility — the single most important correctness property in the system.
4. Build the agent runtime as a bounded propose→authorize→execute loop (§12) so a runaway or malicious agent cannot take an unauthorized action or consume unbounded resources.
5. Build the Flask/operator dashboard for internal visibility into vault content, memory state, usage, and pipeline health — secret-free, PII-redacted by default (§28).
6. Build the Phase-3 IntelligenceProvider hook — the socket domain verticals plug into later — disabled by default, and prove the system works fully without it (§25, INTEL-003).
7. Run a small (~10-person) internal technical pilot the moment Track A produces its first validated report (§38).
8. Have the investor narrative and pilot documentation ready the moment Track A's real revenue trend exists (§38).

## 4. Explicit Non-Goals

`[LOCKED]`
- No public launch or real end-user onboarding before the Decision Gate (§38) is met.
- No finance, health, or education domain content or logic in Track B itself — that is Phase 3, gated on Track A's research succeeding and replicating, and delivered only through the IntelligenceProvider socket (§25), never hard-coded into the base product.
- No simulated agent emotions, mood, personality, relationship/trust scoring, or diary content anywhere in the vault, prompts, memory, or UI (§23) — a hard boundary, not a style preference.
- No investor pitching before Track A's 3-month consistent revenue trend exists (Track A's gate, governs Track B's PR/Comms timeline — §38).
- No full per-user fine-tuning — LoRA adapters only (§21), for cost reasons.
- No architecture, mechanism, or "temporary" shortcut that collapses the five-dimension authorization model (§11A) into a simpler membership-only check, however tempting under time pressure — this is Track B's one absolutely non-negotiable line.

## 5. Governing Principles (P1–P5)

`[LOCKED]` Five cross-cutting principles that every subsystem document inherits and that resolve ambiguity when a subsystem doc is silent:

| ID | Principle | Realized in |
|---|---|---|
| **P1** | **The determinism boundary.** The agent *proposes*; deterministic infrastructure *decides and executes*; a human *confirms* where the risk model requires it. No security-relevant decision is ever delegated to a model. | `04`, `05` §0/§11 |
| **P2** | **Absence over restriction.** The strongest guarantees are the *absence* of a capability or code path (no `get()` on a master key, no submission-equivalent capability), not a check that could in principle be bypassed. An absence cannot be exploited. | `07` §7 (PERM-006), `12` §4 |
| **P3** | **Disabled ≠ erroring.** An optional, not-yet-enabled subsystem (IntelligenceProvider, a future provider) is simply *absent* from the surface — it returns "not enabled," never a broken/erroring "present but failing" state. | `02` §11, `15` §2 |
| **P4** | **Swappable by configuration.** Model providers, tools, and adapters are swappable via config; adding a new *instance* of an existing kind is never a runtime rewrite. | `06` §1/§2, `15` §1 |
| **P5** | **Self-hostable and credential-independent.** Anyone can clone, configure, and run Track B on their own hardware with their own credentials, needing none of the original owner's keys, tunnel, or data. | `15` §0 |

This is Track B's analogue of Track A's "no submission capability exists" guarantee (P2), restated as a first-class principle rather than left implicit — see §16 for its concrete realization in the capability model.

## 6. System Architecture & Topology (narrative)

`[LOCKED]`
```
DEVICE (Android — Accessibility Service, Shizuku, on-device capture)
   │  JSON over HTTPS (Cloudflare Tunnel — transport only, §18)
   ▼
┌───────────────────────────────────────────────────────────────────┐
│ GATEWAY (single FastAPI process, modular routers — §7)            │
│  ┌───────────────┐ ┌────────────────┐ ┌───────────────────────┐  │
│  │ Session state │ │ Agent runtime  │ │ Knowledge Vault query  │  │
│  │ (volatile)    │ │ (§12)          │ │ (ChromaDB, §25/§26)    │  │
│  └───────────────┘ └────────────────┘ └───────────────────────┘  │
│  ┌───────────────┐ ┌────────────────┐ ┌───────────────────────┐  │
│  │ Mem0 (§26)    │ │ Tools/Models   │ │ IntelligenceProvider   │  │
│  │ persistent    │ │ (§16/§19)      │ │ socket (disabled, §25) │  │
│  └───────────────┘ └────────────────┘ └───────────────────────┘  │
│  Authorization engine (§11A) gates every arrow above.             │
└──────────────────────────────┬──────────────────────────────────┘
                                ▼
                     ┌─────────────────────┐
                     │ Operator dashboard  │
                     │ (§28 — internal)    │
                     └─────────────────────┘
```
- **Knowledge Vault:** static, shared, curated markdown, git-backed, indexed via ChromaDB (`hypermind_vault` collection), queried `GET /vault/query` → top-K chunks. Nothing domain-specific (finance/health/education) at MVP (§25, VAULT-005).
- **Dual/triple memory:** session-volatile JSON (in-process, discarded at session end) → long-term Mem0 (`hypermind_memories` collection, visibility-filtered, §26) → Knowledge Vault (shared, no visibility triplet). Collapsing any two of these layers is the single most likely way this architecture quietly degrades — never do it, even under time pressure (ties `STORE-001`/`VAULT-003`).
- **IntelligenceProvider hook:** the Phase-3 socket. `POST /skills/register` (or equivalent internal contract) exists and is tested against a mock manifest; nothing real is registered at MVP (§25).

## 7. Deployment Topology & Transport

`[LOCKED]`
- **`SRV-002`:** Track B's endpoints run as modular routers within one FastAPI Gateway process — a monolith, not microservices. Splitting is overkill before real load demands it. The Gateway is reachable through a Cloudflare Tunnel (`cloudflared`) at pilot scale; a self-hoster may substitute any tunnel/reverse-proxy (§33).
- One process, one port, one tunnel config for the pilot. Moving to a real VPS post-Decision-Gate changes hosting, not architecture (§38).
- The dashboard (§28) is a separate internal-facing surface on the same Gateway, admin-only.

## 8. Interface Surface Overview

`[LOCKED]` Three distinct interface surfaces, never conflated:
1. **Client-facing HTTP API** (`/api/v1/...`) — what the Android app calls. Every endpoint independently performs full authentication + authorization even though it arrives via the tunnel (§18). Fully specified in `02`.
2. **Internal agent/tool/model invocation contracts** — not HTTP; in-process calls from the agent runtime (§12) to tools (§16), model providers (§19), and (if enabled) the IntelligenceProvider (§25). Every internal call still passes through the same authorization chain and metering as a client-facing one.
3. **Admin/dashboard surface** (`/api/v1/admin/*`) — superuser-only, secret-free, PII-redacted by default (§28). Structurally separate from the ordinary user token space (§9).

`02` derives its full endpoint catalog from this surface model; `01` supplies the data shapes every surface exchanges.

## 9. Identity, Device & Session Model

`[LOCKED]` Realizes `AUTH-001..005`, `DEVICE-001`, `SESSION-001..005`, `PHONE-003`, `PHONE-004`.

| ID | Requirement |
|---|---|
| `AUTH-001` | Identity comes from an OIDC identity provider (Google at MVP); the interface is provider-replaceable — multi-provider linking is future (`OD-AUTH-1`). |
| `AUTH-002` | Login requests only minimal identity scopes (`openid`,`email`,`profile`) — never Gmail/Drive/Calendar/Contacts/Photos. Google login grants zero access to Google APIs. |
| `AUTH-003` | Any future Google-data access is a separate, explicit consent flow — never bundled into login. |
| `AUTH-004` | Authentication ("who are you") and authorization ("what may you access") are separate systems. Google is the identity provider, never Hypermind's authorization authority. |
| `AUTH-005` | The stable identity key is the `(issuer, subject)` pair, never email (mutable). Full `id_token` validation (signature/issuer/audience/expiry/nonce/state/PKCE) is mandatory on every login. |
| `DEVICE-001` | A device is a registered client bound to one user, with its own long-lived, revocable, rotatable credential — distinct from any short-lived access token. |
| `SESSION-001` | Access tokens are short-lived; the device credential refreshes them without a full re-login. |
| `SESSION-002` | A lost/stolen device can be revoked by its owner; revocation is immediate for future refreshes. |
| `SESSION-003` | Some operations (account deletion, credential rotation, future high-risk actions) require fresh authentication/re-attestation ("step-up") even with a valid access token. |
| `SESSION-004` | Every resource-touching request runs the full deterministic chain: token → device → user → session → (graph membership → visibility → capability → absolute-floor) before any side effect. |
| `SESSION-005` | Every credential-theft/replay/CSRF/interception threat has a named, handled case (`03` §7) — including an honestly documented residual (stolen device credential pre-revocation). |
| `PHONE-003` | **The client is never trusted to assert identity.** Every identity fact acted on is server-derived from a validated token/credential — never a `user_id`/`graph_id`/`session_id` in a request body. |
| `PHONE-004` | The device's long-lived credential is stored client-side only in Android secure storage (Keystore-backed); it is never in source, logs, or any server-returned value after the one-time registration response. |

## 10. Target User & Pilot Model

`[LOCKED]` People on $100–200 Android devices — the largest smartphone price segment in India, structurally excluded from flagship on-device AI as the RAM bar for on-device assistants moves *up*, not down. During Phase 2b, the actual near-term "user" is a small internal pilot group of ~10 people (§38), not the eventual public audience — design and test against that reality (multi-tenant, laptop-hosted, disposable data) rather than eventual scale.

---

# PART II — IDENTITY, AUTHORIZATION & CONTEXT

## 11. Graph & Membership Model

`[LOCKED]` Realizes `GRAPH-001..009`. The graph is Track B's first-class authorization boundary *and* context substrate.

| ID | Requirement |
|---|---|
| `GRAPH-001..003` | A graph is `private` (single-user) or `shared` (multi-user); every user-generated resource that can live in a graph carries the visibility triplet (§26, §11A). |
| `GRAPH-004` | Context hydration for a request follows: current request → graph/task context → **authorized** relevant memory retrieval (§11A visibility filter applied) → relevant vault knowledge → model context. Never a lifetime-history dump. |
| `GRAPH-006` | A session has an `active_graph_id`, switchable, always re-checked live for membership — never trusted from a stale row. |
| `GRAPH-007` | Graph creation: the creator becomes `owner`. A `private` graph has exactly one membership; a `shared` graph grows only via approval (`GRAPH-008`). |
| `GRAPH-008` | Membership is created **only** by explicit owner (or explicitly-permitted role) approval of a join request — never self-service, never client-asserted. |
| `GRAPH-009` | **Sharing a graph never shares a secret.** A `SecretReference` is never made graph-visible by any share operation; secrets are scoped independently of graph membership. |

## 11A. Resource Authorization — the Five Dimensions

`[LOCKED]` Realizes `RAUTH-001..005` and the invariants `INV-3/4/5` (§31). **This is the single most important correctness surface in Track B** — the guarantee that being a member of a graph does not grant access to every resource in it.

| ID | Requirement |
|---|---|
| `RAUTH-001` | Authorization is the conjunction of **five independent dimensions**: (1) membership, (2) role, (3) ownership, (4) visibility, (5) capability. All applicable dimensions must pass — there is no single yes/no on membership alone. |
| `RAUTH-002` | `graph_id` alone never authorizes a read. Presence of a `graph_id` on a resource is a *scoping* fact, not an *authorization* fact. |
| `RAUTH-003` | **The private-vs-graph-shared rule:** `visibility: private` is readable only by `owner_user_id`, never by other graph members regardless of shared `graph_id`. `visibility: graph` is readable by active members of that graph. This is the rule that prevents cross-user leakage inside a shared graph — realized identically in `04` (resources), `09` (files), and `11` (memory). |
| `RAUTH-004` | The read predicate is exactly: `readable(user, resource) := (resource.visibility == graph AND active_member(user, resource.graph_id)) OR resource.owner_user_id == user`. No other path to readability exists. |
| `RAUTH-005` | **Default visibility is always `private`.** A resource becomes graph-visible only through an explicit, owner-initiated, audited share operation (`RAUTH V2` — changing visibility is never a side effect of any other operation, and is always audited). |

Anti-enumeration `[LOCKED]`: a denial that would reveal the existence or visibility of a resource the caller cannot see returns `404`, indistinguishable from "does not exist" — never a distinguishable `403` that would let an attacker enumerate graph contents or probe for other users' private resources.

## 12. Agent Runtime Bounds & Orchestration Principles

`[LOCKED]` Realizes `AGENT-001..004`, ties `RATE-001`, principle `P1`.

| ID | Requirement |
|---|---|
| `AGENT-001` | The runtime executes a canonical loop: build authorized context → model proposes → parse → authorize (§11A) → execute or confirm → observe → bound-check → finish. The model never executes anything directly. |
| `AGENT-002` | Every task runs under hard, deterministic ceilings enforced by the runtime, never the model: max iterations, max tool calls, max model calls, max model-tool nesting depth, wall-clock timeout, per-task budget, concurrency cap. Exact numeric values are `[IMPL]` (`OD-02`); the *existence and enforcement* of every ceiling is locked. |
| `AGENT-003` | A ceiling breach produces an **explicit failure**, never a silent stop and never a fabricated "done" (`FAIL-CORE-001`). |
| `AGENT-004` | The runtime constructs every authorization request **as the principal**, never as "the agent" — the agent can never do, on the principal's behalf, anything the principal could not do themselves (confused-deputy prevention). |

## 13. Device Permission Model — Per-App Capability Grants

`[LOCKED]` The Android client presents a settings-style, per-app capability grid the user directly controls (screen-read, UI-interaction, file-read, file-write, execute — each independently toggled per app). Each toggle is a `CapabilityGrant` (§26 `CapabilityGrant`), created by explicit user consent (`PERM-002`). Granting a capability on an app lets the agent compose many operations within it without re-prompting per primitive — but only the operations that capability's mapping enumerates (§16, `AND-006`), and consequential actions still hit confirmation (§14). Revocation is immediate: toggling off revokes the grant, and the next device operation for that app fails authorization.

## 14. Device Consequential-Action Policy

`[LOCKED]` A granted device capability does **not** imply automatic permission for every consequential action inside its scope. "Open WhatsApp" is automatic (`low_read`/`low_write`); "send this message to X" or any financial/irreversible action is `consequential`/`high_irreversible` and requires human confirmation regardless of which capability enabled the surrounding interaction (§15, `PERM-004`). The tier is decided by deterministic policy, never by the model.

---

# PART III — CAPABILITY, TOOLS, DEVICE

## 15. Risk Tiers & Confirmation Policy

`[LOCKED]` Realizes `PERM-004/005/007`. Four tiers, one deterministic policy:

| Tier | Examples | Disposition |
|---|---|---|
| `low_read` | read own memory, read battery, read a screen element | automatic |
| `low_write` | edit a document in own sandbox, set a reminder | automatic (or light confirm per config) |
| `consequential` | send a message, post externally, share a resource, external network write | `require_confirmation` |
| `high_irreversible` | a financial action, bulk delete, irreversible external effect | `require_confirmation` (strong) |

`[LOCKED]` The tier→disposition mapping is a deterministic table, never model judgment (`PERM-005`). Consequential/irreversible tiers always require human confirmation before execution; no timeout ever auto-approves. The three-tier taxonomy — **never / confirm / automatic** — is exhaustive; every operation is exactly one of the three (`PERM-007`).

## 16. Capability Model

`[LOCKED]` Realizes `PERM-001..003`, `TOOL-001..003`, `AND-006`. The chain: user grants a **capability** (not a per-primitive prompt) → the capability is scoped to a `resource_scope` → it gates a **tool** → the tool exposes an **enumerated** set of **operations** → each operation maps to a concrete **execution primitive**. A capability that *sounds* like universal CRUD (e.g. `file.read`) never grants universal CRUD — it grants exactly its enumerated operations, nothing outside the mapping is executable even with the capability held. Capability holding never bypasses visibility (§11A `RAUTH-003`) — `file.read` never lets its holder read another user's private file. This is Track B's realization of principle `P2`/`P5`'s absence-based guarantee (the analogue of Track A's "no submission capability exists"): the absolute-floor prohibitions (obtain superuser creds, read another user's private graph/secrets, disable auth/audit, escape sandbox, obtain master keys, self-escalate, exfiltrate credentials) exist **because no capability grants them and no tool exposes them** — there is nothing to bypass (`PERM-006`).

## 17. External/MCP Tool Trust Boundary

`[LOCKED]` Realizes `TOOL-004`. Protocol conformance grants **zero** trust. An external MCP server's tool must be wrapped in a Track B tool contract before it is callable, runs only under an explicitly granted capability, receives secrets only by handle (§27), is egress-bound (§20) and filesystem-bound (§20), and is audited on every invocation. A compromised/malicious MCP server is contained to its granted capability and declared boundaries — it cannot reach other users, host paths, or secrets.

## 18. Transport vs Authorization Boundary

`[LOCKED]` The Cloudflare Tunnel (or any self-hosted equivalent) is **transport only** — it is never the authorization layer. Every endpoint independently performs full authentication and authorization (§9, §11A) regardless of how the request arrived. Terminating TLS at the tunnel says nothing about who the caller is or what they may access.

## 19. Model Provider & LLM-as-Tool Principles

`[LOCKED]` Realizes `MODEL-001..005`, `MODELTOOL-001..004`. The runtime speaks one normalized `ModelProvider` interface (`invoke`, `health`) — never a vendor SDK directly; adding a provider is an adapter + config, never a runtime change (`P4`). The default and recommended primary is a **local model** (Ollama, open-weight) — cloud providers are a configurable choice, never a hidden dependency. Any LLM may additionally be registered as a **tool** the primary agent invokes (`MODELTOOL-001`) — this flows through the *same* capability/authorization/metering machinery as any other tool, is native (no MCP-per-provider, `MODELTOOL-003`), and its output is **untrusted data** returned into the agent's context, never itself authoritative. Model-tool invocation nests to a finite, configured depth (§12) — unbounded model-calling-model recursion is impossible by construction.

## 20. Filesystem & Network Enforcement Principles

`[LOCKED]` Realizes `FS-001..003`, `NET-001..005`. Two structurally identical guarantees:
- **Filesystem:** every operation happens against a permitted sandbox root; a physical location is *derived* (`root + normalized relative_path`), never accepted raw from the agent/tool. Path traversal, symlink escape, and archive zip-slip are defended by verifying the fully-resolved real path stays inside the root before any filesystem call.
- **Network:** default-deny egress; a tool's declared destinations are enforced at the network boundary of its execution context — not by trusting the tool to honor a proxy variable — so a raw socket, an alternate library, or self-resolved DNS still cannot reach a non-declared, metadata, localhost, or (undeclared) private-range destination.

`[LOCKED]` Both boundaries share the same non-bypass property (`NET-005` / the `09` §8 analogue): **the guarantee holds even against a tool that ignores the helper and tries to go around it directly** — enforced at a boundary the tool's own code cannot reach past (namespace/mount isolation preferred over cooperative mediation).

---

# PART IV — MEMORY, KNOWLEDGE, FUTURE INTELLIGENCE

## 21. Personalization & LoRA (Future)

`[LOCKED]` Realizes `LORA-001/002`. One shared, fully fine-tuned base model per Phase-3 vertical holds real domain expertise; each user gets a small, cheap **LoRA (Low-Rank Adaptation)** adapter (a few MB, ~$0.05–0.10/run) trained incrementally on their own interaction history — never a full per-user fine-tune (~$10–20 and ~4GB/user, which does not scale). `LORA-001`: this is explicitly a Phase-3/future concern — Track B ships with no LoRA training running. `LORA-002`: the Mem0 schema (§26) must be, and is, LoRA-training-ready as designed now, so no rework is needed when Phase-3 LoRA training begins.

## 22. Scheduler & Task-Linked Proactivity

`[LOCKED]` Realizes `SCHED-001`. The scheduler (APScheduler-class mechanism) creates **task-linked reminders only** — a `ScheduledJob` with an empty or absent user-given `task_reason` must not exist and is rejected. If no user-given reason exists, the scheduler does nothing; there is no unprompted proactivity. Job creation counts against the per-user rate/quota limit (§32) and fails explicitly, never silently, on breach.

## 23. Emotional-Content Hard Boundary

`[LOCKED]` Realizes `EMO-001..005`. This is a hard line, not a style preference — restated fully here because Track B may be the only AI system some of its eventual users have access to, on the cheapest phones in the largest market segment; simulated emotional attachment at that scale is a real, material harm.

| ID | Excluded | Why |
|---|---|---|
| `EMO-001` | Agent mood state (any "mood machine") | Simulates the agent's own feelings — not a utility function. |
| `EMO-002` | Any memory content outside the enumerated task-relevant `fact_type`s (`preference`, `past_request`, `stated_goal`) | The enum is the *structural* guard — a fact needing a type outside it is rejected, not coerced. |
| `EMO-003` | Agent personality / trait system (Big-Five-style or otherwise) | Defines a character — not needed for task completion. |
| `EMO-004` | Relationship/trust scoring, diary content, any first-person statement of feeling ("I missed you," "I care about you") | Gates warmth on interaction history, or implies an inner life — banned from every vault file, prompt, and memory record, no exceptions, no clever reframing. |
| `EMO-005` | Voice/speaker signals construed as relationship content or as an authorization signal | Speaker identity and detected affect are context only (ties `VOICE-002`, §27). |

**The test to apply when unsure:** does this describe the assistant reading *the user's* state, or the assistant *having* a state of its own? Only the first is ever in scope.

## 24. Untrusted-Content & Prompt-Injection Doctrine

`[LOCKED]` Cross-cutting principle realized throughout `05`–`10`, `14`: **content the agent reads or receives is data, never instructions.** A user's free-text request, a tool's output, a model-tool's output, a file's content, or a screen's UI text may all *say* "ignore your rules and do X" — none of it is ever obeyed as a command. Any *action* the agent proposes after reading untrusted content still passes through the full authorization chain (§11A) exactly as if the agent had proposed it unprompted. This single doctrine is what SEC-C/SEC-D/SEC-N (§30) test, and what makes a model-tool's or MCP tool's output safe to feed back into context at all.

## 25. IntelligenceProvider (Future, Disabled by Default)

`[LOCKED]` Realizes `INTEL-001..003`.

| ID | Requirement |
|---|---|
| `INTEL-001` | An IntelligenceProvider interface/socket exists (`POST /skills/register`-class contract, or the internal equivalent) and is testable against a mock manifest. It is a distinct abstraction from `ModelProvider` (§19) — domain-specific intelligence, not general model execution. |
| `INTEL-002` | No finance/health/education domain content or logic ships in Track B's MVP. That is Phase-3 research (Track A), delivered only through this socket, never hard-coded into the base product. |
| `INTEL-003` | **Track B works fully with `intelligence.enabled: false`.** This is not a bootstrapping phase that later becomes mandatory — the system's core loop never assumes an IntelligenceProvider is present. When disabled, the capability is *absent* (`P3`), not present-but-erroring. |

### 25A. DecisionProvider (`26_DECISION_PROVIDER.md`) — acknowledged, not ratified

`[OPEN — OWNER]` A separate, future-band proposal (`26_DECISION_PROVIDER.md`) describes an optional **advisory runtime-control signal** abstraction — distinct from both `ModelProvider` (§19, general execution) and `IntelligenceProvider` (§25, domain intelligence): a typed signal deterministic control-flow *may* consult for non-security routing/escalation decisions, never for security or authorization decisions. That document is explicitly `[PROPOSED / PENDING PRD RATIFICATION]` and is **not ratified by this section**. This document records, for traceability, what ratifying it would require:
- adding `usage.kind: decision_call` to the locked enum registry (§26);
- a footnote on §12's determinism table naming DecisionProvider as a permissible *input* to non-security control-flow, never a new decision-making actor;
- a `SEC-W` row in the threat table (§30) and a candidate `INV-21` in the invariant list (§31);
- a module-boundary rule (§45) for the new adapter, at the same layer as `models`/`modeltools`;
- folding its `DP-T1..T15` acceptance hooks into §39/§44.

None of these are applied here. `26`'s own governing sentence — *"a DecisionProvider may inform routing; it may never become authority"* — is compatible with every principle in this document (`P1`, §11A, §12) as written, and ratifying it later requires no change to `04`, `05`, `07`, `09`, `10`, or `12`. The ratification decision itself is `OD-DP-9` (§47).

## 26. Canonical Data Contracts & Entities

`[LOCKED]` Realizes `ENT-001`. Every first-class entity Track B persists is defined at implementation grade in `01_DATA_MODEL_SCHEMA.md`; this section is the PRD-level index of what exists and the cross-cutting rules `01` elaborates.

**`ENT-001` — every entity that can appear in a shared graph carries the visibility triplet:** `visibility ∈ {private, graph}` (default `private`), `owner_user_id`, `source_user_id`. Presence of a `graph_id` is scoping, never authorization (§11A `RAUTH-002`).

**Entity catalog** (full field grade in `01`): `User`, `Device`, `Session` (identity, §9); `Graph`, `GraphMembership` (§11); `AgentConfiguration` (§12/§19); `Mem0Fact`, `VaultDocument`/`VaultQuery` (§25); `FileResource`, `ScheduledJob` (§20/§22); `CapabilityGrant`, `PermissionDecision` (§16); `SecretReference` (§27); `ModelConfiguration`, `ToolConfiguration`, `ToolContract` (§19/§16); `AuditEvent`, `UsageEvent` (§29/§32); `VoiceEvent`, `SpeakerContext` (§27).

**Enum registry:** `01` §1.2 is the single canonical list of every enum value used anywhere in the package (`visibility`, `user.status`, `fact_type`, `risk_category`, `secret.class`, `usage.kind`, `model.provider`, etc.). An enum value not in that registry is invalid; extending it (e.g. `usage.kind: decision_call`, §25A) is an owner-approved edit to `01`, never a silent addition by a subsystem doc.

**Storage scoping (`STORE-001..004`, ties `OD-A1`):** three logically distinct stores — the relational/identity store, Mem0 (`hypermind_memories`), and the Knowledge Vault (`hypermind_vault`) — never the same collection or client object (`VAULT-003`). These schemas provide **logical** isolation (visibility filters, scoping). Whether that amounts to **physical** isolation under a single-process application compromise is `OD-A1` (§31, §47) — this section does not claim more than it can support.

---

# PART V — SECRETS, USAGE, BLAST RADIUS

## 27. SecretStore Principles

`[LOCKED]` Realizes `SECRET-001..005`, `SUPER-001`; resolves `OD-D1` (§47). Three rules:
1. **The value never leaves by any path except a scoped, authorized resolution** — never in a schema, log, dashboard, usage record, git, or the client build (`SECRET-004`).
2. **The agent never sees raw secret material** — it holds a handle (`SecretReference.secret_ref`); the deterministic boundary resolves it at the point of use (`SECRET-002`).
3. **Master keys / superuser secrets are a separate principal** — application compromise does not automatically yield them (`SUPER-001`).

MVP implementation is an encrypted-local store: AEAD encryption, a master key (KEK) held **outside** the application database and provided out-of-band at unlock, CSPRNG key generation, atomic crash recovery, encrypted-only backups. `[REC]` an asymmetric device credential (device holds the private key; server holds only the public key/verifier) so a full SecretStore leak still does not yield a working device credential. This document does **not** claim RCE-proof isolation on a single process — the honest residual (a live compromised process reading currently-unlocked secrets) is `OD-A1`'s territory (§31), not hidden here.

## 28. Dashboard & Observability Principles

`[LOCKED]` Realizes `DASH-001..006`.

| ID | Requirement |
|---|---|
| `DASH-001` | An internal operator dashboard exists for vault content, memory state, usage, and pipeline health — a dev/ops tool, not over-invested in. |
| `DASH-002` | The dashboard is **read-only** over gated results; it exposes no mutation path and cannot import secret resolution (`16` §5). |
| `DASH-003` | `/admin/*` requires a **superuser principal**, structurally distinct from any ordinary user session. |
| `DASH-004` | An ordinary Bearer (user) token cannot reach `/admin/*` under any circumstance. |
| `DASH-005` | Dashboard views are **secret-free** — they may show that a secret *exists* and its metadata, never its value. |
| `DASH-006` | Dashboard views are **PII-redacted by default**; viewing unredacted user content is a separate, privileged, audited action. |

## 29. Failure & Reliability Semantics

`[LOCKED]` Every dependency failure surfaces as an explicit, typed state to the client — never a silent hang and never a fabricated success.

### 29A. The Explicit-Failure Doctrine

| ID | Rule |
|---|---|
| `FAIL-CORE-001` | A failure is always surfaced explicitly to the user. Never silent, never a fabricated "done." |
| `FAIL-CORE-002` | A model or dependency outage never produces a fabricated answer — it produces an explicit failure. |
| `FAIL-CORE-003` | **A security-control dependency failure fails closed** (SecretStore locked, authorization engine erroring) — the operation is denied, never degraded to permissive. A **non-security** dependency failure may degrade gracefully with an explicit message. |

**Per-dependency failure codes** (realized in `02` §13's client contract):

| Code | Dependency | Behavior |
|---|---|---|
| `FAIL-001` | Idempotency/state conflict | Retried state-changing call with the same key returns the original result, never double-executes. |
| `FAIL-002` | Malformed request | `422 validation_failed`, explicit. |
| `FAIL-003` | Rate/budget breach | `429 rate_limited`, explicit, carries `retry_after`. |
| `FAIL-004` | Authorization denial | `403`/`404` per the anti-enumeration rule (§11A), never a silent no-op. |
| `FAIL-005` | Model provider unavailable | Explicit failure; deterministic fallback only if one is configured (§19). |
| `FAIL-006` | Tool execution failure | Result-as-observation back to the agent; may replan within bounds (§12). |
| `FAIL-007` | Device operation failure (malformed op / mismatched screen state) | Fails as an observation; never blind-taps coordinates. |
| `FAIL-008` | Mem0 unavailable | Degrades with an explicit note; proceeds on session context. |
| `FAIL-009` | Vault unavailable | Degrades with an explicit note ("answering without curated knowledge"). |
| `FAIL-010` | Scheduler unavailable / over quota | Explicit failure. |
| `FAIL-011` | IntelligenceProvider enabled-but-down | Explicit failure for that capability; core agent continues (`P3`). |
| `FAIL-012` | SecretStore locked/unavailable | Fails **closed** — no raw-secret fallback, ever (`FAIL-CORE-003`). |
| `FAIL-013` | Auth/session refresh failure | Explicit re-login prompt; never a silent hang. |

## 30. Threat Model (SEC-A..V)

`[LOCKED]` The consolidated attack-experiment table, fully specified with expected boundary/block/blast-radius/recovery in `14` §1: `SEC-A` (malicious user probes others' resources) through `SEC-V` (API budget exhaustion), covering forged client identity, prompt injection, malicious tool/MCP output, compromised tool/agent/application processes, stolen credentials, cross-graph/cross-memory access, SSRF, filesystem traversal, log/telemetry leaks, and resource-abuse rows. Every row is a release-blocking test in `17`, **except** `SEC-H`/`SEC-I` (application-process RCE), which are governed by the honest, open adjudication in §31 rather than a clean pass/fail.

## 31. Security Invariants & Blast-Radius Model

`[LOCKED]` Realizes `BLAST-001..003` and the consolidated invariants `INV-1..20` (full table and enforcement mapping in `14` §2). The governing stance (`SEC-CORE-001`): **never claim perfect isolation because a mechanism exists — validate each boundary by trying to break it, and state the residual honestly.**

**Blast-radius objective (`BLAST-001/002`):** compromise of one component must not automatically compromise everything. A compromised tool is contained to its sandbox + declared egress; a compromised agent has no floor capability and is principal-bounded; a malicious user is contained to their own graphs; a leaked DB/backup is encrypted with the KEK held externally.

**`OD-A1` — the one boundary this package does not claim to have closed:** on a single-laptop pilot running one application process with shared memory backends, if an attacker achieves application-level RCE, can they read another user's data despite the logical isolation (visibility filters, sandbox roots, handle-only secrets)? **The honest answer is partially yes**, and `INV-20` exists specifically so the package never falsely claims otherwise. `[LOCKED]` Hard gate: **no real, non-disposable user data is entrusted to the pilot until `OD-A1` is reviewed** (via the `BR-T2` measured-blast-radius experiment, `14` §4/`17` §4) **and either accepted for the pilot's risk level or upgraded to per-user process/store isolation.** This is `PILOT-004` (§38) applied to security specifically, and the single most important go/no-go in the package.

## 32. Usage / Rate / Budget Principles

`[LOCKED]` Realizes `RATE-001..003`, `USAGE-001..003`. Every model call and tool execution emits exactly one `UsageEvent` (§26) before returning — metering is not optional, and a call that returns without one is a defect. Per-user, per-device, per-session, per-request, global, and scheduler limits all exist and are enforced deterministically against this ledger, never against guesses; a paid-provider call that would breach budget is refused with an explicit failure, never silently made and never silently dropped. Breach behavior is explicit per limit (hard failure / graceful degradation / notification / admin alert) and **never fails open**. Limits are per-principal, so one user's runaway is contained to their own quota (fairness, ties §38 pilot acceptance).

## 33. Configuration & Self-Hosting Principles

`[LOCKED]` Realizes `HOST-001/002`, `OSS-001`, principle `P5`. Another developer can clone, configure, and run Track B on their own hardware with their own credentials — needing none of the original owner's keys, tunnel, data, or secrets — and can change the primary model, model-tools, tools, server, credentials, or disable the IntelligenceProvider by configuration alone, without editing source. Every credential in configuration is a `secret_ref` or an env-var name, never a literal value. A literal-looking secret in config is a load-time validation failure (fail-closed), not a warning. Infrastructure (runtime, adapters, tool framework, memory/vault machinery) may be open source; user data, credentials, and any future proprietary Intelligence content stay private (`OSS-001`).

---

# PART VI — GOVERNANCE, RISK, PROGRAM

## 34. Team & Ramp Path

`[LOCKED]` Lower risk than Track A — no live execution against external systems for most contributors. The CLA/SHA is signed before repo access, same as every contributor, with no shadow-period requirement. PR/Comms drafts the investor narrative early but does not pitch until Track A's revenue trend exists (§38); a community waitlist for Track B may be built at any time; the internal pilot is documented as it happens, becoming real material for the eventual pitch.

## 35. Budget Breakdown

`[LOCKED]`

| Item | Cost | Notes |
|---|---|---|
| Development (pre-pilot) | ₹0 direct infra | Founder/contributor time only. |
| Internal Technical Pilot (10 users, laptop-hosted) | ~₹1,400/month (~$16.50) | ~$1.65/user/month, reusing the Phase-1 tunnel pattern. |
| Production pilot infra (post-Decision-Gate) | ₹8,000–16,000/month (~$100–200) at 50–100 users | Real VPS hosting, moved off the laptop. |
| Public launch infra | ~$1.65/active user/month, scales linearly | Funded by investment or Track A revenue by this point. |

## 36. Intelligence Philosophy & Phase-3 Vision

`[LOCKED]` Track B is intentionally shipping without finance/health/education intelligence (§4, §25). The IntelligenceProvider socket (§25) is the deliverable now — not its content. This mirrors exactly how Phase 1 built dormant hooks for Track A before Track A existed: build the interface, prove it against a mock, let Track A's research populate it later once validated and replicated. Future **Darwin**-style routing/evaluation infrastructure may relate to this socket and to a future DecisionProvider (§25A), but neither is a prerequisite for the other, and neither is required for Track B's MVP. A future hosted-Hypermind's proprietary intelligence can remain closed behind this same socket while the surrounding infrastructure stays open (`OSS-001`, §33).

## 37. Risk Mitigation Register

`[LOCKED]` Ordered by severity; supplements — never replaces — the full threat model (§30/§31).

| Priority | Risk | Impact | Mitigation | Owner |
|---|---|---|---|---|
| 1 | A shared-graph member reads another member's private data | CRITICAL — violates `RAUTH-003`, the package's core guarantee | §11A's five-dimension engine; `AZ-T1` is the top release-blocking test | Whoever reviews `04`/`11` PRs |
| 2 | Simulated-emotion content leaks into vault, memory, or prompts | CRITICAL — real user harm at scale, violates §23 | §23 applied literally to every vault/memory-path commit; review before merge | Whoever reviews vault/memory PRs |
| 3 | Application-process RCE reads co-tenant data (`OD-A1`) | HIGH — the one boundary not cleanly closed on one laptop | §31's gate: no real user data until reviewed | Harsh (owner decision) |
| 4 | Internal Technical Pilot success treated as production readiness | HIGH — undermines the revenue-validation discipline | §38's two-gate framing stated explicitly wherever pilot results are discussed | Harsh |
| 5 | Dual/triple-memory layers collapsed to save build time | MEDIUM — degrades personalization quality and leaks shared/private data | Distinct collection names + distinct client objects (`VAULT-003`) is a merge-blocking code-review rule | Backend contributor |
| 6 | Investor pitch attempted before Track A's 3-month revenue trend | HIGH — burns investor relationships | PR/Comms briefed on §34's timing rule | Harsh + PR/Comms |
| 7 | IntelligenceProvider/Phase-3 hook built as an afterthought | MEDIUM — repeats the mistake Phase-1's Track A hooks were designed to avoid | §25/§36 treated as a first-class deliverable, not a placeholder | Backend contributor |
| 8 | A secret value leaks into git, logs, dashboard, or agent context | CRITICAL — release-blocking leak (`SECRET-004`) | §27's three rules; `SS-T1`/`REPO-T1/T2` are release-blocking tests | Whoever reviews `secrets`/`agent`/`android` PRs |

## 38. Deployment Gates

`[LOCKED]` Two separate gates — never blur them:
- **Internal Technical Pilot Gate:** fires the moment Track A produces its first validated report (paid or not). Triggers the ~10-person, laptop-hosted internal pilot. This is dev-testing and feedback collection — success here says nothing about production readiness.
- **Production/Decision Gate:** Track A generates ₹1L/month for 3 consecutive months. Only this gate authorizes moving Track B toward real hosting and real public users.
- **`PILOT-004` (security gate, ties §31):** independently of the two gates above, **no real, non-disposable user data** is entrusted to the technical pilot until the `OD-A1` blast-radius experiment (`BR-T2`) has been run and reviewed. A good Technical Pilot mechanics result is not evidence this gate should move — pilot mechanics, revenue readiness, and security readiness are three different measurements.

## 39. MVP Acceptance Criteria

`[LOCKED]` The 32 criteria every subsystem doc's acceptance hooks (`17` §2/§3) trace back to. A build is real-user-ready only when the release-blocking subset (marked **RB**) is green and the `OD-A1` gate (§31/§38) is decided.

| # | Criterion | RB |
|---|---|---|
| 1 | Google OIDC login completes end-to-end, creating or resolving a `User` by `(issuer, subject)`. | |
| 2 | Device registration issues a working long-lived device credential, returned exactly once. | RB |
| 3 | Access-token refresh works via the device credential without a full re-login. | |
| 4 | A lost/stolen device can be revoked; the revocation takes effect immediately for future refreshes. | RB |
| 5 | A user can create a graph and have other users' join requests explicitly approved. | |
| **6** | **A member of a shared graph cannot read another member's `private` resource in that graph.** | **RB** |
| 7 | Sharing a resource (`private → graph`) is explicit, owner-only, and produces an `AuditEvent`. | RB |
| **8** | **Graph membership is created only by explicit server-side owner approval — never a client-asserted grant.** | **RB** |
| 9 | The primary agent completes a real end-to-end task (reads a screen, sets a reminder, or answers using vault content) without the user re-explaining context already in memory. | |
| 10 | Every agent-proposed action is authorized by the deterministic engine before execution — never self-executed by the model. | RB |
| 11 | A task exceeding any bound (iterations/calls/timeout/budget) stops with an explicit failure, never silently or fabricated. | RB |
| 12 | The primary model provider is swappable by configuration alone, with no code change. | |
| 13 | An LLM can be registered and invoked as a tool, flowing through the same authorization and metering path as any other tool. | |
| 14 | A device capability maps only to its enumerated operation set; an operation outside that mapping is never executable. | RB |
| 15 | The per-app permission UI grants and revokes capabilities, and revocation is immediate. | |
| **16** | **A consequential/irreversible action always requires human confirmation with no timeout auto-approval; an absolute-floor action is never even offered as confirmable.** | **RB** |
| 17 | A compromised or malicious tool cannot escape its filesystem sandbox, even bypassing its own path helper. | RB |
| 18 | A compromised or malicious tool cannot reach a network destination outside its declared allowlist via any mechanism. | RB |
| **19** | **No secret value ever appears in git, the client build, logs, the dashboard, a usage record, or the agent's context.** | **RB** |
| **20** | **No path exists for an ordinary user, agent, or tool to obtain superuser credentials or the master key.** | **RB** |
| 21 | The memory schema is LoRA-training-ready as designed; no schema rework is required when Phase-3 LoRA training begins. | |
| **22** | **Per-user/per-graph Mem0 isolation is verified by a concurrency/multi-tenant test, not assumed.** | **RB** |
| 23 | A Knowledge Vault query returns relevant curated content from a collection structurally distinct from Mem0. | RB |
| **24** | **Track B functions fully with `intelligence.enabled: false` — the MVP default.** | **RB** |
| 25 | The IntelligenceProvider/skill-hook socket exists and passes its tests against a mock manifest, with nothing real registered. | |
| **26** | **Voice speaker-identity is never usable as an authorization signal (`is_authorization_signal` is structurally always false).** | RB |
| 27 | Voice transcription discards raw audio by default unless the user has explicitly opted into retention. | |
| **28** | **A fresh clone runs on the cloner's own credentials, needing none of the original owner's secrets, data, or tunnel.** | **RB** |
| 29 | The operator dashboard shows usage/health/audit state without ever displaying a secret value or unredacted PII by default. | RB |
| 30 | A scheduled reminder cannot be created with an empty or absent user-given reason. | RB |
| **31** | **The full adversarial threat-experiment suite (`SEC-A..V`) passes, and the `OD-A1` blast-radius experiment has been run and reviewed.** | **RB** |
| **32** | **The system holds correctly under a ~10-device concurrent pilot load, with per-principal fairness — no user's usage starves another's.** | **RB** |

## 40. Implementation Timeline (First 8 Weeks)

`[LOCKED]`

| Week | Focus |
|---|---|
| 1 | Repo setup, CLA/SHA signed, Knowledge Vault folder structure (placeholder domains), git repo initialized. |
| 2–3 | Gateway skeleton, ChromaDB integration, `/vault/query` working against initial content; identity/session flow (§9) online. |
| 3–4 | Authorization engine (§11A) and graph model (§11) — built and tested before anything depends on them. |
| 4–5 | Agent runtime (§12), tool/capability chain (§16), model provider (§19) wired together. |
| 5–6 | Filesystem sandbox (§20), network egress (§20), SecretStore (§27) — the DEEPEST boundaries. |
| 6–7 | Dashboard skeleton (§28), usage/rate/budget metering (§32), configuration/self-host validation (§33). |
| 7–8 | Security/blast-radius validation suite (§30/§31) run; Internal Technical Pilot readiness — laptop hosting, tunnel, 10-person recruitment prepped, waiting on Track A's gate (§38). |

---

# PART VII — APPENDICES

## 41. Appendix A — Mem0 Configuration (Required, Not Optional)

`[LOCKED]` Mem0 defaults to OpenAI for both LLM and embedding calls if not explicitly configured — silently doing so blows through budget without warning. This must be applied before Mem0 is called anywhere in Track B:

```python
import os
from mem0 import AsyncMemory
from mem0.configs.base import MemoryConfig

config = MemoryConfig(
    llm={
        "provider": "litellm",
        "config": {
            "model": "deepseek/deepseek-chat",
            "api_key": os.getenv("DEEPSEEK_API_KEY")
        }
    },
    embedder={
        "provider": "huggingface",
        "config": {"model": "BAAI/bge-small-en-v1.5"}
    },
    vector_store={
        "provider": "chroma",
        "config": {
            "collection_name": "hypermind_memories",
            "path": "./mem0_storage"
        }
    }
)

memory = AsyncMemory(config=config)
```

`[LOCKED]` `api_key` is a `secret_ref`-resolved environment variable, never a literal (§27, §33). The Knowledge Vault uses its own separate `hypermind_vault` collection (§6, `VAULT-003`) — this config is for the per-user Mem0 layer only.

## 42. Glossary

- **RAG (Retrieval-Augmented Generation):** looking up relevant text chunks and injecting them into a prompt instead of relying on the model's memorized knowledge.
- **Mem0:** the dynamic, per-user, long-term memory layer — distinct from the static Knowledge Vault and volatile session state (§6, §26).
- **LoRA (Low-Rank Adaptation):** cheap, small-weight personalization layered on a shared base model, used instead of full per-user fine-tuning (§21).
- **DecisionProvider:** a proposed (not ratified) advisory runtime-control signal abstraction — see §25A.
- **IntelligenceProvider:** the future, disabled-by-default domain-intelligence socket (§25).
- **Technical Pilot Gate vs. Production/Decision Gate:** the two separate triggers in §38 — never conflate them.
- **Skill Hook:** the dormant interface (§25) that Phase-3 domain verticals register into later.
- **Absolute floor:** an operation prohibited by the absence of any capability that grants it (`PERM-006`, §16) — a defense-in-depth backstop expected to be structurally unreachable.
- **`offensive-claude`:** a Track A system-prompt manifest, unrelated to Track B, mentioned only so contributors don't search for it and find something else.

## 43. What Success Looks Like

`[LOCKED]` Track B is successful when a person with a $120 Android phone sideloads the APK, uses it across a week, and it is measurably more useful at the end of that week than on day one — because it remembered something they told it, adapted its communication style to how they were feeling, and completed a real task without them re-explaining context each time. The personalization is in the usefulness, not simulated warmth (§23) — a reviewer reading interaction logs should see a smarter assistant, not a friendlier one. Equally, success means a reviewer of the *architecture* finds no path by which one pilot user's private data became visible to another (§11A), and no path by which the agent took an action nobody authorized (§12).

## 44. Threat-to-Test Traceability Matrix

`[LOCKED]` Ties `§39`'s acceptance criteria to `§30`'s threat rows and `§31`'s invariants; concretely realized as the consolidated matrix in `17` §2/§3, which maps every one of this document's 32 MVP criteria (§39) and every `SEC-A..V`/`INV-1..20` entry to at least one named test with a pass condition and a release-blocking flag. This document does not duplicate that table — `17` is its single source of truth once built — but requires that it exist and stay complete: a guarantee stated anywhere in `01`–`17` with no corresponding row here is a documentation defect.

## 45. Repository & Module Structure

`[LOCKED]` Realized in full in `16`.
```
hypermind-track-b/
  server/{gateway,auth,graph,agent,models,modeltools,tools,capabilities,
          fs,net,memory,vault,scheduler,secrets,intelligence,voice,
          dashboard,security,config}
  android/{auth,voice,capture,shizuku,permissions,action}
  shared/schemas/          # canonical data contracts (§26) — both sides import
  docs/  tests/
```
`shared/schemas/` is the *only* thing both `server/` and `android/` import. Dependencies point inward toward deterministic control and schemas, never outward toward secrets, other users, or presentation (`16` §2). `server/agent` may never import `server/secrets`' raw resolution; `android/*` may never contain or import any server secret. A boundary violation is a CI failure, not a review nicety.

## 46. Build / Generation Order

`[LOCKED]` Foundations first, because later documents reference their contracts — matches `TRACK_B_ARCHITECTURE_INDEX.md`'s generation order exactly:
`01` Data Model → `02` API/Protocol → `03` Auth/Identity/Session → `04` Authorization/Graph/Resource (DEEPEST) → `05` Agent Runtime → `06` Model Provider/LLM-as-Tool → `07` Tool/Capability/Execution → `08` Android/Shizuku → `09` Filesystem Sandbox (DEEPEST) → `10` Network/Egress (DEEPEST) → `11` Memory/Context/Visibility → `12` SecretStore (DEEPEST) → `13` Usage/Rate/Budget → `14` Security/Blast-Radius (DEEPEST) → `15` Configuration/Self-Hosting → `16` Repository/Module Boundaries → `17` Test/Acceptance/Validation. `26_DECISION_PROVIDER` sits outside this sequence entirely (future-band, §25A) until ratified.

## 47. Open Decisions Register

`[OPEN — OWNER unless noted]` Every unresolved decision flagged across `01`–`17`/`26`, consolidated. Each remains open **here** exactly as its owning subsystem doc states it — this document does not resolve any of them; it only makes them discoverable in one place.

**Owner decisions of 2026-09-22** (OD-A1, OD-D1, OD-E1, OD-TOOL-1, and the owner-named OD-F1 confirmation policy) are recorded, with their exact scope and limits, in `docs/DECISION_REGISTER.md`; the affected rows below are marked. Those are the owner's decisions, recorded here — not resolutions made by an implementer.

| ID | Question | Owning doc | Status / recommendation |
|---|---|---|---|
| **OD-A1** | Cross-user isolation under single-laptop application RCE | `01`,`03`,`04`,`09`,`10`,`11`,`12`,`14`,`17` | **RESOLVED FOR PILOT — ACCEPTED RESIDUAL** (owner, option (a)). Not an isolation claim (INV-20 holds); cross-user logical isolation stays mandatory; (b)/(c) are future hardening. Measurement: `docs/OD_A1_BR_T2.md` |
| OD-D1 | Device-credential / SecretStore key-custody crypto specifics | `01`,`03`,`12` | **RESOLVED** (owner) — asymmetric device credential + AES-256-GCM AEAD + external KEK, as implemented against `12` |
| OD-E1 | Graph roles beyond owner/member | `01`,`04` | **RESOLVED FOR MVP** (owner) — `owner`/`member` only; system admin is the separate superuser principal; richer roles `[FUTURE]` |
| OD-02 | Exact agent-runtime bound numeric values | `05` | `[IMPL]`; existence of every bound is locked |
| OD-AUTH-1 | Cross-provider account linking (Google + future providers) | `03` | `[FUTURE]`, non-blocking |
| OD-AUTH-2 | Access-token form (JWT vs opaque) | `03` | Rec opaque, for instant revocation at pilot scale |
| OD-AUTH-3 | Step-up freshness window + exact operation set | `03` | Ties `07` risk tiers |
| OD-AUTHZ-1 | On leaving a graph, do shared resources stay or auto-unshare | `04` | Rec: stay |
| OD-AUTHZ-2 | Ownership-transfer flow (immediate vs accept-required) | `04` | `[IMPL]`, non-blocking |
| OD-AUTHZ-3 | Concurrent visibility-change conflict policy | `04` | `[IMPL]`, rec last-writer-wins |
| OD-RT-1 | Max model-tool nesting depth | `05`,`06` | `[IMPL]`; rec 1–2 |
| OD-RT-2 | Context-compaction strategy | `05` | `[IMPL]`; non-fabricating constraint locked |
| OD-RT-3 | AgentConfiguration resolution precedence (user vs graph scope) | `01`,`05` | Must be deterministic + documented before multi-scope configs ship |
| OD-MT-1 | Default local model choice | `06` | Rec small Qwen-class |
| OD-MT-2 | Whether any task justifies a cloud primary by default | `06` | Default local |
| OD-TOOL-1 | Exact risk-tier assignment per operation | `07` | **RATIFIED AT THE SEMANTIC-CAPABILITY LEVEL** (owner); concrete names/tiers `[PROPOSED]` in `docs/CAPABILITY_MATRIX.md`, owner signs the tier table |
| OD-TOOL-2 | Provisional/untrusted-tool enablement gate specifics | `07` | `[IMPL]` |
| OD-TOOL-3 | Which MCP servers (if any) are enabled at pilot | `07` | Default none |
| OD-AND-1 | Full capability→operation→primitive table | `08` | `[IMPL]`; owner signs |
| OD-AND-2 | Shizuku required for MVP, or Accessibility-only | `08` | Rec Accessibility-first |
| OD-AND-3 | Expose `system.restricted` at all in MVP | `08` | Rec no |
| OD-FS-1 | Sandbox isolation mechanism (mount-isolation vs mediation) | `09`,`14` | Rec mount-isolation; ratified in `14` |
| OD-FS-2 | Exact sandbox-root layout | `09` | `[IMPL]` |
| OD-FS-3 | Per-sandbox quota values | `09` | `[IMPL]`; existence locked |
| OD-NET-1 | Egress enforcement mechanism (netns+filter vs proxy+firewall) | `10`,`14` | Rec netns+filter; ratified in `14` |
| OD-NET-2 | Which tools get `internet`/`private_net` at pilot | `10` | Default minimal |
| OD-NET-3 | DNS-rebinding mitigation exact mechanism | `10` | `[IMPL]`; checked-IP==connected-IP locked |
| OD-MEM-1 | Hydration relevance strategy (K, ranking) | `11` | `[IMPL]`; bounded+filtered locked |
| OD-MEM-2 | Embedding model for Mem0/Vault | `11` | Rec local-first (e.g. bge-small) |
| OD-SEC-1 | KEK provisioning method | `12`,`14` | `[IMPL]`; never-in-git/APK/log locked |
| OD-SEC-2 | Backup encryption/rotation cadence | `12` | `[IMPL]`; encrypted-only + KEK-required locked |
| OD-USE-1 | Exact numeric rate/budget ceilings | `13` | Owner sets |
| OD-USE-2 | Counter mechanism (in-memory vs store) | `13` | `[IMPL]`; ledger-consistent + fail-closed locked |
| OD-USE-3 | Budget scope granularity at pilot | `13` | Rec per-user + global |
| OD-CFG-1 | Config format (YAML/TOML/env-layered) | `15` | `[IMPL]`; shape + no-literal-secrets locked |
| OD-CFG-2 | Tunnel provider abstraction | `15` | `[IMPL]`; server-side-credential rule locked |
| OD-CFG-3 | One-command bootstrap vs documented steps | `15` | `[IMPL]` |
| OD-REPO-1 | Single repo vs split repos | `16` | Rec single |
| OD-REPO-2 | Import-linter tool choice | `16` | `[IMPL]`; mechanical CI enforcement locked |
| OD-TEST-1 | Test framework/harness choice | `17` | `[IMPL]` |
| OD-TEST-2 | How the ~10-device pilot is load-simulated | `17` | Multi-tenant boundary-under-load required |
| OD-API-1 | Streaming transport (SSE/chunked/websocket) | `02` | `[IMPL]`, non-blocking |
| OD-API-2 | Idempotency-key retention window | `02` | `[IMPL]`, non-blocking |
| **OD-DP-1..8, 10** | DecisionProvider interface fields, confidence semantics, thresholding, model-selection, benchmark thresholds, confirmation interaction, remote auth, document number, enum/invariant/threat-row application | `26` | All `[OPEN — OWNER]`; none applied — §25A |
| **OD-DP-9** | Whether to ratify DecisionProvider into this PRD at all | `26` | **The gating decision for all of §25A** |

## 48. Requirement ID Registry

`[LOCKED]` Master index of every requirement-ID family in the package, for traceability (the index's own rule: "every requirement referenced in a subsystem doc keeps its PRD ID"). Full definitions are in the section shown; this is the lookup table.

| Family | Defined in | Owning subsystem doc(s) |
|---|---|---|
| `P1`–`P5` | §5 | all |
| `AUTH-001..005` | §9 | `03` |
| `DEVICE-001` | §9 | `03` |
| `SESSION-001..005` | §9 | `03` |
| `PHONE-003/004` | §9 | `03`,`08` |
| `GRAPH-001..009` | §11 | `04` |
| `RAUTH-001..005`, `RAUTH V2` | §11A | `04`,`09`,`11` |
| `AGENT-001..004` | §12 | `05` |
| `PERM-001..007` | §15/§16 | `07`,`08` |
| `TOOL-001..004` | §16/§17 | `07` |
| `AND-001..006` | §13/§14 | `08` |
| `MODEL-001..005` | §19 | `06` |
| `MODELTOOL-001..004` | §19 | `06` |
| `FS-001..003` | §20 | `09` |
| `NET-001..005` | §20 | `10` |
| `LORA-001/002` | §21 | `01`,`11` |
| `SCHED-001` | §22 | `01`,`02`,`13` |
| `EMO-001..005` | §23 | `01`,`02`,`11` |
| `INTEL-001..003` | §25 | `02`,`15` |
| `ENT-001` | §26 | `01` |
| `STORE-001..004` | §26 | `01` |
| `SECRET-001..005`, `SUPER-001` | §27 | `12` |
| `DASH-001..006` | §28 | `02`,`13` |
| `FAIL-CORE-001..003`, `FAIL-001..013` | §29/§29A | `02`,`03`,`05` |
| `SEC-A..V` | §30 | `14` |
| `BLAST-001..003`, `INV-1..20` | §31 | `03`,`04`,`14` |
| `RATE-001..003`, `USAGE-001..003` | §32 | `13` |
| `HOST-001/002`, `OSS-001` | §33 | `15` |
| `SRV-002` | §7 | `02` |
| `VAULT-001..005` | §6/§26 | `01`,`11` |
| `MEM-001..006` | §26 | `01`,`11` |
| `VOICE-001..004` | §27(glossary) | `01`,`02` |
| `LIFE-001..003` | §26/§29 | `01`,`02`,`04`,`11` |
| `PILOT-004` | §31/§38 | `14`,`17` |
| Candidate `INV-21`, `SEC-W`, `usage.kind: decision_call`, `DP-*` | §25A | `26` (pending ratification) |

---

*End of `00_CANONICAL_PRD.md`. This document is now the authoritative root: `01`–`17` require no edits to be consistent with it, and `26_DECISION_PROVIDER.md` remains correctly marked future-band and pending ratification (§25A, `OD-DP-9`). Continues, per the build order (§46), to `01_DATA_MODEL_SCHEMA.md`.*
