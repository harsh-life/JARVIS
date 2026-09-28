# Running the `foundation` branch locally

This covers only what the `foundation` branch actually implements: config
loading, the database schema/migrations, and the API skeleton (versioning,
request IDs, the error envelope, a health check). There is **no
authentication, no authorization, and no agent** yet — that is later
branches' work (starting with `security-core`). Don't expect to register a
device or submit an agent task; those endpoints don't exist here.

This is the subset of 15_CONFIGURATION_SELF_HOSTING.md §3's bootstrap flow
that foundation owns (steps 2, 3, 5, 7, 10 below — parenthetical numbers
refer to that document's own step numbering).

## 1. Prerequisites

- Python 3.11+
- [`uv`](https://github.com/astral-sh/uv) (or plain `pip` — either works)

## 2. Install (15 §3 step 2)

```bash
git clone <this-repo>
cd JARVIS
uv venv .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

(`pip install -e ".[dev]"` works identically without `uv`.)

## 3. Create your own config (15 §3 step 3 — never commit this file)

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml`:
- `security.oidc.client_id` — a placeholder for now (auth isn't implemented
  yet); set it to your own value when you do wire up OIDC in a later
  branch. **Never** put a real client secret in this file — Track B's OIDC
  flow is a public client (PKCE), so there shouldn't be one, and any
  secret-shaped value anywhere in config must be a `secret_ref` (see the
  comments in `config.example.yaml`), never a literal.
- Leave `intelligence.enabled: false` (the default) — this is intentional
  (INTEL-003); nothing in Track B requires it to be true.

Config loading is fail-closed: a missing file, malformed YAML, a literal
secret, or a `memory.mem0.collection` matching `vault.collection` all abort
startup with a clear message rather than a partial/degraded start.

## 4. Initialize the database (15 §3 step 5)

Two stores are supported, and the URL's driver picks one. Anything else — or
a URL with a password in it — fails at startup (and at `alembic`) with a
message naming the problem, never echoing the value.

**PostgreSQL — the pilot's runtime store (H-1).** PostgreSQL 13 or later. Put
the URL, **without a password**, in `config.yaml` (or `HYPERMIND_DATABASE_URL`
for migrations):

```yaml
database_url: "postgresql+asyncpg://jarvis_app@db.example.internal:5432/jarvis"
```

The password is supplied out of band, the libpq way, in the server's (and
`alembic`'s) environment: `PGPASSWORD`, or a `~/.pgpass` line
`db.example.internal:5432:jarvis:jarvis_app:<password>` in a file with mode
`0600`. It never goes in `config.yaml`, the repository, a log or the console
(SECRET-004). For a database that is not on localhost, add `?ssl=require` to
the URL. Create the role and database once, as the database owner:

```sql
CREATE ROLE jarvis_app LOGIN;          -- then set its password out of band
CREATE DATABASE jarvis OWNER jarvis_app;
```

then:

```bash
export HYPERMIND_DATABASE_URL="postgresql+asyncpg://jarvis_app@db.example.internal:5432/jarvis"
alembic upgrade head
```

**SQLite — development and tests.** One writer at a time, so it does not meet
PRD #32 (fairness under ~10-device load); fine for a single developer and for
the test suites.

```bash
export HYPERMIND_DATABASE_URL="sqlite+aiosqlite:///./data/hypermind.db"
mkdir -p data
alembic upgrade head
```

(If you don't set `HYPERMIND_DATABASE_URL`, migrations fall back to reading
`database_url` from `config.yaml` via `server/config`.) Every migration
downgrades as well as upgrades, on both stores:
`alembic upgrade head && alembic downgrade base && alembic upgrade head && alembic check`.

Startup fails closed. The server (`server.composition.main`) touches the store
before serving — it closes tasks a previous run left unfinished — so an
unreachable server, a missing database or a refused login stops it with
"Application startup failed" and the driver's error in the log (measured:
connection refused and `database "…" does not exist` both exit with status 3).
A URL carrying a password never gets that far: config loading refuses it
(and `alembic` refuses it in `HYPERMIND_DATABASE_URL`), naming the rule and
not the value. Once running, a transient conflict the store reports (a
deadlock, a serialization failure, a lock timeout, too many connections, or
SQLite's busy lock) fails the affected request `503 dependency_unavailable`
(`details.dependency = "storage"`), retryable. A database server that goes
away mid-run is **not** mapped yet: those requests fail `500 internal_error`
(logged) until it is back — a known limitation (`docs/RELEASE_VALIDATION.md` §N).

This creates every table in `01_DATA_MODEL_SCHEMA.md`'s relational-store
catalog (users, devices, sessions, graphs, graph_memberships,
agent_configurations, capability_grants, secret_references, audit_events,
usage_events, permission_decisions, file_resources, scheduled_jobs,
voice_events, speaker_contexts, plus foundation's own idempotency_keys
table). It does **not** stand up Mem0 or the Knowledge Vault's ChromaDB
collections — those are a later branch's job (11_MEMORY_CONTEXT_VISIBILITY.md).

To reset from scratch:

```bash
alembic downgrade base   # drops every table
alembic upgrade head     # recreates them
```

## 5. Run the server (15 §3 step 7)

```bash
uvicorn server.composition.main:app --host 127.0.0.1 --port 8000
```

## 6. Health check (15 §3 step 10)

```bash
curl -s http://127.0.0.1:8000/api/v1/health | python3 -m json.tool
```

Expected: `{"status": "ok", "database": "ok"}` (or `"degraded"` /
`"unreachable"` if the DB isn't reachable — the endpoint stays `200` either
way; it's public and unauthenticated by design, see
`server/gateway/routers/health.py`).

## What's intentionally missing

- **Pairing a phone / OIDC login / device registration** (15 §3 steps
  4, 6, 8, 9) — needs the auth branch (`03_AUTH_IDENTITY_SESSION.md`).
- **A Cloudflare Tunnel** — nothing to tunnel to yet beyond `/health`.
- **Any endpoint under `/api/v1` other than `/health`** — every other
  endpoint in `02_API_PROTOCOL.md`'s catalog needs authentication and/or
  authorization this branch does not implement.

## Running the tests

```bash
python3 -m pytest tests/ -q
```

Each test gets its own throwaway SQLite file (see `tests/foundation/conftest.py`)
— nothing here touches `config.yaml` or `data/hypermind.db`.

To run the same suites on PostgreSQL (as CI's `postgres` job does), point them
at a server the tests may create and drop databases on — a throwaway one,
never a deployment's:

```bash
export HYPERMIND_TEST_DATABASE_URL="postgresql+asyncpg://<user>@127.0.0.1:5432/postgres"
python -m pytest tests/ -q --ignore=tests/memory
```

Each test then gets a fresh database (`tests/dbsupport.py`); an unreachable
server is a test failure, not a skip. The PRD #32 acceptance
(`tests/memory/test_prd32_service_measurement.py`) runs only on PostgreSQL.

## Checking module boundaries

```bash
lint-imports --config pyproject.toml
```

This is the mechanical check behind 16_REPOSITORY_MODULE_BOUNDARIES.md's
"a boundary violation is a CI failure, not a review nicety" rule — wire it
into CI as a required check alongside `pytest`.
