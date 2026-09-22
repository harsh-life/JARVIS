"""Uvicorn entrypoint: `uvicorn server.gateway.main:app` (see docs/RUNNING_FOUNDATION.md).

Loads config via server.config.load_config() (fail-closed — see
server/config/loader.py) and builds the app from it.
"""

from __future__ import annotations

from server.config import load_config
from server.gateway.app import create_app

app = create_app(config=load_config())
