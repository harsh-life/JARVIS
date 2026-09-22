"""Uvicorn entrypoint: `uvicorn server.gateway.main:app` (see docs/RUNNING_SECURITY_CORE.md).

Loads config via server.config.load_config() (fail-closed — see
server/config/loader.py) and builds the app from it.

`unlock_secrets_on_startup=True` performs 12 §3's unlock step during startup. If
the KEK is unavailable the startup raises and the server does not serve: a
Hypermind whose SecretStore is locked cannot register a device or verify a
credential, so accepting requests anyway would be the degraded, silently
secretless mode 12 §8 forbids.
"""

from __future__ import annotations

from server.config import load_config
from server.gateway.app import create_app

app = create_app(config=load_config(), unlock_secrets_on_startup=True)
