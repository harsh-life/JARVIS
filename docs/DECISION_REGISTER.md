# Decision Register — owner decisions and runtime-branch proposals

**Authority order** (owner instruction, 2026-09-22):

```
canonical PRD (Working Markdown/00_CANONICAL_PRD.md)
  > locked decision register (this file, owner rows)
  > subsystem contracts (01–17)
  > implementation
  > proposals
```

`00_CANONICAL_PRD.md` §47 is the index of every open decision. This file records
**how** each one the owner has now decided was decided, and lists the `[PROPOSED]`
values the runtime branch had to choose. A `[PROPOSED]` row is *not* a lock; it
stands until the owner ratifies or changes it.

The PRD's canonical file name in this repository is
`Working Markdown/00_CANONICAL_PRD.md`. `TRACK_B_ARCHITECTURE_INDEX.md` refers to
the same document as `Track_B_PRD_Refined.md`.

---

## 1. Owner decisions (2026-09-22)

### OD-A1 — cross-user isolation under single-laptop application RCE

**Status: RESOLVED FOR PILOT — ACCEPTED RESIDUAL** · option **(a)** of 14 §4.

- The owner explicitly accepts, for the controlled pilot, the current
  **logical-isolation** architecture and its measured residual blast radius
  (`docs/OD_A1_BR_T2.md` §3): a compromised live application process may reach data
  and secrets that are already available to that running process.
- This is a **risk acceptance, not an isolation claim.** Logical isolation ≠
  physical process/store isolation under RCE. INV-20 still holds: the package does
  not claim cross-user isolation under application RCE, and the BR-T2 experiment
  keeps asserting the reachable rows *stay* reachable so the residual cannot quietly
  turn into a false claim.
- **Not weakened by this decision:** cross-user logical isolation is mandatory.
  The five-dimension engine, the visibility predicate, anti-enumeration, and
  handle-only secrets are unchanged, and every one of their tests still runs.
- **Intentional:** one user's multiple devices share that user's authorized state
  (tasks, grants, memory, configuration). A different user's private state stays
  isolated.
- **Deferred, not rejected:** (b) per-user process isolation and (c) per-user store
  isolation remain future hardening for a hostile-hosting or larger multi-tenant
  deployment. Not implemented in this branch.
- **What this does and does not open.** PILOT-004's OD-A1 gate is decided. Real
  user data still requires 17 §5's *whole* release-blocking set to be green, and
  that set includes suites whose subsystems do not exist yet (`09` FS-T9, `10`
  NET-T2, `11` MEM-T1 on a real Mem0 store, `08` AND-T*). Those are separate gates,
  not OD-A1. BR-T2 is re-run when `09`/`11` land; a reachable row *outside* the
  accepted class would go back to the owner.

### OD-D1 — SecretStore key custody / crypto specifics

**Status: RESOLVED** — as implemented in `server/secrets/` against `12`:
AES-256-GCM AEAD with the handle as AAD, DEK sealed under an external KEK supplied
out of band, handle-only agent access (the agent requester is refused
unconditionally), a separate superuser/master-key boundary, rotation, revocation,
and fail-closed resolution. Asymmetric (Ed25519) device credentials. Not reopened.

SecretStore key custody and persistent-memory encryption are **separate
boundaries**. Memory encryption is recorded as future hardening (§4), not folded
into OD-D1.

### OD-E1 — graph roles beyond owner/member

**Status: RESOLVED FOR MVP** — `membership.role ∈ {owner, member}`. No graph-level
RBAC beyond that. A system Owner/Admin is a separate principal (the superuser,
12 §4) and is not a graph role. User, device, session, graph, graph owner, graph
member, resource ownership, visibility, capability, authorization, and system
administrator stay distinct concepts. Richer roles later extend the enum without a
schema break; `_role_permits` already denies an unrecognised role.

### OD-F1 — confirmation policy

**Status: RESOLVED (policy)** · concrete mapping `[PROPOSED]` in
`docs/CAPABILITY_MATRIX.md` §2–§3.

Four tiers plus the absolute floor, exactly PRD §15: `low_read` automatic,
`low_write` automatic inside the activated boundary, `consequential` explicit
confirmation, `high_irreversible` strong confirmation (confirmation + step-up),
floor never. Confirmation sits at meaningful risk boundaries, not on every
primitive. No timeout auto-approves. The model never decides whether confirmation
is needed. Financial / account-security actions keep conventional controls:
explicit operation details, deterministic authorization, action-bound single-use
tokens with expiry (transaction binding + replay protection), step-up, scoped
credentials, audit, fail-closed.

(OD-F1 is the owner's identifier for this policy; PRD §47 tracks the same ground
under OD-TOOL-1's tier table and OD-AUTH-3's step-up window.)

### OD-TOOL-1 — exact risk-tier assignment per operation

**Status: RATIFIED AT THE SEMANTIC-CAPABILITY LEVEL** — capabilities are broad
semantic authorization classes the agent composes within; they are not command
menus. Concrete names are derived from the repository and documented in
`docs/CAPABILITY_MATRIX.md`. The per-operation tier table and the reserved floor
names remain `[PROPOSED]` until the owner signs them.

---

## 2. Runtime-branch proposals (`[PROPOSED]`, pending ratification)

| ID | Decision needed | Proposed value | Where |
|---|---|---|---|
| OD-02 | Agent-runtime bound values | `max_iterations` 12 · `max_model_calls` 16 · `max_tool_calls` 24 · `max_model_tool_nesting_depth` 1 · `wall_clock_timeout_seconds` 120 · `per_task_budget` 0.0 (paid calls refused unless raised) · `max_parse_retries` 2 · `max_observation_chars` 4000 | `agent.bounds` in config |
| OD-RT-1 | Max model-tool nesting depth | **1** — a model-tool is single-shot and cannot itself propose tool calls | `agent.bounds.max_model_tool_nesting_depth` |
| OD-RT-2 | Context compaction | Deterministic: keep the system prompt, the hydrated context, the user input, and the newest observations within `max_context_chars`; older observations are *dropped* and replaced by a fixed marker. No summarization, so nothing can be fabricated (RT-T9). | `server/agent/context.py` |
| OD-RT-3 | AgentConfiguration precedence | **user scope → graph scope → server config default**, first match wins. A user's model choice (and its cost) is theirs; a graph owner cannot switch another member's model. | `server/composition/models.py` |
| OD-USE-1 | Numeric rate/budget ceilings | existing `security.rate_limits` (60/min per user and per device, applied to metered calls) and `security.budgets` (0.0 — paid calls refused by default), plus concurrency: 1 task per session, 2 per user, 8 global; 600 metered calls/min globally | config |
| OD-USE-2 | Counter mechanism | Rates and budgets are **derived from the `usage_events` ledger** by query (USAGE-002); concurrency is an in-process gate (single-process pilot). A ledger read that fails is treated as at-limit (fail-closed). | `server/security/usage.py` |
| OD-USE-3 | Budget scope | per-user + global (13 §8's recommendation) | `server/security/usage.py` |
| (new) | Agent task record | `agent_tasks` table holds lifecycle only (owner, status, counters, final response or failure code). The working transcript and the paused action are **volatile** and never persisted (MEM-001); a restart fails a paused task closed. | migration `…_agent_runtime` |
| (new) | Resource-less tool operations | `ResourceType.TOOL_ACTION` + `Operation.CREATE`: authorized on capability, floor and tier alone; not loadable, so nothing can be read through it. Tools that touch a real resource (e.g. a `FileResource`) declare that resource type instead and get D3/D4. | `shared/schemas/authorization.py` |
| (new) | Model pricing | A non-local provider must declare `pricing`; a paid provider without pricing is a config error, because an unknown cost cannot be checked against a budget. | `server/config/schema.py` |
| (new) | Execution platforms | `server`, `linux`, `android` — engine vocabulary, not a `01` §1.2 registry value | `shared/schemas/agent.py` |

---

## 2A. Execution-branch proposals (`[PROPOSED]`, pending ratification)

| ID | Decision needed | Proposed value | Where |
|---|---|---|---|
| OD-FS-1 | Filesystem containment mechanism (09 §8) | **`mediated`** — real, `dir_fd`-walking, `O_NOFOLLOW`-at-every-hop path resolution, TOCTOU-resistant by construction (the containment check *is* the syscall that does the work, not a separate stat performed earlier). `09` §8's `[REC]` `mount_isolated` mode (mount-namespace isolation) is not implemented — this branch has no privilege to create one in its development environment. `FilesystemSandbox` refuses to start if configured for `mount_isolated` rather than silently run the weaker mode under that name. | `server/fs/paths.py`, `server/config/schema.py`'s `FilesystemSandboxConfig.containment_mode` |
| OD-NET-1 | Network egress enforcement mechanism (10 §3) | **`mediated_proxy`** — a hand-built HTTP/1.1 client that resolves, classifies every candidate IP, and connects only to the checked IP (DNS-rebinding defense), rather than `netns_filtered` (kernel-level, `[REC]`). Same refuse-rather-than-misrepresent posture as OD-FS-1. | `server/net/client.py`, `NetworkEgressConfig.enforcement_mode` |
| OD-NET-3 | DNS-rebinding mitigation mechanism | Resolve once, classify every candidate address, connect to the exact IP that passed classification — never a second, independent resolution between check and connect. | `server/net/client.py` |
| (new) | `sandbox_root` scope-value semantics | Treated as an opaque **label** the operation is authorized against, never a filesystem path — even though 09 §1's own illustrative example writes it as one (`"sandbox_root": "/…"`). The physical root is always derived from the request's own authorized `user_id`/`graph_id`; the label only selects a sub-sandbox *within* that principal's own area. Accepting the scope value as a literal path would be exactly the "raw path from a client" 09 §1 forbids. | `server/fs/paths.py::allocate_root`, `server/fs/__init__.py` |
| (new) | fs operation addressing | `file.read`/`file.write` operations are authorized as resource-less `TOOL_ACTION`s (capability + tier + `resource_scope`), addressed by `sandbox_root` label + `relative_path` — not by a `FileResource.resource_ref`. Wiring individual-`FileResource` D3/D4 visibility into a physical read needs a DB session no `ToolAdapter.execute` receives; that integration is left to `11`/a future branch, not invented here. | `server/tools/platforms.py` |
| (new) | `net.request` capability | Added to the closed registry (`get` → low_read, `post` → consequential) now that `10`'s egress boundary exists to back it — `docs/CAPABILITY_MATRIX.md` §3.2 had withheld it for exactly this reason. Registered with a **closed-by-default** `EgressPolicy` (`execution.network.default_*` all empty/false); an operator opts a destination in. | `server/capabilities/registry.py`, `server/composition/execution_tools.py` |
| (new) | `build_application`'s default tool set | `extra_tools=None` (production's default) now builds the real execution tools from `ExecutionConfig` rather than none at all — `file.read`/`file.write` become immediately usable once granted; `net.request`/`system.shell` register but stay inert (empty destination/executable allow-lists); the Android tools use `UnavailableDeviceTransport`. An explicit `extra_tools` list, as every test passes, still fully substitutes. | `server/composition/__init__.py`, `server/composition/execution_tools.py` |
| (new) | `execution` module-boundary layer | Inserted between `graph \| capabilities` and `net \| fs` in the layering contract; `server.tools`/`server.modeltools`/`server.agent` are additionally barred from importing `subprocess`/`socket` directly (direct-import check only — `allow_indirect_imports = true`, since the legitimate dependency on `server.net`/`server.execution.process` necessarily uses them transitively). | `pyproject.toml`'s `[tool.importlinter]` |

**OD-A1 note.** The owner's `docs/OD_A1_BR_T2.md` decision already anticipated this: "BR-T2 is re-run when `09`/`11` land." `09` has now landed; `11` (Mem0) has not. Re-running BR-T2 against the real filesystem sandbox, and any adjudication of a newly-reachable row outside the previously accepted class, is separate follow-up work this branch does not itself perform — OD-A1's pilot-acceptance decision (§1 above) is unchanged by this branch, not reopened by it.

---

## 3. Genuinely unresolved owner decisions

| ID | Question | Why it is the owner's |
|---|---|---|
| (matrix §5.1) | Sensitive-app classification for UI primitives | A tap can complete a payment. Which apps are "sensitive" and how far their tiers rise is a product-risk call. Needed before `08` ships device control. |
| OD-TOOL-3 | Which MCP servers (if any) are enabled at pilot | Default none; unchanged. |
| OD-DP-9 | Ratify DecisionProvider at all | Unchanged; nothing in the runtime depends on it. |
| OD-MT-2 | Any cloud primary by default | Default local (ollama); unchanged. |

---

## 4. Future hardening (recorded, not in scope)

- Per-user process isolation (OD-A1 option b) and per-user data-store isolation
  (option c).
- Per-user / per-graph key separation for persistent memory at rest. This protects
  stored ciphertext against storage and backup compromise; it does **not** protect
  against a compromised live process that is legitimately using the plaintext.
- A separate model-service process holding no auth DB, SecretStore, or vector store.
  Today the model adapter runs in the gateway process and is handed only the
  authorized, hydrated context for the task.
