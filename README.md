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

## Current state: `foundation` branch

This branch implements only the technical substrate later branches build
on: shared data contracts, configuration, persistence/migrations, and the
API skeleton (versioning, request IDs, the error envelope, a health
check). **There is no authentication, authorization, agent runtime, tool
execution, or SecretStore yet.** See `docs/RUNNING_FOUNDATION.md` to run
what exists today.

## Repository layout

```
server/    the modular-monolith FastAPI application (one package per subsystem)
shared/    schemas/  — canonical Pydantic data contracts, importable by both
                        server/ and a future android/ client
tests/     pytest suite
docs/      operational docs (this branch: docs/RUNNING_FOUNDATION.md)
Working Markdown/   the architecture/PRD document package (source of truth)
```

Module boundaries (who may import whom) are enforced mechanically via
`import-linter` — see `pyproject.toml`'s `[tool.importlinter]` section and
`docs/RUNNING_FOUNDATION.md`'s "Checking module boundaries" section.
