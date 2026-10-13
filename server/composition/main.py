"""Uvicorn entrypoint: `uvicorn server.composition.main:app`.

Loads config fail-closed (server/config/loader.py) and builds the full
application. `unlock_secrets_on_startup=True` performs 12 §3's unlock: if the KEK
is unavailable the server refuses to start, rather than serving in the silently
secretless mode 12 §8 forbids. `reconcile_tasks_on_startup=True` closes every
task the previous run left unfinished (a restart fails them closed) before the
first request is served. `check_deployment` refuses a configuration a deployed
server must not run with (a remote primary without budgets or without an
invite-only allow-list — register §2N, OD-PILOT-2/5).
"""

from __future__ import annotations

from server.composition import build_application
from server.config import load_config
from server.config.preconditions import check_deployment

app = build_application(
    check_deployment(load_config()), unlock_secrets_on_startup=True, reconcile_tasks_on_startup=True
)
