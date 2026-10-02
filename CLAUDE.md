# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

JARVIS / Hypermind Track B: a configurable, self-hostable personal-agent runtime
(Python/FastAPI server + a Kotlin Android client). The full spec lives in
`Working Markdown/00_CANONICAL_PRD.md`; `Working Markdown/TRACK_B_ARCHITECTURE_INDEX.md`
maps the 17 numbered subsystem documents (`Working Markdown/0X_*.md`,
`docs/1X_*.md`+) to the code. `README.md` has a detailed per-branch tour of
what's built and, critically, what is **not** — read it before assuming a
feature (Darwin, a real IntelligenceProvider, account
deletion) exists. The central invariant, enforced structurally, not by
convention:

```
AGENT PROPOSES → DETERMINISTIC INFRASTRUCTURE AUTHORIZES → TOOLS EXECUTE → HUMAN CONFIRMS WHERE REQUIRED
```

Model output is never the security boundary. Real user data is **not**
cleared for this codebase yet — see "Before putting real data anywhere near
this" in `README.md` and `docs/OD_A1_BR_T2.md`; disposable/test data only.

## Setup

```bash
uv venv .venv && source .venv/bin/activate
uv pip install -e ".[dev]"          # add ",memory" for the Mem0/Chroma/fastembed stack
cp config.example.yaml config.yaml  # never commit config.yaml
export HYPERMIND_DATABASE_URL="sqlite+aiosqlite:///./data/hypermind.db"   # dev store
mkdir -p data && alembic upgrade head
# The pilot's runtime store is PostgreSQL (H-1): postgresql+asyncpg://<user>@<host>:5432/<db>,
# never with a password in the URL (config refuses it) — PGPASSWORD or ~/.pgpass instead.
# See docs/RUNNING_FOUNDATION.md §4.
export HYPERMIND_KEK="$(python3 -m server.secrets.kek)"   # security-core: store durably, out of repo
```

Run the server: `uvicorn server.composition.main:app --host 127.0.0.1 --port 8000`

If working on the `memory`/`vault` stack, also provision the local embedder
once: `python -m server.memory provision --config config.example.yaml`
(the only sanctioned network call; everything after loads offline).

Per-branch setup detail (OIDC client id, superuser bootstrap, vault indexing,
etc.) lives in `docs/RUNNING_FOUNDATION.md`, `docs/RUNNING_SECURITY_CORE.md`,
`docs/RUNNING_RUNTIME.md`, `docs/RUNNING_EXECUTION.md`, `docs/RUNNING_MEMORY.md`,
`docs/RUNNING_SCHEDULER.md`, `docs/RUNNING_VOICE.md` — read the relevant one
before working in that area.

## Commands

```bash
# Full suite (excludes tests/memory, which needs the memory stack provisioned)
python -m pytest tests/ -q --ignore=tests/memory

# One subsystem's tests
python -m pytest tests/security_core -q
python -m pytest tests/runtime -q
python -m pytest tests/execution -q
python -m pytest tests/scheduler -q
python -m pytest tests/voice -q
python -m pytest tests/integration -q
python -m pytest tests/agents -q

# A single test
python -m pytest tests/runtime/test_confirmation_boundary.py::test_name -q

# Memory suite (needs `pip install -e ".[memory]"` + provisioning above)
HYPERMIND_REQUIRE_MEMORY_STACK=1 python -m pytest tests/memory -q

# Any suite on PostgreSQL (CI's `postgres` job): one fresh database per test on a
# throwaway server; unreachable = failure, not skip. PRD #32's acceptance
# (tests/memory/test_prd32_service_measurement.py) runs only this way.
HYPERMIND_TEST_DATABASE_URL="postgresql+asyncpg://postgres@127.0.0.1:5432/postgres" python -m pytest tests/ -q --ignore=tests/memory

# Module-boundary contracts (see Architecture below) — run this after any new
# cross-package import; it's a separate CI job, not folded into pytest
lint-imports --config pyproject.toml

# Blast-radius measurement (docs/OD_A1_BR_T2.md, 14 §4) — re-measured, not asserted
python -m pytest tests/security_core/test_od_a1_br_t2.py -q -s

# Migration round-trip (must actually work both ways — 15 §3/STORE-004)
alembic upgrade head && alembic downgrade base && alembic upgrade head && alembic check

# Android (from android/)
./gradlew --no-daemon ktlintCheck detekt
./gradlew --no-daemon :contract:test :app:testDebugUnitTest
```

CI (`.github/workflows/ci.yml`) needs no secrets at all — suites build their
own throwaway config and generate a fresh key per test. It runs everything
twice: on SQLite (`checks`) and on a PostgreSQL service container (`postgres`,
trust auth, no credential), which also round-trips the migrations and runs
PRD #32. `tests/asyncio_mode`
is `auto` (pytest-asyncio), configured in `pyproject.toml`.

## Architecture

The system is a modular monolith under `server/`, one package per subsystem,
plus `shared/` (Pydantic schemas + the Android device-mapping artifact,
importable by both `server/` and `android/`) and `android/` (`:contract`
pure-Kotlin wire contract + device-side guard, and `:app`).

**Module boundaries are not a convention — they're mechanically enforced**
by `import-linter` (`pyproject.toml`'s `[tool.importlinter]`, ~15 contracts,
run as its own CI step). A boundary violation is a CI failure. The layering
(bottom → top):

```
config | storage
secrets
security
net | fs
execution
graph | capabilities
auth
gateway
models
agent | agents | modeltools | tools | memory | vault | scheduler | voice | evaluation
dashboard
composition
```

Key rules this encodes (read `docs/RUNNING_EXECUTION.md` §3 and
`Working Markdown/16_REPOSITORY_MODULE_BOUNDARIES.md` before adding a
cross-package import):

- **`server.agent` never imports `server.secrets`, `server.capabilities`, or
  `server.graph`.** The model proposes; it cannot grant itself authority or
  touch secrets directly. It reaches the authorization engine and SecretStore
  only through Protocols in `server/agent/ports.py`, satisfied by
  `server/composition/` at the top.
- **`server.graph` (decides) and `server.capabilities` (supplies policy)
  never import each other** — kept independent so the half that evaluates a
  check can't also grant one. They meet only at the gateway composition root
  via `server/graph/ports.py`.
- **`server.execution`, `server.fs`, `server.net` never import
  `server.secrets`** and receive only an already-authorized
  `ExecutionRequest` (`shared/schemas/execution.py`) — no field or method
  lets them assert authority themselves.
- **`server.tools`, `server.modeltools`, `server.agent` cannot import
  `subprocess`/`socket` directly** — all process/network access goes through
  `server.execution`/`server.fs`/`server.net`.
- **`server.dashboard` is read-only**: forbidden from importing anything
  that mutates or acts (agent, tools, execution, capabilities, graph,
  secrets, superuser paths, etc.) — it consults read-only Protocols in
  `server/dashboard/ports.py`.
- **Only `server.gateway` and `server.composition` may reach superuser
  authority** (`server.security.superuser`, `server.gateway.control_port`,
  break-glass, the Judge's control switches) — every other package, the
  dashboard included, is explicitly barred.
- **`server.evaluation` (the Judge) never authorizes, executes, or resolves
  secrets**, and the runtime (`server.agent`) never depends on it.
- **`shared/` never imports `server`** — it's the one thing both the server
  and the Android client import; the dependency direction must stay one-way.

Subsystem map (see `README.md` for the full narrative per branch):

| Package | Owns |
|---|---|
| `server/auth` | Google OIDC, Ed25519 device credentials, opaque access tokens, step-up |
| `server/graph` + `server/capabilities` | the five-dimension authorization engine (membership, role, ownership, visibility, capability) + the closed capability/risk-tier/confirmation registry |
| `server/secrets` | SecretStore: AES-256-GCM under an external KEK, handle-only access |
| `server/agent` | the propose→parse→authorize→confirm→execute→observe loop, under hard ceilings |
| `server/models` / `server/modeltools` | normalized model-provider interface (Ollama default; OpenAI-compatible adapters); LLM-as-a-tool |
| `server/tools` | tool registry validated against the capability registry; per-platform adapters |
| `server/execution` / `server/fs` / `server/net` | constrained execution: fs sandbox (`dir_fd`+`O_NOFOLLOW`, not path-prefix checks), default-deny egress with DNS-rebinding protection, `argv`-only process execution, Android/Shizuku dispatch |
| `server/memory` / `server/vault` | Mem0-backed `MemoryProvider` (visibility re-checked at hydration) + the Git-backed Knowledge Vault |
| `server/scheduler` | task-linked reminders (`scheduled_jobs` table; a firing reminder delivers a message, never executes; an agent's reminder only carries `agent_id` as data — the tap is the owner's own run) |
| `server/voice` | STT/TTS; on-device by default; voice can never confirm or step up |
| `server/evaluation` | the Judge (scoring/recommendation only, no authority) |
| `server/agents` | the Agent Factory (docs/29, a proposal, off by default): templates, ability table, pure selector/compiler, owner-private definitions, the native runtime provider; Phase 2 runs are present-user, on-demand ordinary tasks (envelope gate in `server/agent/envelope.py`, run port in `server/agent/agent_run.py`, wiring in `server/composition/agents.py`); Phases 3–4 add the in-process Agent/Model Gateway (`server/agents/gateway/`: run tokens, nonces, replay, model alias + live month budget), owner-scoped `agent.purpose` Judge candidates, the operator's agent pause, and reminder-tap runs (`kind = reminder_tap`); Phase 5 adds unattended runs under a StandingDelegation (`server/agents/delegation.py`, `server/agents/triggers.py`, the trigger loop in `server/composition/agent_triggers.py`; the run's principal is a `DelegatedPrincipal` with no device/session; off unless `agents.unattended_enabled`); never an authority (contracts AF-C1…C6) |
| `server/dashboard` | read-only operator views |
| `server/composition` | the composition root — the only place upper-layer Protocols get their concrete implementations wired in; `server/composition/main.py` is the app entrypoint |
| `server/gateway` | FastAPI routers, request context, the one HTTP entry to superuser auth |

Tests mirror this: `tests/foundation`, `tests/security_core`, `tests/runtime`,
`tests/execution`, `tests/memory`, `tests/scheduler`, `tests/voice`,
`tests/integration` (cross-cutting acceptance + BR-T2 rows).

When adding a feature, first place it in the right layer per the table
above, then check whether an import-linter contract in `pyproject.toml`
already documents the rule you're about to touch — most non-obvious
boundary decisions have a comment there explaining the invariant and citing
the source document/decision (e.g. `INV-8`, `REPO-T1`, `DASH-002`).
