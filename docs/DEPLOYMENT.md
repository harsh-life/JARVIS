# Deploying the JARVIS pilot

`docs/PILOT_EXECUTION_PLAN.md` step **M1** (F1–F6, F10/C3), under the owner's
confirmation of register §2N (2026-10-13). It runs the existing server on the
real topology: PostgreSQL, a reverse proxy with a stable HTTPS hostname,
systemd, Google OIDC and one priced remote model.

> **Disposable/test data only.** The real-data gate stays closed (README,
> "Before putting real data anywhere near this"; `docs/OD_A1_BR_T2.md`;
> OD-MB-4 is open, so memory is plaintext at rest). The pilot is
> **invite-only** (OD-PILOT-5) and the server refuses to start a remote
> primary without budgets and an allow-list (§7).

Files: `config.pilot.example.yaml`, `deploy/bootstrap.sh`,
`deploy/jarvis.service`, `deploy/jarvis-start.sh`, `deploy/Caddyfile`,
`deploy/cloudflared.example.yml`, `deploy/postgres.md`.

## 1. Topology (P24)

```
phone / browser ──HTTPS──▶ Caddy  (or Cloudflare named tunnel → cloudflared)
                              │  loopback only
                              ▼
                  uvicorn 127.0.0.1:8000  (systemd: jarvis.service, User=jarvis)
                     │                 │
          PostgreSQL (Unix socket,   /var/lib/jarvis  (Mem0, vault, index,
          peer auth, role jarvis)     sandboxes, embedder cache; 0700)
```

One host. Laptop and VPS use the same files with different values. No
Kubernetes, no managed service, no paid infrastructure. A container image is
optional packaging, not this path: Landlock confinement needs the host kernel.

| Path | Owner / mode | Holds |
|---|---|---|
| `/opt/jarvis` | root, world-readable | the checkout and `.venv` (read-only to the service) |
| `/etc/jarvis/config.yaml` | root:jarvis 0640 | configuration; references only, never a secret |
| `/etc/jarvis/credentials/` | root 0700, files 0600 | `kek`, `model_api_key`, optional `superuser_token` |
| `/var/lib/jarvis` | jarvis 0700 | all server state; the service's only writable path |

## 2. Hostname, domain and OIDC client (F6)

The one unavoidable external item is a **domain name**: Google's OIDC
redirect URI and the Android App Link both need a stable HTTPS hostname.

1. A domain you control (for the tunnel: its DNS on Cloudflare; the free plan
   suffices). Pick one hostname, e.g. `jarvis.example.com`.
2. **Either** point it at a host with a public address and use Caddy (§5.1),
   **or** create a Cloudflare **named** tunnel to it (§5.2). A quick tunnel's
   random `trycloudflare.com` URL is not an option: it changes on restart, so
   it cannot be a registered redirect URI or App Link host.
3. A Google OAuth client (Web application) with authorized redirect URI
   `https://jarvis.example.com/api/v1/auth/oidc/callback`. Keep the consent
   screen in **Testing** with the pilot users as test users — a second,
   external invite gate in front of `allowed_emails`.
4. In `/etc/jarvis/config.yaml` set the same host in `server.base_url` and
   `android.app_links.base_url`, the client id in `security.oidc.client_id`,
   and the APK signing certificate's SHA-256 in
   `android.app_links.sha256_cert_fingerprints` (public values).

## 3. Bootstrap (F2)

```bash
sudo git clone <this repository> /opt/jarvis
# PostgreSQL role and database first: deploy/postgres.md §1
sudo /opt/jarvis/deploy/bootstrap.sh
```

It is idempotent — run it again after any change and it repeats only what is
missing. It creates the `jarvis` system user and the directories above,
builds the venv with the memory stack, installs `config.yaml` from
`config.pilot.example.yaml` **once**, generates the KEK **once** and prints it
**once**, migrates the database to head, bootstraps (or, on later runs,
unlocks) the SecretStore, provisions the embedder (the only download),
initialises and indexes an empty vault, and installs the unit. It never
replaces an existing KEK, token, config or database.

**KEK custody.** Copy the printed KEK to a password manager or paper, off this
host and out of every backup. A backup without it is useless (by design);
losing it makes every stored secret unrecoverable.

Then:

1. Edit every `REPLACE` value in `/etc/jarvis/config.yaml`: the model, its
   endpoint and real prices; `agent.bounds.per_task_budget` and
   `security.budgets.*` (your numbers, all > 0); `security.oidc.allowed_emails`.
2. `sudo install -m 0600 /dev/stdin /etc/jarvis/credentials/model_api_key`
   and paste the provider key (it never goes in `config.yaml`).
3. `--with-superuser` generates the optional superuser token; leave it off
   unless you need `/admin/control/*` (`docs/RUNNING_SECURITY_CORE.md` §5).

## 4. The service (F4)

```bash
sudo systemctl enable --now jarvis
journalctl -u jarvis -f
```

`deploy/jarvis.service` runs as `jarvis` with `UMask=0077`, a read-only
system (`ProtectSystem=strict`, writable `/var/lib/jarvis` only), no
capabilities and `Restart=on-failure`. Secrets arrive as systemd credentials
(`LoadCredential=`), which `deploy/jarvis-start.sh` exports under the names
`config.yaml` references (`env:HYPERMIND_KEK`, `env:JARVIS_MODEL_API_KEY`) —
they are never in the unit file, an environment file or the configuration.
uvicorn binds `127.0.0.1` only, trusts forwarded headers from `127.0.0.1`
only, and has its access log off (a request line can carry an OIDC `code`).

Startup fails closed — the unit restarts and keeps failing visibly — when:
the KEK is missing or wrong (12 §8); `config.yaml` is invalid; or a remote
primary lacks budgets or an allow-list (§7). Migrations are **not** run at
startup (15 §3): after a code upgrade run `bootstrap.sh` (or
`alembic upgrade head` as `jarvis`) before `systemctl restart jarvis`.

A process killed mid-task (`kill -9`) restarts; tasks it left running are
reconciled to failed on startup, never resumed.

## 5. Reverse proxy (F5)

The public surface is exactly: `/api/*` (including the device WebSocket),
`/ui` (the web client, M3), `/app/login` (the App Link login return) and
`/.well-known/assetlinks.json`. Everything else is 404 at the edge.

### 5.1 Caddy (a host with a public address)

`deploy/Caddyfile`: automatic HTTPS, HSTS, a 16 MB request cap (image input
is ≤ 3 × 4 MB), per-address rate limiting of `/api/v1/auth/*` and
`/api/v1/sessions/token` (the in-app limits meter model and tool calls, not
logins), and an access log with query strings removed. It needs the
`caddy-ratelimit` module (`xcaddy build --with github.com/mholt/caddy-ratelimit`);
stock Caddy refuses the file rather than running unthrottled.

### 5.2 Cloudflare named tunnel (no public address)

`deploy/cloudflared.example.yml` routes the same paths to `127.0.0.1:8000`
and answers 404 for the rest. Cloudflare terminates HTTPS, so set at the zone:
**Always Use HTTPS** and **HSTS** on; one **rate-limiting rule** for
`/api/v1/auth/*` and `/api/v1/sessions/token` by IP. If you enable any
Cloudflare request logging, exclude query strings (the OIDC callback carries
`code` and `state`).
The tunnel credentials file is a secret (0600, never committed).

### 5.3 Checklist

```bash
H=https://jarvis.example.com
curl -fsS $H/api/v1/health                      # 200 {"status":"ok",...}
curl -fsS $H/api/v1/ready                       # 200 {"ready":true,...}
curl -sI  $H/api/v1/health | grep -i strict-transport-security
curl -so /dev/null -w '%{http_code}\n' $H/anything-else          # 404
curl -fsS $H/.well-known/assetlinks.json        # your package + fingerprint
for i in $(seq 40); do curl -so /dev/null -w '%{http_code} ' $H/api/v1/auth/oidc/start; done   # 429s appear
```

## 6. Liveness vs readiness (F10, C3)

| | `GET /api/v1/health` | `GET /api/v1/ready` |
|---|---|---|
| For | "is the process up" | "can it serve a request" — monitors, the M1 exit check |
| Status | **always 200** | 200 when ready, else **503** |
| Body | `status`, `database` | booleans only: `ready`, `database`, `migrations`, `secrets` |

`/ready` is 200 only when the store answers, its schema is at the code's
Alembic head, and the SecretStore is unlocked. It carries no versions,
revision ids or error text, and is `Cache-Control: no-store`. M1 is done when
`GET https://<host>/api/v1/ready` returns 200 from a phone's browser.

## 7. Invite-only and budgets (OD-PILOT-2, OD-PILOT-5)

* **Admission.** `security.oidc.allowed_emails` (exact addresses) and
  `allowed_domains` (exact domains — not their subdomains) decide who may sign
  in. Only a Google-*verified* email can match. Anyone else completes Google's
  consent and gets `403` with no user row created and an audited
  `auth.oidc.callback.rejected` event (resource `auth:oidc:not_admitted`). Both lists empty means
  "anyone who completes OIDC" — the development default, refused below for a
  remote model.
* **Startup preconditions** (`server/config/preconditions.py`, checked by the
  entrypoint): a non-local primary needs `agent.bounds.per_task_budget`,
  `security.budgets.per_user_daily_cost_limit` and `global_daily_cost_limit`
  all > 0, and a non-empty allow-list. Either missing → the server does not
  start, naming the decision. A local (Ollama) primary has neither
  requirement.

The pilot stays invite-only until isolation, budgets and usage controls are
validated (OD-PILOT-5).

## 8. Not covered here

Backup/restore (F7), structured logging (F8), the optional container
packaging (F9), the web client (M3+) and Android hardware validation (M8) are
later plan steps.
