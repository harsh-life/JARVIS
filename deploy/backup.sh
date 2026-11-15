#!/usr/bin/env bash
# Back up a JARVIS host (docs/PILOT_EXECUTION_PLAN.md F7, docs/DEPLOYMENT.md §8).
# Run as root:
#
#   sudo /opt/jarvis/deploy/backup.sh [--out DIR] [--online]
#
# Writes DIR/jarvis-<UTC timestamp>.tar (default DIR /var/backups/jarvis),
# root-only (directory 0700, file 0600), containing:
#   MANIFEST         what was backed up: time, code commit, schema revision, sha256 of each part
#   database.sql.gz  the store (pg_dump, plain SQL, no owner/privileges) or database.sqlite3.gz
#   state.tar.gz     /var/lib/jarvis without models/ (re-provisioned) and sandboxes/ (per-task temp)
#   config.yaml      /etc/jarvis/config.yaml — references only, never a secret
#
# It NEVER contains /etc/jarvis/credentials: not the KEK, not the model key,
# not the superuser token. SecretStore values are in the dump only as AEAD
# ciphertext, so the backup is useless without the KEK (12 §3, BR-T3) — and
# the KEK must not be stored with it.
#
# NOT encrypted as a whole: the database rows, Mem0 memories (plaintext at
# rest while OD-MB-4 is open) and the vault are user data in the clear. Store
# backups on encrypted media or encrypt them before they leave the host.
#
# Consistency: by default the service is stopped while the store is dumped and
# the state files are copied, then started again (the outage is the copy
# time). --online keeps it running: the dump is still a consistent snapshot,
# but the Mem0/Chroma and index files are copied live and may be torn.
set -euo pipefail

JARVIS_HOME="${JARVIS_HOME:-/opt/jarvis}"
JARVIS_USER="${JARVIS_USER:-jarvis}"
STATE_DIR="${JARVIS_STATE_DIR:-/var/lib/jarvis}"
ETC_DIR="${JARVIS_ETC_DIR:-/etc/jarvis}"
CONFIG="$ETC_DIR/config.yaml"
PY="$JARVIS_HOME/.venv/bin/python"
OUT=/var/backups/jarvis
ONLINE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --out) OUT="$2"; shift 2 ;;
        --online) ONLINE=1; shift ;;
        -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

say() { printf '==> %s\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
[ -f "$CONFIG" ] && [ -x "$PY" ] || { echo "no configured JARVIS install ($CONFIG, $PY)" >&2; exit 1; }
umask 077

# The store, as the configuration names it: kind, then connection fields.
read -r KIND DB_HOST DB_PORT DB_USER DB_NAME < <(cd "$JARVIS_HOME" && HYPERMIND_CONFIG_PATH="$CONFIG" "$PY" - <<'PY'
from sqlalchemy.engine import make_url
from server.config import load_config
u = make_url(load_config().database_url)
host = u.host or u.query.get("host") or "-"
print(u.get_backend_name(), host, u.port or "-", u.username or "-", u.database or "-")
PY
)
case "$KIND" in
    postgresql) command -v pg_dump >/dev/null || { echo "pg_dump not found (postgresql-client)" >&2; exit 1; } ;;
    sqlite) ;;
    *) echo "unsupported database kind: $KIND" >&2; exit 1 ;;
esac

install -d -m 0700 "$OUT"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORK="$(mktemp -d "$OUT/.jarvis-$STAMP.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

WAS_ACTIVE=0
if [ "$ONLINE" -eq 0 ] && systemctl is-active --quiet jarvis 2>/dev/null; then
    WAS_ACTIVE=1
    say "stopping jarvis for a consistent copy"
    systemctl stop jarvis
fi
restart_if_needed() {
    if [ "$WAS_ACTIVE" -eq 1 ]; then systemctl start jarvis; say "jarvis started again"; WAS_ACTIVE=0; fi
}
trap 'restart_if_needed; rm -rf "$WORK"' EXIT

say "dumping the $KIND store"
if [ "$KIND" = postgresql ]; then
    args=(--format=plain --no-owner --no-privileges --dbname="$DB_NAME")
    [ "$DB_HOST" != - ] && args+=(--host="$DB_HOST")
    [ "$DB_PORT" != - ] && args+=(--port="$DB_PORT")
    [ "$DB_USER" != - ] && args+=(--username="$DB_USER")
    # As the service user: peer authentication over the socket, or its
    # ~/.pgpass for a remote store (deploy/postgres.md §2–§3).
    runuser -u "$JARVIS_USER" -- env HOME="$STATE_DIR" pg_dump "${args[@]}" | gzip -9 > "$WORK/database.sql.gz"
    DB_PART=database.sql.gz
else
    # SQLite's online backup API: a consistent copy even if a writer is active.
    DB_FILE="$DB_NAME"; case "$DB_FILE" in /*) ;; *) DB_FILE="$JARVIS_HOME/$DB_FILE" ;; esac
    "$PY" -c 'import sqlite3, sys; s = sqlite3.connect(sys.argv[1]); d = sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' \
        "$DB_FILE" "$WORK/database.sqlite3"
    gzip -9 "$WORK/database.sqlite3"
    DB_PART=database.sqlite3.gz
fi

say "archiving $STATE_DIR (without models/ and sandboxes/)"
tar -C "$STATE_DIR" --exclude=./models --exclude=./sandboxes --exclude=./.cache \
    -czf "$WORK/state.tar.gz" .
restart_if_needed

cp "$CONFIG" "$WORK/config.yaml"

REVISION="$(cd "$JARVIS_HOME" && HYPERMIND_CONFIG_PATH="$CONFIG" runuser -u "$JARVIS_USER" -- env HOME="$STATE_DIR" \
    "$JARVIS_HOME/.venv/bin/alembic" current 2>/dev/null | awk '{print $1}' | head -1 || true)"
{
    echo "format: jarvis-backup/1"
    echo "created_utc: $STAMP"
    echo "host: $(hostname)"
    echo "code_commit: $(git -C "$JARVIS_HOME" rev-parse HEAD 2>/dev/null || echo unknown)"
    echo "schema_revision: ${REVISION:-unknown}"
    echo "database_kind: $KIND"
    echo "consistent: $([ "$ONLINE" -eq 0 ] && echo yes || echo online-copy)"
    echo "kek_included: no"
    echo "sha256:"
    (cd "$WORK" && sha256sum "$DB_PART" state.tar.gz config.yaml | sed 's/^/  /')
} > "$WORK/MANIFEST"

ARCHIVE="$OUT/jarvis-$STAMP.tar"
tar -C "$WORK" -cf "$ARCHIVE.partial" MANIFEST "$DB_PART" state.tar.gz config.yaml
chmod 0600 "$ARCHIVE.partial"
mv "$ARCHIVE.partial" "$ARCHIVE"
say "backup written: $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
cat <<EOF
    It holds user data in the clear (not the KEK). Keep it on encrypted storage;
    restoring it needs the KEK that was current when it was taken.
EOF
