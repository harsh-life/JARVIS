# Running the `security-core` branch locally

This supersedes `docs/RUNNING_FOUNDATION.md` for anything beyond the health
check. Foundation's instructions still apply for install, config, and migrations;
what changes here is that the server now has **authentication, authorization, and
a SecretStore**, and it will not serve without the KEK.

There is still **no agent runtime, no tool execution, no filesystem sandbox, no
network egress enforcement, and no Android integration** — those are later
branches. Don't expect to submit an agent task.

## 1. Install and configure

As `docs/RUNNING_FOUNDATION.md` §2–§3, plus one config change: point
`security.oidc.client_id` at a **real** Google OIDC client id if you intend to log
in. It is a public client using PKCE, so there is no client secret — and a
secret-shaped value anywhere in config is a load-time failure by design
(SECRET-004).

```yaml
security:
  oidc:
    client_id: "<your-own-google-oidc-client-id>.apps.googleusercontent.com"
    issuer: "https://accounts.google.com"

secrets:
  store: "encrypted_local"
  kek_source: "env:HYPERMIND_KEK"   # the *source*, never the key
```

Add this redirect URI to your Google OAuth client:
`<your base_url>/api/v1/auth/oidc/callback`.

## 2. Generate the KEK (12 §3)

The SecretStore encrypts every secret under a data key that is itself sealed under
a **key-encryption key you supply out of band**. It is never in git, never in the
database, never in a log, and never in the APK.

```bash
export HYPERMIND_KEK="$(python3 -m server.secrets.kek)"
```

Store that value somewhere durable and outside the repository — a password
manager, or your OS keychain. **If you lose it, every stored secret is
unrecoverable**, which is the same property that makes a stolen database useless.

## 3. Migrate

```bash
export HYPERMIND_DATABASE_URL="sqlite+aiosqlite:///./data/hypermind.db"
mkdir -p data
alembic upgrade head
```

This adds security-core's tables on top of foundation's: the SecretStore's wrapped
key and AEAD material, OIDC login state, bootstrap tokens, access tokens, device
proof nonces, confirmation tokens, and graph access requests.

## 4. Bootstrap the SecretStore (first run only)

The store has to create its data key before anything can be stored. This is 15 §3's
bootstrap step:

```bash
python3 - <<'PY'
import asyncio
from server.config import load_config
from server.gateway.security import build_security_core, unlock_secret_store
from server.storage import SQLAlchemyStorageBackend

async def main():
    config = load_config()
    storage = SQLAlchemyStorageBackend(config.database_url)
    core = build_security_core(config)
    await unlock_secret_store(core, storage, bootstrap=True)
    await storage.dispose()
    print("SecretStore bootstrapped")

asyncio.run(main())
PY
```

Re-running this is safe: it unlocks rather than replacing the existing key, so it
can never orphan the secrets already encrypted under it.

## 5. Superuser authority (optional, 12 §4)

Master-key references are resolvable **only** by a superuser, and superuser
authority is a separate principal from every Hypermind user — no Google login,
no capability, and no endpoint grants it. If you need it, set a second env var,
distinct from the KEK so that holding one does not yield the other:

```bash
export HYPERMIND_SUPERUSER_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
```

Leave it unset and superuser authority simply does not exist on this deployment —
which is the correct default.

## 6. Run

```bash
uvicorn server.composition.main:app --host 127.0.0.1 --port 8000
```

Startup performs the unlock. **If `HYPERMIND_KEK` is missing or wrong, the server
refuses to start** — a running Hypermind whose store is locked cannot register a
device or verify a credential, so serving anyway would be the silently secretless
mode 12 §8 forbids.

## 7. The login flow end to end

```
GET  /api/v1/auth/oidc/start          → { redirect_url }          # public
     ↓ open redirect_url, consent (identity scopes only)
GET  /api/v1/auth/oidc/callback?code&state
                                      → { needs_device_registration, bootstrap_token }
POST /api/v1/devices                  → { device_id, device_credential }
     Authorization: Bearer <bootstrap_token>
     ⚠ device_credential is returned EXACTLY ONCE and is never retrievable again
POST /api/v1/sessions/token           → { access_token, expires_at }
     { "device_credential": "<proof>" }
```

The device credential is an **Ed25519 private key**; the server keeps only the
public verifier. So `/sessions/token` does not take the credential itself — it
takes a short signed proof of possession. `server.auth.device.build_device_proof`
is the normative implementation an Android client mirrors:

```python
from server.auth.device import build_device_proof
proof = build_device_proof(device_id=device_id, device_credential=credential)
```

Every other endpoint takes `Authorization: Bearer <access_token>`.

## 8. What exists

| Endpoint | Auth |
|---|---|
| `GET /api/v1/health` | public |
| `GET /api/v1/auth/oidc/start` · `GET /api/v1/auth/oidc/callback` | public |
| `POST /api/v1/devices` | bootstrap token |
| `POST /api/v1/devices/{id}/rotate` | Bearer + **step-up** |
| `DELETE /api/v1/devices/{id}` | Bearer (owner) |
| `POST /api/v1/sessions/token` | device-credential proof |
| `POST /api/v1/sessions/logout` · `POST /api/v1/sessions/active-graph` | Bearer |
| `POST`/`GET /api/v1/graphs`, `…/access-requests`, `…/members` | Bearer |
| `GET`/`POST`/`DELETE /api/v1/capabilities` | Bearer |

Credential rotation requires **step-up**: a valid but stale access token is
refused, and the client re-attests with its device credential first. Device
*revocation* deliberately does not, because revocation is the mitigation for a
stolen phone.

## 9. What is intentionally missing

- **Agent tasks, tool execution, model providers** — `05`/`06`/`07`.
- **Account deletion** (`DELETE /api/v1/account`) — the endpoint exists in 02 §3,
  but LIFE-003's cascade covers memories, files, and jobs, which no branch has
  built. Step-up is implemented and exercised on credential rotation instead.
- **`mem0fact` authorization** — the engine recognises the resource type and denies
  it, because Mem0 is `11`'s store, not a table this branch may reach into.
- **Filesystem and network boundaries** — `09`/`10`. `FileResource` rows are
  authorized here; nothing touches a disk or a socket.

## 10. Tests and checks

```bash
python3 -m pytest tests/ -q                       # whole suite (see RUNNING_RUNTIME.md)
lint-imports --config pyproject.toml              # boundary contracts
python3 -m pytest tests/security_core/test_od_a1_br_t2.py -q -s   # BR-T2 measurement
```

The last one prints the measured blast radius under simulated app-level RCE. Read
`docs/OD_A1_BR_T2.md` before putting real data anywhere near this: OD-A1 is
**resolved for the pilot as an accepted residual** — not an isolation claim — and
real-user readiness still needs the unbuilt `09`/`10`/`11`/`08` release-blocking
suites.

All four run in CI on every pull request (`.github/workflows/ci.yml`), which is
what makes REPO-T7 true rather than aspirational — 16 §6 `[LOCKED]`s that a
boundary violation is "a **CI failure**, not a review nicety". CI needs **no
secrets**: every suite builds its own throwaway config and generates a fresh KEK
per test, so a fresh clone runs the whole check set on nothing but the repo
(HOST-001).
