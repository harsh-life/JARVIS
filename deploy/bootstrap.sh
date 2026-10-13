#!/usr/bin/env bash
# JARVIS pilot host bootstrap (docs/PILOT_EXECUTION_PLAN.md F2,
# docs/DEPLOYMENT.md §3). One command on a fresh Debian/Ubuntu host, run as
# root from a checkout at /opt/jarvis:
#
#   sudo /opt/jarvis/deploy/bootstrap.sh            # everything below
#   sudo /opt/jarvis/deploy/bootstrap.sh --no-db    # skip the database steps
#
# Idempotent: every step checks before it acts, so running it again changes
# nothing that already exists. In particular it NEVER replaces an existing
# KEK, superuser token, config.yaml or database — a second KEK would orphan
# every secret already encrypted under the first.
#
# What it does not do: install PostgreSQL, Caddy or cloudflared (distribution
# packages; docs/DEPLOYMENT.md §2–§5), choose your model key, or start the
# service. It prints the remaining manual steps at the end.
set -euo pipefail

JARVIS_HOME="${JARVIS_HOME:-/opt/jarvis}"
JARVIS_USER="${JARVIS_USER:-jarvis}"
STATE_DIR="${JARVIS_STATE_DIR:-/var/lib/jarvis}"
ETC_DIR="${JARVIS_ETC_DIR:-/etc/jarvis}"
CRED_DIR="$ETC_DIR/credentials"
CONFIG="$ETC_DIR/config.yaml"
UNIT_DIR="${JARVIS_UNIT_DIR:-/etc/systemd/system}"
WITH_DB=1
WITH_SUPERUSER=0

for arg in "$@"; do
    case "$arg" in
        --no-db) WITH_DB=0 ;;
        --with-superuser) WITH_SUPERUSER=1 ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

say() { printf '==> %s\n' "$*"; }
as_jarvis() {
    runuser -u "$JARVIS_USER" -- env HOME="$STATE_DIR" HYPERMIND_CONFIG_PATH="$CONFIG" \
        PYTHONDONTWRITEBYTECODE=1 "$@"
}

[ "$(id -u)" -eq 0 ] || { echo "run as root (it creates a system user and /etc/jarvis)" >&2; exit 1; }
[ -f "$JARVIS_HOME/pyproject.toml" ] || { echo "no JARVIS checkout at $JARVIS_HOME" >&2; exit 1; }
# The code and its venv are public (root-owned, world-readable, read-only to
# the service); everything secret is created under an explicit 077 below.
umask 022

# 1. The service account: no login shell, home = the state directory.
if ! id "$JARVIS_USER" >/dev/null 2>&1; then
    say "creating system user $JARVIS_USER"
    useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$JARVIS_USER"
fi

# 2. Directories. State is owner-only; configuration is root-owned and
#    readable by the service; credentials are root-only (systemd hands them
#    to the service through LoadCredential=).
install -d -m 0700 -o "$JARVIS_USER" -g "$JARVIS_USER" "$STATE_DIR"
for sub in mem0_storage models vault vault_index sandboxes; do
    install -d -m 0700 -o "$JARVIS_USER" -g "$JARVIS_USER" "$STATE_DIR/$sub"
done
install -d -m 0750 -o root -g "$JARVIS_USER" "$ETC_DIR"
install -d -m 0700 -o root -g root "$CRED_DIR"

# 3. The virtual environment, owned by root (the service only reads it).
if [ ! -x "$JARVIS_HOME/.venv/bin/python" ]; then
    say "creating $JARVIS_HOME/.venv"
    python3 -m venv "$JARVIS_HOME/.venv"
fi
if ! "$JARVIS_HOME/.venv/bin/python" -c "import server, mem0" >/dev/null 2>&1; then
    say "installing JARVIS with the memory stack"
    "$JARVIS_HOME/.venv/bin/pip" install --quiet --upgrade pip
    "$JARVIS_HOME/.venv/bin/pip" install --quiet -e "$JARVIS_HOME[memory]"
fi
chmod -R u+rwX,go+rX,go-w "$JARVIS_HOME"
chmod 0755 "$JARVIS_HOME/deploy/jarvis-start.sh"

# 4. The configuration: copied once from the pilot model, never overwritten.
if [ ! -f "$CONFIG" ]; then
    say "installing $CONFIG from config.pilot.example.yaml — edit every REPLACE value"
    install -m 0640 -o root -g "$JARVIS_USER" "$JARVIS_HOME/config.pilot.example.yaml" "$CONFIG"
fi

# 5. The KEK: generated once, shown once. Losing it makes every stored secret
#    unrecoverable — by design (12 §3, BR-T3).
if [ ! -s "$CRED_DIR/kek" ]; then
    (umask 077; "$JARVIS_HOME/.venv/bin/python" -c \
        'from server.secrets.kek import generate_kek_value; print(generate_kek_value())' > "$CRED_DIR/kek")
    chmod 0600 "$CRED_DIR/kek"
    cat <<EOF

==> A new key-encryption key (KEK) was generated: $CRED_DIR/kek
    Copy it NOW to a place that is not this host and not any backup of it
    (a password manager, or printed and locked away). Without it, nothing the
    SecretStore holds can ever be decrypted; with it, anyone holding a backup
    can. It will not be shown again:

    $(cat "$CRED_DIR/kek")

EOF
fi

# 6. Optional superuser authority (12 §4): a second, distinct secret.
if [ "$WITH_SUPERUSER" -eq 1 ] && [ ! -s "$CRED_DIR/superuser_token" ]; then
    (umask 077; "$JARVIS_HOME/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(32))' \
        > "$CRED_DIR/superuser_token")
    chmod 0600 "$CRED_DIR/superuser_token"
    say "superuser token written to $CRED_DIR/superuser_token; uncomment its LoadCredential= line in jarvis.service"
fi

# 7. The database: migrate to head, then create the SecretStore's data key
#    under the KEK (first run) or prove the KEK unlocks it (every later run).
if [ "$WITH_DB" -eq 1 ]; then
    say "migrating the database to the code's Alembic head"
    (cd "$JARVIS_HOME" && as_jarvis "$JARVIS_HOME/.venv/bin/alembic" upgrade head)
    say "bootstrapping / unlocking the SecretStore"
    # The KEK travels in the environment (never argv, which `ps` shows);
    # runuser keeps the caller's environment.
    (cd "$JARVIS_HOME" && HYPERMIND_KEK="$(cat "$CRED_DIR/kek")" && export HYPERMIND_KEK \
        && as_jarvis "$JARVIS_HOME/.venv/bin/python" - <<'PY'
import asyncio

from server.config import load_config
from server.gateway.security import build_security_core, unlock_secret_store
from server.storage import SQLAlchemyStorageBackend


async def main() -> None:
    config = load_config()
    storage = SQLAlchemyStorageBackend(config.database_url)
    try:
        await unlock_secret_store(build_security_core(config), storage, bootstrap=True)
    finally:
        await storage.dispose()


asyncio.run(main())
print("    SecretStore unlocked")
PY
    )
fi

# 8. Memory and vault: the embedder is the only download (docs/RUNNING_MEMORY.md);
#    the vault is a Git repository the operator curates, indexed from HEAD.
say "provisioning the local embedder"
(cd "$JARVIS_HOME" && as_jarvis "$JARVIS_HOME/.venv/bin/python" -m server.memory provision --config "$CONFIG")
if [ ! -d "$STATE_DIR/vault/.git" ]; then
    say "initialising the knowledge vault repository"
    as_jarvis git -C "$STATE_DIR/vault" init --quiet
    as_jarvis git -C "$STATE_DIR/vault" -c user.name=jarvis -c user.email=jarvis@localhost \
        commit --quiet --allow-empty -m "empty vault"
fi
(cd "$JARVIS_HOME" && as_jarvis "$JARVIS_HOME/.venv/bin/python" -m server.vault reindex --config "$CONFIG")

# 9. The unit.
if ! cmp -s "$JARVIS_HOME/deploy/jarvis.service" "$UNIT_DIR/jarvis.service"; then
    say "installing $UNIT_DIR/jarvis.service"
    install -m 0644 "$JARVIS_HOME/deploy/jarvis.service" "$UNIT_DIR/jarvis.service"
    if [ -d /run/systemd/system ]; then systemctl daemon-reload; fi
fi

remaining=""
[ -s "$CRED_DIR/model_api_key" ] || remaining="$remaining
  * put the model provider's key in $CRED_DIR/model_api_key (root, 0600)"
grep -q "REPLACE" "$CONFIG" && remaining="$remaining
  * replace every REPLACE value in $CONFIG (base_url, model, client_id, allowed_emails, ...)"
remaining="$remaining
  * reverse proxy: deploy/Caddyfile or deploy/cloudflared.example.yml (docs/DEPLOYMENT.md §5)
  * systemctl enable --now jarvis
  * curl -fsS https://<your host>/api/v1/ready   -> {\"ready\": true, ...}"
printf '\n==> Bootstrap complete. Remaining steps:%s\n' "$remaining"
