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

## Current state: `runtime` branch

Three branches are in:

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

See `docs/RUNNING_SECURITY_CORE.md` to run it.

**`runtime`** — the propose→authorize→execute loop (`05`), never itself a
security authority:

- **Agent runtime** (`server/agent`): task lifecycle, deterministic context
  assembly, deterministic proposal parsing (a model's raw output is data,
  never free-text authorization), and the bounded loop — every ceiling 05 §3
  requires (iterations, tool calls, model calls, model-tool nesting depth,
  wall-clock timeout, budget) enforced by the runtime, breach always an
  explicit failure, never a silent stop.
- **Tool registry & dispatch** (`server/tools`): the registered-contract /
  enabled-configuration chain (07 §1) up to the point of calling a concrete
  executor — no filesystem/network/device primitive is implemented here.
- **ModelProvider & LLM-as-a-Tool** (`server/models`, `server/modeltools`):
  one normalized invoke interface (06 §1), a local-first Ollama adapter, and
  model-tools flowing through the *same* capability-gated tool machinery as
  any other tool.
- **Memory hydration interface** (`server/memory`): the port `11`'s
  visibility-filtered retrieval will implement; today an honest no-op, since
  no branch has built Mem0 yet.

Structurally isolated from Security Core: `server/agent` cannot import
`server.graph`, `server.capabilities`, `server.secrets`, `server.storage`, or
`server.gateway` (import-linter, mechanically enforced) — every authorization,
persistence, and confirmation capability it needs arrives as a
constructor-injected port (`server/agent/ports.py`), built by the gateway
composition root (`server/gateway/runtime.py`) from the real Security Core
objects. The model can propose; it cannot reach the engine.

**There is still no filesystem sandbox, network egress enforcement, or
Android integration** — those are the Execution branch's job (`08`/`09`/`10`),
and the runtime consumes their eventual interfaces rather than re-deriving
them.

### Before putting real data anywhere near this

`docs/OD_A1_BR_T2.md` records the **measured** blast radius under simulated
application-level RCE (BR-T2). The OD-A1 gate is **closed**: the pilot is
cleared for disposable/test data only until the owner reviews that
measurement. Passing unit tests do not imply real-user readiness.

## Repository layout

```
server/    the modular-monolith FastAPI application (one package per subsystem)
shared/    schemas/  — canonical Pydantic data contracts, importable by both
                        server/ and a future android/ client
tests/     pytest suite (tests/foundation/, tests/security_core/, tests/runtime/)
docs/      RUNNING_FOUNDATION.md, RUNNING_SECURITY_CORE.md, OD_A1_BR_T2.md
Working Markdown/   the architecture/PRD document package (source of truth)
```

The two halves of the authorization engine are deliberately independent
modules that never import each other (`16` §5): `server/graph` decides, and
`server/capabilities` supplies the policy it consults, meeting only at the
gateway composition root through the Protocols in `server/graph/ports.py`.
Keeping them apart is what stops the half that evaluates a capability check
from also being able to grant one.

Module boundaries (who may import whom) are enforced mechanically via
`import-linter` — see `pyproject.toml`'s `[tool.importlinter]` section — and
`.github/workflows/ci.yml` runs that check, the test suite, the migration
round-trip, and the BR-T2 measurement on every pull request. A boundary
violation is a CI failure, not a review nicety (`16` §6). CI requires no
secrets of any kind.
