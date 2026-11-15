# Deploying the JARVIS pilot

`docs/PILOT_EXECUTION_PLAN.md` step **M1** (F1–F6, F10/C3), under the owner's
confirmation of register §2N (2026-10-13). It runs the existing server on the
real topology: PostgreSQL, a reverse proxy with a stable HTTPS hostname,
systemd, Google OIDC and one priced remote model.

> **Disposable/test data only.** The real-data gate stays closed (`docs/PROJECT_STATUS.md`,
> "Before putting real data anywhere near this"; `docs/OD_A1_BR_T2.md`;
> OD-MB-4 is open, so memory is plaintext at rest). The pilot is
> **invite-only** (OD-PILOT-5) and the server refuses to start a remote
> primary without budgets and an allow-list (§7).

Files: `config.pilot.example.yaml`, `deploy/bootstrap.sh`,
`deploy/jarvis.service`, `deploy/jarvis-start.sh`, `deploy/logging.json`,
`deploy/Caddyfile`, `deploy/cloudflared.example.yml`, `deploy/postgres.md`,
`deploy/backup.sh`, `deploy/restore.sh`, `deploy/rotate-kek.sh`.

**Linux is the server platform.** macOS and Windows are client and development
environments: they run the desktop companion and the test suites, and reach a
server over HTTPS (§5.4). They never need to host it or to share files with it.
Every step below is exercised on each push to `deploy/` by
`.github/workflows/deploy-lifecycle.yml` (`tests/deploy/host_lifecycle.sh`) on
fresh Ubuntu 24.04 and 22.04 VMs.

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
4. Store the client secret in the SecretStore
   (`python -m server.composition.oidc_client_secret store`, after the bootstrap in §3)
   and put the printed handle in `security.oidc.client_secret_ref`
   (docs/GOOGLE_OIDC_SETUP.md). Never put the secret itself in config.
5. In `/etc/jarvis/config.yaml` set the same host in `server.base_url` and
   `android.app_links.base_url`, the client id in `security.oidc.client_id`,
   and the APK signing certificate's SHA-256 in
   `android.app_links.sha256_cert_fingerprints` (public values).

## 3. Bootstrap (F2)

**Prerequisites.** A Debian 12+/Ubuntu 22.04+ host with systemd, and:

```bash
# Debian 12, Debian 13, Ubuntu 24.04 (python3 is 3.11, 3.13, 3.12):
sudo apt-get install -y git python3 python3-venv postgresql
# Ubuntu 22.04 (python3 is 3.10 — too old; use the distro's 3.11):
sudo apt-get install -y git python3.11 python3.11-venv postgresql
```

`bootstrap.sh` checks these before it changes anything and names what is
missing. It uses the first of `python3`, `python3.13`, `python3.12`,
`python3.11` that is ≥ 3.11 (or `$JARVIS_PYTHON`). The lifecycle drill runs on
x86_64 Ubuntu 24.04 (Python 3.12, PostgreSQL 16) and Ubuntu 22.04 (Python 3.11,
PostgreSQL 14). On Debian 13 (Python 3.13, PostgreSQL 17, arm64) the bootstrap
was run once by hand and completed; the service itself was not exercised there.
On PostgreSQL 14 and older, `deploy/postgres.md` §1's `ALTER SCHEMA` line is
required.

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

**KEK custody.** The KEK is printed only when the bootstrap's output is an
interactive terminal; otherwise (CI, cloud-init, `| tee`) it says to read it
once with `sudo cat /etc/jarvis/credentials/kek`. Copy it to a password manager
or paper, off this host and out of every backup. A backup without it is useless
(by design); losing it makes every stored secret unrecoverable. If it may have
been exposed, rotate it (§9).

Then:

1. Edit every `REPLACE` value in `/etc/jarvis/config.yaml`: the model, its
   endpoint and real prices; `agent.bounds.per_task_budget` and
   `security.budgets.*` (your numbers, all > 0); `security.oidc.allowed_emails`.
2. A remote model: `sudo install -m 0600 /dev/stdin /etc/jarvis/credentials/model_api_key`
   and paste the provider key (it never goes in `config.yaml`). A local
   (Ollama) primary: leave that file as the bootstrap created it — empty. The
   unit always loads it (systemd refuses to start a unit whose credential file
   is missing) and `jarvis-start.sh` exports it only when non-empty.
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

**Logs** go to the journal (`journalctl -u jarvis`), one line per record with
level and logger name (`deploy/logging.json`): the server's `hypermind.*`
loggers at INFO, libraries at WARNING (httpx logs whole URLs at INFO). The
security record is the **audit trail** in the database (`audit_events`:
sign-ins and refusals, device registration and revocation, logouts, secret
handling, KEK rotation — references only, never a value), read through the
operator console (`docs/RUNNING_CONSOLE.md`) or SQL as `jarvis`.

`systemctl stop` is graceful: uvicorn finishes in-flight requests, background
services stop, the SecretStore's data key is dropped from memory, and the unit
reports `Result=success` (measured: well under the 30 s `TimeoutStopSec`).

Startup fails closed — the unit restarts and keeps failing visibly — when:
the KEK is missing or wrong (12 §8: `SecretIntegrityError` in the journal, no
key printed); `config.yaml` is invalid; `server.base_url` is plain http to a
non-loopback host (§5.4); a configured OIDC client-secret handle does not
resolve; or a remote primary lacks budgets or an allow-list (§7). Migrations are **not** run at
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

### 5.4 Remote connections and TLS

Every client that is not on the server itself connects over **HTTPS** to the
§2 hostname, through Caddy or the named tunnel. This is enforced at both ends:

* the server refuses to start when `server.base_url` is `http://` to anything
  but loopback (`server/config/preconditions.py`);
* the desktop companion accepts plain `http` only for `localhost`/`127.0.0.1`,
  and Android App Links require `https`.

**Do not** run uvicorn with `--host 0.0.0.0`, publish port 8000, or open it in
a firewall to "test connectivity": that serves sign-in codes, access tokens and
device proofs in the clear. A host firewall should allow only 22 and 443 (or
nothing inbound at all with a tunnel).

For a development or test server with no hostname yet, reach it from a Mac or
Windows client over SSH, which keeps loopback at both ends:

```bash
ssh -N -L 8000:127.0.0.1:8000 you@linux-server     # client's 127.0.0.1:8000 → server's
```

The companion then uses `http://127.0.0.1:8000` (a loopback URL it accepts),
and Google's redirect URI for that setup is
`http://127.0.0.1:8000/api/v1/auth/oidc/callback` (docs/GOOGLE_OIDC_SETUP.md).
The client needs nothing from the server's filesystem, and the server nothing
from the client's.

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

## 8. Backup and restore (F7)

```bash
sudo /opt/jarvis/deploy/backup.sh                    # → /var/backups/jarvis/jarvis-<UTC>.tar (0600)
sudo /opt/jarvis/deploy/backup.sh --out /mnt/backup --online
```

One tar holds: `MANIFEST` (code commit, schema revision, sha256 of each part),
the store (`pg_dump`, plain SQL), `/var/lib/jarvis` without `models/` and
`sandboxes/`, and `config.yaml`. **Never** the credentials: SecretStore values
are in the dump only as ciphertext, and restoring needs the KEK that was
current when the backup was taken — keep it apart from the backups.

The rest is **user data in the clear**: database rows, Mem0 memories
(plaintext at rest while OD-MB-4 is open) and the vault. Keep backups on
encrypted storage, or encrypt them before they leave the host (e.g.
`age -r <recipient> jarvis-*.tar`).

By default the service is stopped during the dump and copy (seconds for a
pilot) and started again, so the store and the Chroma files match. `--online`
avoids the outage; the dump is still a consistent snapshot, but the Chroma and
index files may be torn.

Restore, onto the same host or a fresh one (bootstrap it with `--no-db` first,
put the backup's KEK in `/etc/jarvis/credentials/kek`, and create an empty
database per `deploy/postgres.md` §1):

```bash
sudo systemctl stop jarvis
sudo /opt/jarvis/deploy/restore.sh /var/backups/jarvis/jarvis-<UTC>.tar
sudo systemctl start jarvis && curl -fsS http://127.0.0.1:8000/api/v1/ready
```

It verifies the checksums, restores only into an **empty** database (it never
drops anything), moves existing state aside to `*.pre-restore-<stamp>`, keeps
your `config.yaml` (the backup's goes to `config.yaml.from-backup`), proves the
host's KEK opens the restored store, re-provisions the embedder and migrates to
head. The CI drill restores into an emptied store and checks that the server
comes back ready with its stored secret resolving.

Schedule it with a systemd timer or cron as root, and prune old archives
yourself. A restore drill on a scratch host after every schema change is the
only proof a backup is good.

## 9. Rotation and revocation

| What | How | Effect |
|---|---|---|
| The KEK | `sudo /opt/jarvis/deploy/rotate-kek.sh` | Every data key re-sealed under a new KEK in one transaction; credential swapped atomically; service restarted. The old KEK no longer opens the store, and is removed from the host. It still opens backups made before the rotation: keep your off-host copy exactly as long as those backups. Audited as `secret.kek.rotate`. |
| The Google client secret | `python -m server.composition.oidc_client_secret rotate --handle <h>` (docs/GOOGLE_OIDC_SETUP.md) | Same handle, new value, no dual-valid window; restart the service. |
| A stored secret | `delete` / `rotate` on the SecretStore (12 §5) | Revoked handles refuse to resolve. |
| A device | `DELETE /api/v1/devices/{id}` from any of the owner's sessions (not step-up gated — the lost-phone path) | Its sessions end and its socket closes now; its credential stops verifying. |
| A session | `POST /api/v1/sessions/logout` | The access token stops working. |
| The model key | rewrite `/etc/jarvis/credentials/model_api_key`, `systemctl restart jarvis` | |
| The superuser token | rewrite `/etc/jarvis/credentials/superuser_token`, restart | |

Sessions and device registrations live in the database, so they survive a
restart; tasks interrupted by one are failed closed, never resumed (§4).

## 10. Upgrading

```bash
cd /opt/jarvis && sudo git pull --ff-only
sudo /opt/jarvis/deploy/backup.sh                 # before every schema change
sudo /opt/jarvis/deploy/bootstrap.sh              # migrates; never replaces keys or config
sudo systemctl restart jarvis
```

The core dependencies are lower bounds, not a lock (only the memory stack is
pinned exactly), so two installs a month apart can differ. Rebuild the venv
on a scratch host first if that matters to you.

## 11. Not covered here

Structured (JSON) log shipping, the optional container packaging (F9), the
web client (M3+) and Android hardware validation (M8) are later plan steps.
A dependency lock for reproducible installs is not in place yet (§10).
