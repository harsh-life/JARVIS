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

The owner's decisions (OD-A1, OD-D1, OD-E1, OD-F1, OD-TOOL-1) are recorded in
`docs/DECISION_REGISTER.md`; the capability/risk/confirmation matrix is
`docs/CAPABILITY_MATRIX.md`. See `docs/RUNNING_RUNTIME.md` to run it.

**Still not built:** filesystem sandbox (`09`), network egress (`10`), Mem0
(`11`), Android integration (`08`). Their capabilities exist, but no adapter does,
so the runtime refuses them rather than running them unbounded.

### Before putting real data anywhere near this

`docs/OD_A1_BR_T2.md` records the **measured** blast radius under simulated
application-level RCE (BR-T2). The owner decided OD-A1 as **RESOLVED FOR PILOT —
ACCEPTED RESIDUAL** (option (a)): logical isolation, with the measured in-process
residual accepted for the pilot. That is not an isolation claim. Real-user
readiness additionally needs 17 §5's full release-blocking set, which includes
the `09`/`10`/`11`/`08` suites that do not exist yet.

## Repository layout

```
server/    the modular-monolith FastAPI application (one package per subsystem)
shared/    schemas/  — canonical Pydantic data contracts, importable by both
                        server/ and a future android/ client
tests/     pytest suite (tests/foundation/, tests/security_core/, tests/runtime/)
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

Module boundaries (who may import whom) are enforced mechanically via
`import-linter` — see `pyproject.toml`'s `[tool.importlinter]` section — and
`.github/workflows/ci.yml` runs that check, the test suite, the migration
round-trip, and the BR-T2 measurement on every pull request. A boundary
violation is a CI failure, not a review nicety (`16` §6). CI requires no
secrets of any kind.
