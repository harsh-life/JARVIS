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

## Current state: `security-core` branch

Two branches are in:

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

**There is still no agent runtime, tool execution, model provider,
filesystem sandbox, network egress enforcement, or Android integration** —
those are later branches, and they consume this one's interfaces rather
than re-deriving them.

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
tests/     pytest suite (tests/foundation/, tests/security_core/)
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
`import-linter` — see `pyproject.toml`'s `[tool.importlinter]` section.
A boundary violation is a CI failure, not a review nicety (`16` §6).
