"""FastAPI application factory.

Versioning (02_API_PROTOCOL.md §1.5): "All endpoints are namespaced
`/api/v1/...`." This factory mounts exactly one versioned router; a future
breaking change adds a sibling `/api/v2` router rather than mutating this
one (the doc's own guidance).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import APIRouter, FastAPI

from server.config import AppConfig
from server.gateway.errors import install_error_handlers
from server.gateway.request_context import RequestIdMiddleware
from server.gateway.routers import health
from server.storage import StorageBackend

API_V1_PREFIX = "/api/v1"


def create_app(*, config: AppConfig | None = None, storage: StorageBackend | None = None) -> FastAPI:
    """Build the FastAPI app.

    `storage` may be injected directly (tests construct their own throwaway
    backend and pass it in); otherwise a `SQLAlchemyStorageBackend` is built
    from `config.database_url` at startup. Foundation never runs
    `init_models()`/migrations implicitly here — schema initialization is a
    documented bootstrap step (15_CONFIGURATION_SELF_HOSTING.md §3), not an
    app-startup side effect, so a misconfigured/un-migrated deployment fails
    loudly rather than silently creating an unexpected schema.
    """

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            backend: StorageBackend | None = getattr(app.state, "storage", None)
            if backend is not None:
                await backend.dispose()

    app = FastAPI(title="Hypermind Track B — Gateway (foundation branch)", lifespan=_lifespan)

    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)

    v1 = APIRouter(prefix=API_V1_PREFIX)
    v1.include_router(health.router)
    app.include_router(v1)

    # Storage is attached synchronously at construction time, not deferred
    # to lifespan startup: deterministic initialization (§11), and it does
    # not depend on a test client actually driving the ASGI lifespan
    # protocol (not every ASGI transport does). Lifespan is used only for
    # shutdown cleanup above.
    if storage is not None:
        app.state.storage = storage
    elif config is not None:
        from server.storage import SQLAlchemyStorageBackend

        app.state.storage = SQLAlchemyStorageBackend(config.database_url)
    else:
        raise RuntimeError(
            "create_app() requires either `config` or a pre-built `storage` backend"
        )

    return app
