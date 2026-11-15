# Google sign-in: Web application OAuth client

JARVIS signs people in with Google (OIDC, `server/auth/`). The server is the
OAuth client: it sends the browser to Google, receives the callback, exchanges
the code at Google's token endpoint and validates the id_token. PKCE (S256) is
always used.

Google's token endpoint also authenticates a **Web application** client by its
**client secret**. Without one the exchange fails, and the server log shows
`Google token exchange refused: status=400 error=invalid_request client_authenticated=False`.
So a deployment using a Web application client configures the secret **by
reference**: `security.oidc.client_secret_ref`.

What does not change:
- PKCE, the stored `redirect_uri`, the five id_token checks (signature, issuer,
  audience, expiry, nonce), state single-use, and the email/domain allow-list.
- The desktop companion and the Android app never receive the secret, and no
  browser-visible URL carries it.

## 1. Create the OAuth client (Google Cloud console)

Select the intended project in the console's project picker first, and change
nothing else in it. Newer consoles group these pages under **Google Auth
Platform**; older ones put them under *APIs & Services → OAuth consent screen*.

1. **Consent screen** (*Branding*): app name and support email. User type
   **External**, publishing status **Testing**.
2. **Test users** (*Audience → Test users*): add each disposable account that
   may sign in. While the app is in Testing, any other Google account gets
   `Error 403: access_denied` from Google before JARVIS sees anything. This is
   a second gate in front of `security.oidc.allowed_emails`. Adding a test user
   grants that account nothing in your project: it only lets it complete the
   consent screen.
3. **Clients → Create client** (or *Credentials → Create credentials → OAuth
   client ID*), application type **Web application**.
4. **Authorized redirect URIs**: add exactly the URI from §2, character for
   character. `localhost` and `127.0.0.1` are **different** URIs to Google.
   *Authorized JavaScript origins* are not used.
5. Copy the **client ID** into `security.oidc.client_id` (a public value).
6. Put the **client secret** straight into the SecretStore through the hidden
   prompt (§3a), or into an environment variable (§3b). Never put it in
   `config.yaml`, a commit, a downloaded JSON file you keep, a ticket or a chat.
   Newer consoles show a secret only when it is created: if you cannot see
   it, add a new secret (§5) rather than reusing an old file.

Any `client_secret_*.json` you downloaded earlier holds a secret in plain text.
Treat it as exposed: delete the file, and disable that secret in the console
once a new one works.

**Check that a client exists** without signing in: open the server's
`GET /api/v1/auth/oidc/start` `redirect_url` in a private window. Google's
sign-in page means the client and redirect URI are accepted. `Error 401:
deleted_client` means the client is gone; create a new one.

Scopes are fixed in code to `openid email profile` (`server/auth/oidc.py`).
Request nothing else on the consent screen.

## 2. The redirect URI

The server sends Google exactly:

```
<server.base_url without a trailing slash> + /api/v1/auth/oidc/callback
```

This is built in `server/gateway/security.py`, with `OIDC_CALLBACK_PATH`. Each
login stores the URI with its state, and the code exchange re-sends that stored
value. Google requires an exact match: scheme, host, port and path, with no
trailing slash.

| `server.base_url` | URI to register |
|---|---|
| default `http://127.0.0.1:8000` | `http://127.0.0.1:8000/api/v1/auth/oidc/callback` |
| `https://jarvis.example.com` (pilot, docs/DEPLOYMENT.md §2) | `https://jarvis.example.com/api/v1/auth/oidc/callback` |

`tests/security_core/test_oidc_client_secret.py::test_the_default_redirect_uri_is_base_url_plus_the_callback_path`
pins the default row.

Google's own rules (outside this repository): a redirect URI must be HTTPS
unless its host is `localhost` or a loopback IP, and a raw non-loopback IP such
as `http://192.168.1.20:8000` is refused. That is why local development keeps
`base_url` on `127.0.0.1` (§4) and the pilot uses a stable HTTPS hostname.

## 3. Provide the secret

Choose one of two forms. Both are references; a literal value in config is a
load-time failure (SECRET-004).

### a. SecretStore (recommended for a deployment)

The secret is stored encrypted under the KEK as a **server-owned** secret of
class `oauth_token`, and is resolved only for the server's own login exchange.
This needs a migrated database and a bootstrapped store
(docs/RUNNING_SECURITY_CORE.md §3–§4), with the KEK in its environment variable.

```bash
python -m server.composition.oidc_client_secret store      # hidden prompt; prints only the handle
# → secretstore:3f2c…   (also printed as the config line to add)
```

```yaml
security:
  oidc:
    client_id: "<your-client-id>.apps.googleusercontent.com"
    issuer: "https://accounts.google.com"
    client_secret_ref: "secretstore:3f2c…"     # the handle, never the secret
```

On a non-interactive host, the command reads one line from stdin. Pipe it from
your secret manager rather than typing it into a shell:
`… | python -m server.composition.oidc_client_secret store`.

### b. Environment variable (simplest for local development)

```yaml
    client_secret_ref: "env:JARVIS_GOOGLE_CLIENT_SECRET"
```

Set `JARVIS_GOOGLE_CLIENT_SECRET` in the server's environment. Under systemd, use
an `EnvironmentFile=` readable only by the service user, as for `HYPERMIND_KEK`
in docs/DEPLOYMENT.md. The variable name is your choice.

### What happens at runtime

- **Startup:** after the SecretStore unlocks, the server checks the reference
  without decrypting it. For `env:`, the variable must be set and non-empty. For
  `secretstore:`, the handle must exist, be unrevoked, and be a server-owned
  `oauth_token`. If the check fails the server **refuses to start**, naming the
  setting and the reference (never a value). Run the same check on demand with
  `python -m server.composition.oidc_client_secret check`.
- **That check cannot tell whether Google accepts the secret.** A resolvable
  secret of the *wrong* client (for example, a deleted client's) passes it and
  fails only at the first real callback. `check --live` closes that gap: it
  sends Google's token endpoint one request with a deliberately invalid
  authorization code. Google authenticates the client first, so `invalid_client`
  means the ID and secret are not a pair, and `invalid_grant` means the client
  authenticated (only the fake code was refused). No real code is used and no
  token can be issued. It is operator-run, never at startup.
- **Each login:** the secret is resolved just before the code exchange and
  sent only in the POST body to `https://oauth2.googleapis.com/token`
  (`client_secret_post`). If it cannot be resolved, the callback answers
  `503 dependency_unavailable` ("sign-in is not available on this server right
  now"). Nothing is sent to Google, and the audit trail records
  `auth.oidc.callback.rejected` on `auth:oidc:client_secret_unavailable`.
- **Only a client secret is sent.** `oauth_token` is also the class of the push
  wake's Google service-account key, and the setting names a handle directly.
  So a resolved value shaped like another credential (a JSON object, a PEM key,
  or anything with whitespace) is refused with the same `503`, and nothing is
  sent to Google.
- **Never** in a log line, an audit row (SecretStore audit rows carry the handle
  and requester `server`), an API response, a redirect, the database in clear,
  or anything the desktop companion or a browser receives.
- **No reference** (the default): no secret is sent, as before. That suits a
  client that does not need one, but a Web application client does.

## 4. Local development bootstrap

**Scripted, isolated (recommended for a first real sign-in):** keep a
disposable dev environment outside the repository, in a directory only you can
read. It holds its own SQLite database, a generated KEK file (mode 0600), and a
`config.yaml` copied from `config.example.yaml`, with `server.base_url` at the
default, `desktop.enabled: true`, and `secrets.kek_source: env:HYPERMIND_KEK`.
Migrate it, bootstrap its SecretStore (docs/RUNNING_SECURITY_CORE.md §4), then
run the provisioning steps **yourself, in your own terminal**:

```bash
# reads the client ID (shown) and the secret (hidden prompt), stores the secret,
# writes only the handle into config.yaml, then runs the startup check
python -m server.composition.oidc_client_secret store     # → secretstore:<id>
python -m server.composition.oidc_client_secret check --live   # Google accepts this ID + secret?
```

Nothing on a command line, in shell history or in a file ever holds the secret.

**By hand:**

From the repository root, with the venv active (CLAUDE.md "Setup"):

```bash
cp config.example.yaml config.yaml          # git-ignored; never commit it
# edit config.yaml:
#   security.oidc.client_id: your Web client's ID
#   security.oidc.client_secret_ref: "env:JARVIS_GOOGLE_CLIENT_SECRET"
#   server.base_url: leave the default http://127.0.0.1:8000
export HYPERMIND_DATABASE_URL="sqlite+aiosqlite:///./data/hypermind.db"
export HYPERMIND_KEK="$(python3 -m server.secrets.kek)"          # keep it; the store is sealed under it
mkdir -p data && alembic upgrade head
# SecretStore bootstrap: docs/RUNNING_SECURITY_CORE.md §4
read -rs JARVIS_GOOGLE_CLIENT_SECRET && export JARVIS_GOOGLE_CLIENT_SECRET   # paste; not echoed, not in history
uvicorn server.composition.main:app --host 127.0.0.1 --port 8000
```

Register `http://127.0.0.1:8000/api/v1/auth/oidc/callback` on the client, then
follow README "Get an access token" (`GET /api/v1/auth/oidc/start`, open the
`redirect_url`, sign in).

**Server in a VM, browser on the host** (for example an Ubuntu VM): keep
`server.host` and `server.base_url` on `127.0.0.1:8000` inside the VM, and
forward the port to the host, for example
`ssh -L 8000:127.0.0.1:8000 <user>@<vm>` or the hypervisor's port forwarding.
The browser on the host then reaches `http://127.0.0.1:8000`, and the
registered URI stays the default. Do not set `base_url` to the VM's LAN IP:
Google refuses that redirect URI (§2), and the desktop companion allows plain
HTTP only for a loopback server (`desktop/src-tauri/src/net.rs`).

The desktop companion's own loopback listener (`127.0.0.1:<port>/jarvis/callback`)
is unrelated to Google. It belongs to the server → companion handoff
(`server/auth/desktop_handoff.py`) and is not registered with Google.

## 5. Rotating the secret

The SecretStore handle is stable, so rotation never touches `config.yaml`:

1. In the console, add a new client secret to the same OAuth client, if your
   console offers it. Otherwise reset the secret: the old one then stops working
   as soon as the reset completes.
2. Replace the value under the existing handle:
   ```bash
   python -m server.composition.oidc_client_secret rotate --handle secretstore:3f2c…
   ```
   It is resolved per login, so the next sign-in uses the new value with no
   restart. Only an active server-owned `oauth_token` handle can be rotated.
3. Run `… check --live`, check that a sign-in works, then disable and delete
   the old secret in the console.

For `env:`, change the variable and restart the server.

If the secret may have leaked, rotate first, then delete the old secret in
the console at once. The audit trail (`secret.get` / `secret.rotate` rows on the
handle) shows when the server resolved it.

## 6. Testing and troubleshooting

Automated tests mock Google and never use real credentials:

```bash
python -m pytest tests/security_core/test_oidc_client_secret.py -q   # this feature
python -m pytest tests/security_core -q                              # auth, desktop handoff, SecretStore
lint-imports --config pyproject.toml
```

| Symptom | Cause | Fix |
|---|---|---|
| Server will not start: `security.oidc.client_secret_ref is env:X, but that environment variable is unset or empty` | the variable is missing in the service environment | set it, or remove the reference |
| Server will not start: `… no such SecretStore reference exists` / `… is revoked` / `… is not a server-owned oauth_token secret` | wrong handle, a revoked one, or a different kind of secret | `store` a new one and update the handle |
| Callback `503 dependency_unavailable`; log `OIDC client secret unavailable: …` | resolution failed at login (variable removed, store locked, handle revoked) | as above; `… check` |
| Callback `503`; log `OIDC client secret refused: … shaped like another credential` | the reference names something that is not a client secret, such as the push wake's service-account key (also a server-owned `oauth_token`) | point `client_secret_ref` at the handle `store` printed for the client secret |
| Callback `401`; log `error=invalid_request client_authenticated=False` plus a hint naming `security.oidc.client_secret_ref` | a Web client with no secret configured | configure §3 |
| Callback `401`; log `error=invalid_client client_authenticated=True` and `Google rejected the configured client secret for client_id …`; audit `auth:oidc:client_rejected` | wrong or deleted secret, or the secret of a different client (`check` still passes: the reference resolves) | `… check --live` confirms it; rotate (§5) with the secret of *this* client |
| Callback `401`; log `error=invalid_grant` | code reused or expired, `redirect_uri` mismatch, or PKCE mismatch | start a new login; check §2 |
| Google shows `redirect_uri_mismatch` before the callback | the URI on the client differs from `base_url` + callback path. Often `http://localhost:8000/…` registered while JARVIS sends `http://127.0.0.1:8000/…` | register the exact URI from §2; don't change `server.base_url` to match an old client |
| Google shows `Error 401: deleted_client` | the OAuth client was deleted (or belongs to a deleted project). Its old secret is useless | create a new Web client (§1) and provision its secret (§3) |
| Google shows `Error 401: invalid_client` before sign-in | `security.oidc.client_id` is mistyped, or names a client that never existed | copy the client ID again |
| Google shows `Error 403: access_denied` ("has not completed the Google verification process") | the account is not a test user while the app is in Testing | add it under *Audience → Test users* (§1) |
| Callback `403` | the account is not on `allowed_emails` / `allowed_domains`, or its email is unverified | admit it in config |

The server logs only the RFC 6749 `error` code of a refused exchange, never the
response body or its description, which can echo request values.

Do not attempt a real Google sign-in until the OAuth client and its secret are
configured as above. Use disposable test accounts only (docs/PROJECT_STATUS.md).
