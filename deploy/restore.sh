#!/usr/bin/env bash
# Restore a backup made by deploy/backup.sh (docs/DEPLOYMENT.md §8). Run as root
# with the service stopped:
#
#   sudo systemctl stop jarvis
#   sudo /opt/jarvis/deploy/restore.sh /var/backups/jarvis/jarvis-<stamp>.tar
#   sudo systemctl start jarvis
#
# What it does, refusing before it changes anything if a check fails:
#   * verifies every part against the MANIFEST checksums;
#   * restores the store ONLY into an empty database (create it per
#     deploy/postgres.md §1 first) — it never drops or overwrites tables;
#   * moves any existing state (mem0_storage, vault, vault_index) aside to
#     <name>.pre-restore-<stamp> instead of deleting it, then unpacks the backup;
#   * leaves /etc/jarvis/config.yaml alone, writing config.yaml.from-backup next
#     to it when the two differ;
#   * checks that the KEK in /etc/jarvis/credentials/kek opens the restored
#     SecretStore — the backup never carries the KEK; put the one that was
#     current when the backup was taken in place first;
#   * re-provisions the embedder if models/ is empty (it is not in backups);
#   * if the backup's schema is older than the code, migrates it to head.
set -euo pipefail

JARVIS_HOME="${JARVIS_HOME:-/opt/jarvis}"
JARVIS_USER="${JARVIS_USER:-jarvis}"
STATE_DIR="${JARVIS_STATE_DIR:-/var/lib/jarvis}"
ETC_DIR="${JARVIS_ETC_DIR:-/etc/jarvis}"
CONFIG="$ETC_DIR/config.yaml"
KEK="$ETC_DIR/credentials/kek"
PY="$JARVIS_HOME/.venv/bin/python"

say() { printf '==> %s\n' "$*"; }
die() { echo "restore refused: $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "run as root"
[ $# -eq 1 ] && [ -f "$1" ] || { echo "usage: $0 /path/to/jarvis-<stamp>.tar" >&2; exit 2; }
ARCHIVE="$1"
[ -f "$CONFIG" ] && [ -x "$PY" ] || die "no configured JARVIS install (run deploy/bootstrap.sh --no-db first)"
[ -s "$KEK" ] || die "no KEK at $KEK — install the KEK that was current when the backup was taken"
if systemctl is-active --quiet jarvis 2>/dev/null; then die "jarvis is running — systemctl stop jarvis first"; fi
umask 077

as_jarvis() {
    (cd "$JARVIS_HOME" && runuser -u "$JARVIS_USER" -- env HOME="$STATE_DIR" HYPERMIND_CONFIG_PATH="$CONFIG" \
        PYTHONDONTWRITEBYTECODE=1 "$@")
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
tar -C "$WORK" -xf "$ARCHIVE"
[ -f "$WORK/MANIFEST" ] && grep -qx "format: jarvis-backup/1" "$WORK/MANIFEST" || die "not a jarvis-backup/1 archive"
grep -qx "kek_included: no" "$WORK/MANIFEST" || die "MANIFEST does not state kek_included: no"
say "verifying checksums"
(cd "$WORK" && sed -n 's/^  //p' MANIFEST | sha256sum --check --quiet) || die "checksum mismatch — the archive is damaged"
KIND="$(sed -n 's/^database_kind: //p' "$WORK/MANIFEST")"

read -r CUR_KIND DB_HOST DB_PORT DB_USER DB_NAME < <(cd "$JARVIS_HOME" && HYPERMIND_CONFIG_PATH="$CONFIG" "$PY" - <<'PY'
from sqlalchemy.engine import make_url
from server.config import load_config
u = make_url(load_config().database_url)
host = u.host or u.query.get("host") or "-"
print(u.get_backend_name(), host, u.port or "-", u.username or "-", u.database or "-")
PY
)
[ "$KIND" = "$CUR_KIND" ] || die "the backup is a $KIND store but config.yaml names a $CUR_KIND store"

if [ "$KIND" = postgresql ]; then
    args=(--quiet --no-psqlrc --set=ON_ERROR_STOP=1 --dbname="$DB_NAME")
    [ "$DB_HOST" != - ] && args+=(--host="$DB_HOST")
    [ "$DB_PORT" != - ] && args+=(--port="$DB_PORT")
    [ "$DB_USER" != - ] && args+=(--username="$DB_USER")
    tables="$(cd / && runuser -u "$JARVIS_USER" -- env HOME="$STATE_DIR" psql "${args[@]}" -tAc \
        "select count(*) from information_schema.tables where table_schema = 'public'")" \
        || die "cannot connect to the configured database as $JARVIS_USER (create it per deploy/postgres.md §1)"
    [ "$tables" = 0 ] || die "database $DB_NAME is not empty ($tables tables). Restore goes into an empty database only: drop and recreate it per deploy/postgres.md §1 if that is what you intend"
    say "restoring the PostgreSQL store into the empty database $DB_NAME"
    gzip -dc "$WORK/database.sql.gz" | (cd / && runuser -u "$JARVIS_USER" -- env HOME="$STATE_DIR" \
        psql "${args[@]}" --single-transaction --file=-) >/dev/null
else
    DB_FILE="$DB_NAME"; case "$DB_FILE" in /*) ;; *) DB_FILE="$JARVIS_HOME/$DB_FILE" ;; esac
    [ ! -s "$DB_FILE" ] || die "$DB_FILE exists — move it away first; restore never overwrites a store"
    gzip -dc "$WORK/database.sqlite3.gz" > "$DB_FILE"
    chown "$JARVIS_USER:$JARVIS_USER" "$DB_FILE"; chmod 0600 "$DB_FILE"
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
say "restoring $STATE_DIR (existing state moved aside, never deleted)"
for sub in mem0_storage vault vault_index; do
    if [ -e "$STATE_DIR/$sub" ] && [ -n "$(ls -A "$STATE_DIR/$sub" 2>/dev/null)" ]; then
        mv "$STATE_DIR/$sub" "$STATE_DIR/$sub.pre-restore-$STAMP"
        say "  moved $STATE_DIR/$sub aside to $sub.pre-restore-$STAMP"
    else
        rm -rf "${STATE_DIR:?}/$sub"
    fi
done
tar -C "$STATE_DIR" --no-same-owner -xzf "$WORK/state.tar.gz"
chown -R "$JARVIS_USER:$JARVIS_USER" "$STATE_DIR"
chmod 0700 "$STATE_DIR"

if ! cmp -s "$WORK/config.yaml" "$CONFIG"; then
    install -m 0640 -o root -g "$JARVIS_USER" "$WORK/config.yaml" "$CONFIG.from-backup"
    say "config.yaml differs from the backup's; the backup's copy is $CONFIG.from-backup (yours is unchanged)"
fi

say "checking that the KEK on this host opens the restored SecretStore"
HYPERMIND_KEK="$(cat "$KEK")" as_jarvis "$PY" - <<'PY' || die "the KEK in $KEK does not open the restored store — install the KEK that was current when the backup was taken (the restored data stays in place)"
import asyncio
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

asyncio.run(main())
print("    SecretStore unlocked")
PY

install -d -m 0700 -o "$JARVIS_USER" -g "$JARVIS_USER" "$STATE_DIR/models" "$STATE_DIR/sandboxes"
if [ -z "$(ls -A "$STATE_DIR/models" 2>/dev/null)" ]; then
    say "re-provisioning the local embedder (not part of backups; same step as deploy/bootstrap.sh)"
    as_jarvis "$PY" -m server.memory provision --config "$CONFIG" || say "  embedder provisioning failed — run it again before starting"
fi

say "migrating the restored store to the code's head (no-op if already there)"
as_jarvis "$JARVIS_HOME/.venv/bin/alembic" upgrade head

cat <<EOF

==> Restore complete from $(basename "$ARCHIVE") ($(sed -n 's/^created_utc: //p' "$WORK/MANIFEST")).
    systemctl start jarvis && curl -fsS http://127.0.0.1:8000/api/v1/ready
EOF
