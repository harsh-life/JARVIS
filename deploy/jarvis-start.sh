#!/bin/sh
# The JARVIS service entrypoint (docs/DEPLOYMENT.md §4). Run by systemd only
# (deploy/jarvis.service), never by hand.
#
# Secrets reach the process through systemd credentials (LoadCredential=):
# files under $CREDENTIALS_DIRECTORY that only this service can read, never
# the unit file, an EnvironmentFile or config.yaml. They are exported here
# under the names config.yaml's `env:` references resolve:
#   kek              -> HYPERMIND_KEK              (secrets.kek_source, required)
#   model_api_key    -> JARVIS_MODEL_API_KEY       (agent.secret_ref; empty = none, e.g. a local model)
#   superuser_token  -> HYPERMIND_SUPERUSER_TOKEN  (optional; absent or empty = no superuser)
#
# The server binds loopback only: Caddy or cloudflared on this host is its
# only client, so forwarded headers are trusted from 127.0.0.1 alone. The
# access log is off — a request line can carry an OIDC `code`/`state`.
set -eu

: "${CREDENTIALS_DIRECTORY:?jarvis-start.sh runs under systemd with LoadCredential=kek:...}"
: "${JARVIS_HOME:=/opt/jarvis}"
: "${JARVIS_PORT:=8000}"

HYPERMIND_KEK="$(cat "$CREDENTIALS_DIRECTORY/kek")"
export HYPERMIND_KEK

if [ -s "$CREDENTIALS_DIRECTORY/model_api_key" ]; then
    JARVIS_MODEL_API_KEY="$(cat "$CREDENTIALS_DIRECTORY/model_api_key")"
    export JARVIS_MODEL_API_KEY
fi

if [ -s "$CREDENTIALS_DIRECTORY/superuser_token" ]; then
    HYPERMIND_SUPERUSER_TOKEN="$(cat "$CREDENTIALS_DIRECTORY/superuser_token")"
    export HYPERMIND_SUPERUSER_TOKEN
fi

# Logging (deploy/logging.json): one line per record, level and logger name,
# to stderr — journald adds time, unit and PID. The server's own `hypermind.*`
# loggers at INFO; libraries at WARNING (httpx logs whole request URLs at INFO).
cd "$JARVIS_HOME"
exec "$JARVIS_HOME/.venv/bin/uvicorn" server.composition.main:app \
    --host 127.0.0.1 --port "$JARVIS_PORT" \
    --proxy-headers --forwarded-allow-ips=127.0.0.1 \
    --log-config "$JARVIS_HOME/deploy/logging.json" \
    --no-access-log
