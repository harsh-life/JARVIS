#!/usr/bin/env bash
# Replace the SecretStore's key-encryption key on a deployed host
# (docs/DEPLOYMENT.md, "Rotating the KEK"). Run as root:
#
#   sudo /opt/jarvis/deploy/rotate-kek.sh
#
# 1. generates a new KEK into /etc/jarvis/credentials/kek.new (root, 0600);
# 2. re-seals every data key under it in one database transaction
#    (python -m server.composition.kek_rotate, as the jarvis user, both keys in
#    the environment — never on a command line);
# 3. replaces /etc/jarvis/credentials/kek with the new one (atomic rename) and
#    restarts the service if it was running, then waits for /api/v1/ready.
#
# The old KEK is removed from this host. It still opens backups taken before
# the rotation — keep your off-host copy only as long as you keep those.
#
# If a previous run was interrupted after step 2, kek.new is left behind; this
# script then checks which key opens the store and finishes or discards the
# rotation accordingly. It never leaves the host without a working KEK.
set -euo pipefail

JARVIS_HOME="${JARVIS_HOME:-/opt/jarvis}"
JARVIS_USER="${JARVIS_USER:-jarvis}"
STATE_DIR="${JARVIS_STATE_DIR:-/var/lib/jarvis}"
ETC_DIR="${JARVIS_ETC_DIR:-/etc/jarvis}"
CRED_DIR="$ETC_DIR/credentials"
CONFIG="$ETC_DIR/config.yaml"
KEK="$CRED_DIR/kek"
NEW="$CRED_DIR/kek.new"
PY="$JARVIS_HOME/.venv/bin/python"

say() { printf '==> %s\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
[ -s "$KEK" ] || { echo "no KEK at $KEK — nothing to rotate" >&2; exit 1; }
[ -x "$PY" ] || { echo "no JARVIS venv at $JARVIS_HOME/.venv (run deploy/bootstrap.sh first)" >&2; exit 1; }

as_jarvis() {
    (cd "$JARVIS_HOME" && runuser -u "$JARVIS_USER" -- env HOME="$STATE_DIR" HYPERMIND_CONFIG_PATH="$CONFIG" \
        PYTHONDONTWRITEBYTECODE=1 "$@")
}

# Does the key in file $1 open the store? (exit 0 = yes). The key travels in
# the environment; runuser keeps it.
opens_store() {
    HYPERMIND_KEK="$(cat "$1")" as_jarvis "$PY" - <<'PY' >/dev/null 2>&1
import asyncio, sys
from server.config import load_config
from server.gateway.security import build_security_core, unlock_secret_store
from server.storage import SQLAlchemyStorageBackend

async def main() -> None:
    config = load_config()
    storage = SQLAlchemyStorageBackend(config.database_url)
    try:
        await unlock_secret_store(build_security_core(config), storage)
    finally:
        await storage.dispose()

try:
    asyncio.run(main())
except Exception:
    sys.exit(1)
PY
}

finish() {
    mv -f "$NEW" "$KEK"
    chmod 0600 "$KEK"
    say "the new KEK is in place: $KEK"
    if systemctl is-active --quiet jarvis 2>/dev/null; then
        say "restarting jarvis so it unlocks with the new KEK"
        systemctl restart jarvis
        for _ in $(seq 60); do
            curl -fs -o /dev/null "http://127.0.0.1:${JARVIS_PORT:-8000}/api/v1/ready" && { say "ready"; break; }
            sleep 1
        done
    fi
    if [ -t 1 ]; then
        printf '\n    Copy the new KEK off this host now (it will not be shown again):\n\n    %s\n\n' "$(cat "$KEK")"
    else
        printf '\n    Copy the new KEK off this host now:  sudo cat %s\n\n' "$KEK"
    fi
    cat <<EOF
    The old KEK no longer opens this store and has been removed from this host.
    It still opens backups taken BEFORE this rotation: keep your off-host copy
    exactly as long as you keep those backups, then destroy both.
EOF
}

if [ -e "$NEW" ]; then
    say "found $NEW from an interrupted rotation"
    if opens_store "$NEW"; then
        say "the store is already sealed under it — completing the rotation"
        finish; exit 0
    elif opens_store "$KEK"; then
        say "the store is still sealed under the current KEK — discarding $NEW"
        rm -f "$NEW"
    else
        echo "neither $KEK nor $NEW opens the store; nothing changed. Restore the KEK from your off-host copy." >&2
        exit 1
    fi
fi

opens_store "$KEK" || { echo "the current KEK does not open the store; nothing changed" >&2; exit 1; }

say "generating a new KEK"
(umask 077; "$PY" -c 'from server.secrets.kek import generate_kek_value; print(generate_kek_value())' > "$NEW")
chmod 0600 "$NEW"

say "re-sealing the SecretStore's data keys under the new KEK"
if HYPERMIND_KEK="$(cat "$KEK")" HYPERMIND_NEW_KEK="$(cat "$NEW")" \
        as_jarvis "$PY" -m server.composition.kek_rotate; then
    finish
else
    rm -f "$NEW"
    echo "rotation refused; the store and $KEK are unchanged" >&2
    exit 1
fi
