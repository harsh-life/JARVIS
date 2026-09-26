# JARVIS / Hypermind Track B

A configurable, self-hostable personal-agent runtime. See
`Working Markdown/00_CANONICAL_PRD.md` for the full product and architecture
specification, and `Working Markdown/TRACK_B_ARCHITECTURE_INDEX.md` for how
the 17 subsystem documents relate to it.

The central invariant every branch of this codebase is built to preserve:

```
AGENT PROPOSES
    ↓
DETERMINISTIC INFRASTRUCTURE AUTHORIZES
    ↓
TOOLS EXECUTE
    ↓
HUMAN CONFIRMS WHERE REQUIRED
```

Model output is never the security boundary.

## Current state: the memory build

Five branches, the runtime foundation (U0–U6), and the memory build are in:

**`foundation`** — the technical substrate: shared data contracts,
configuration, persistence/migrations, and the API skeleton (versioning,
request IDs, the error envelope, a health check). See
`docs/RUNNING_FOUNDATION.md`.

**`security-core`** — the deterministic security authority every later
branch must pass through:

- **Authentication** (`03`): Google OIDC with issuer/audience/state/nonce/PKCE
  validation, subject-keyed identity, Ed25519 device credentials with
  rotation/revocation/replay protection, opaque access tokens with immediate
  revocation, and step-up on sensitive operations.
- **Authorization** (`04`): the five-dimension engine — membership, role,
  ownership, visibility, capability — as the single place resource access is
  decided, with anti-enumeration surfaces and fail-closed behaviour.
- **Capabilities, risk tiers, the absolute floor, confirmation tokens** (`07`):
  a closed capability registry with enumerated operations, a deterministic
  tier table, prohibition by absence, and confirmations bound to one exact
  action.
- **SecretStore** (`12`): AES-256-GCM under an external KEK, handle-only
  access, master-key/superuser separation, and fail-closed resolution.
- **Audit primitives** (`01` §11.1) on every security-sensitive operation.

**`runtime`** — the deterministic agent runtime around that authority:

- **The loop** (`05`): propose → parse → authorize → confirm → execute →
  observe, under hard ceilings (iterations, model/tool calls, nesting, wall
  clock, budget, concurrency), each ending in an explicit failure.
- **Model providers** (`06`): one normalized interface; local Ollama by default;
  OpenAI-compatible adapters with keys resolved by handle at call time;
  LLM-as-a-tool through the same capability, authorization, and metering path.
- **Tools** (`07`): a registry that asserts every contract against the closed
  capability registry, per-platform adapters, and on-demand capability
  activation scoped to one task.
- **Usage, rate, budget** (`13`): one ledger; rates and budgets derived from it;
  fail-closed.
- **Memory hydration** (`11` §3): visibility pushed into the store query and
  re-checked with the engine's own predicate.

**`execution`** — the mechanism through which an authorized tool operation
becomes an actual operation on a target platform (`server/execution/`'s module
docstring: "Runtime owns orchestration. Security Core owns authorization.
Execution owns constrained execution."):

- **Filesystem sandbox** (`09`): real, `dir_fd`-walking, `O_NOFOLLOW`-at-every-hop
  path containment (not a `path.startswith(root)` check — see
  `server/fs/paths.py`), zip/tar-slip-safe archive extraction, per-file and
  per-sandbox size caps, least-privilege file modes.
- **Network egress** (`10`): default-deny, per-tool destination allow-listing,
  resolved-IP classification (metadata/loopback always blocked; private ranges
  only with explicit policy) before every connection, and a checked-IP-is-the-
  connected-IP guarantee against DNS rebinding (`server/net/client.py`).
- **Process execution** (`system.restricted`, `08` §6): `argv`-only (never a
  shell), a closed-by-default executable allow-list, a from-scratch
  environment, POSIX resource limits, and whole-process-group cleanup on
  timeout (`server/execution/process.py`).
- **Android/Shizuku** (`08`), server-side half: the capability→operation→
  primitive mapping and dispatch contract, with `UnavailableDeviceTransport`
  as the only shipped transport — every call fails deterministically until a
  real device channel is wired in (`android/` does not exist in this
  repository yet).

**`integration-hardening`** — a review of the composed system against the
canonical PRD and the locked decisions, fixing what only shows once the layers
meet: kernel confinement (Landlock + seccomp) for `system.restricted`, failing
closed where unavailable; graph admission only from a pending access request;
per-user idempotency namespaces and no confirmation token in stored replays;
stored model configs limited to SecretStore handles of class `model_api_key`;
suspended users stopped mid-task; tool calls bound to the authorizing device;
cancellable running tools; egress deadline/chunk bounds; per-principal fs
quotas. End-to-end acceptance cases live in `tests/integration/`, and BR-T2 was
re-run for the execution dimensions (`docs/OD_A1_BR_T2.md` §3b). Decisions are
in `docs/DECISION_REGISTER.md` §2B.

### Boundaries this codebase keeps distinct

- **Authentication ≠ authorization.** A valid token identifies a principal; it
  grants nothing. Every resource access still goes through the engine.
- **Capability ≠ tool ≠ adapter.** A capability is a closed-registry permission;
  a tool is a contract that declares which capability it needs; an adapter is
  the code that runs it. Registering an adapter grants no capability.
- **Model ≠ authority.** Model output is a proposal, parsed and authorized like
  any untrusted input.
- **Runtime ≠ execution.** The runtime orchestrates; execution performs the
  already-authorized operation under its own constraints.
- **Device ≠ trust root.** A phone is an execution target bound to a principal,
  not a source of authority.
- **SecretStore ≠ model context.** Secrets are resolved by handle at call time
  and never enter a prompt or an observation.
- **Sandbox ≠ path check.** Filesystem containment walks `dir_fd`s with
  `O_NOFOLLOW`; `system.restricted` gets a kernel ruleset, not a prefix check.
- **Logical isolation ≠ process isolation.** All users share one server
  process. Code running inside it reaches what it can reach — the accepted
  OD-A1 (a) residual. Nothing here is a claim of isolation under
  application-level RCE, and the build is not production-ready.

The owner's decisions (OD-A1, OD-D1, OD-E1, OD-F1, OD-TOOL-1) are recorded in
`docs/DECISION_REGISTER.md`; the capability/risk/confirmation matrix is
`docs/CAPABILITY_MATRIX.md`. See `docs/RUNNING_RUNTIME.md` and
`docs/RUNNING_EXECUTION.md` to run it.

**Memory build** — persistent memory and the Knowledge Vault (`11`,
`docs/21_MEMORY_PROVIDER_VAULT.md`; operator guide `docs/RUNNING_MEMORY.md`):

- **`MemoryProvider`** (`server/memory/provider.py`): the one interface JARVIS
  depends on. Providers store and retrieve; the authorization engine decides —
  `mem0fact` operations go through the same five-dimension engine as every other
  resource, and hydration re-checks every result with its `readable()` predicate.
- **Mem0 OSS, self-hosted, as a library** (`server/memory/mem0_provider.py`,
  pinned `mem0ai==2.2.1`, not forked): telemetry off, no Mem0 model calls
  (writes use no-inference mode; extraction, when enabled, is JARVIS's own
  metered call), an offline local embedder, no history file, and deletion that
  removes the text from the store files.
- **A deterministic write gate** (`server/memory/gate.py`): typed facts only; no
  secrets, emotional/relationship content, tool observations or payloads.
- **The Knowledge Vault** (`server/vault/`): Git-backed markdown, indexed from the
  committed tree into its own Chroma client and directory; no HTTP write path.

Disabled by default (`memory.enabled`, `vault.enabled`); the stack is the
optional `memory` extra.

**Still not built:** the scheduler, the dashboard, voice, and a real Android
client (`android/`). Their capabilities (where any exist) are absent from the
registry or, for Android, dispatch to a transport that refuses every call — the
runtime and execution layer both refuse rather than run unbounded, not silently
degrade.

### Before putting real data anywhere near this

`docs/OD_A1_BR_T2.md` records the **measured** blast radius under simulated
application-level RCE (BR-T2). The owner decided OD-A1 as **RESOLVED FOR PILOT —
ACCEPTED RESIDUAL** (option (a)): logical isolation, with the measured in-process
residual accepted for the pilot. That is not an isolation claim. Real-user
readiness additionally needs 17 §5's full release-blocking set, which includes
the `08` suites that do not exist yet. The memory suites (MEM-T1 on a real Mem0
store, and the BR-T2 memory re-run) now exist and run in CI; BR-T2's at-rest
memory rows need an owner decision (`docs/OD_A1_BR_T2.md` §3c/§5).

## Repository layout

```
server/    the modular-monolith FastAPI application (one package per subsystem)
shared/    schemas/  — canonical Pydantic data contracts, importable by both
                        server/ and a future android/ client
tests/     pytest suite (tests/foundation/, tests/security_core/, tests/runtime/,
                          tests/execution/, tests/integration/, tests/memory/)
docs/      RUNNING_*.md, OD_A1_BR_T2.md, CAPABILITY_MATRIX.md, DECISION_REGISTER.md
Working Markdown/   the architecture/PRD document package (source of truth)
```

The two halves of the authorization engine are deliberately independent
modules that never import each other (`16` §5): `server/graph` decides, and
`server/capabilities` supplies the policy it consults, meeting only at the
gateway composition root through the Protocols in `server/graph/ports.py`.
Keeping them apart is what stops the half that evaluates a capability check
from also being able to grant one.

The agent runtime (`server/agent`) is kept further still: it cannot import the
engine, the capability package, or the SecretStore at all. It reaches them only
through the Protocols in `server/agent/ports.py`, which the top-level
composition root (`server/composition/`) satisfies with the Security Core's own
objects — so there is no second authorization path to drift.

The execution layer (`server/execution`, `server/fs`, `server/net`) sits below
`graph`/`capabilities` in the same dependency graph and cannot import either —
it receives only an already-authorized `ExecutionRequest`
(`shared/schemas/execution.py`) and has no field, method, or import path that
could assert authority for itself. `server/tools`/`server/modeltools`/
`server/agent` are additionally barred from importing `socket`/`subprocess`
directly, so a tool adapter cannot open a raw connection or process that
bypasses `server.fs`/`server.net`/`server.execution` — see
`docs/RUNNING_EXECUTION.md` §3 for exactly what that guarantee does and does
not cover.

Module boundaries (who may import whom) are enforced mechanically via
`import-linter` — see `pyproject.toml`'s `[tool.importlinter]` section — and
`.github/workflows/ci.yml` runs that check, the test suite, the migration
round-trip, and the BR-T2 measurement on every pull request. A boundary
violation is a CI failure, not a review nicety (`16` §6). CI requires no
secrets of any kind.
