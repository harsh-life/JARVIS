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
from server.gateway.agent_port import AgentTaskPort
from server.gateway.control_port import SupervisorControlPort
from server.gateway.routers import agent, auth, capabilities, control, graphs, health, sessions
from server.gateway.security import SecurityCore, build_security_core
from server.gateway.security_errors import install_security_error_handlers
from server.storage import StorageBackend

API_V1_PREFIX = "/api/v1"


def create_app(
    *,
    config: AppConfig | None = None,
    storage: StorageBackend | None = None,
    security: SecurityCore | None = None,
    unlock_secrets_on_startup: bool = False,
    agent_tasks: AgentTaskPort | None = None,
    supervisor_control: SupervisorControlPort | None = None,
) -> FastAPI:
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
        if unlock_secrets_on_startup:
            # 12 §3's unlock step. Any failure propagates and the server refuses
            # to serve: a running Hypermind whose SecretStore is locked cannot
            # register a device or verify a credential, so starting anyway would
            # be the "degraded, silently-secretless mode" 12 §8 forbids.
            from server.gateway.security import unlock_secret_store

            await unlock_secret_store(app.state.security, app.state.storage)
        try:
            yield
        finally:
            core: SecurityCore | None = getattr(app.state, "security", None)
            if core is not None:
                # Drop the unwrapped DEK rather than leaving it in the memory of
                # a process that is shutting down (12 §3 — a restart requires a
                # fresh unlock; nothing auto-unlocks from disk).
                core.secret_store.lock()
            backend: StorageBackend | None = getattr(app.state, "storage", None)
            if backend is not None:
                await backend.dispose()

    app = FastAPI(title="Hypermind Track B — Gateway", lifespan=_lifespan)

    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)
    # Installed after the foundation handlers so the security mappings take
    # precedence for their own exception types (02 §1.7).
    install_security_error_handlers(app)

    v1 = APIRouter(prefix=API_V1_PREFIX)
    v1.include_router(health.router)
    v1.include_router(auth.router)
    v1.include_router(sessions.router)
    v1.include_router(graphs.router)
    v1.include_router(capabilities.router)
    v1.include_router(agent.router)
    v1.include_router(control.router)
    app.include_router(v1)

    # The agent runtime is assembled above this layer (server/composition/) and
    # handed in through the `AgentTaskPort` Protocol: the gateway sits below
    # `server.agent` in the layering (16 §2) and cannot import it. Without one,
    # the agent endpoints answer `503 dependency_unavailable` explicitly.
    app.state.agent_tasks = agent_tasks
    # 18 §5.4 — likewise assembled above this layer. Without one, the control
    # endpoints answer `503` (after superuser authentication).
    app.state.supervisor_control = supervisor_control
    app.state.intelligence_enabled = bool(config.intelligence.enabled) if config else False

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

    # The security core is assembled here and attached alongside storage, for the
    # same reason storage is: deterministic construction, not a lifespan side
    # effect. It is built **locked** — the SecretStore cannot resolve anything
    # until an operator supplies the KEK out-of-band via `unlock_secret_store`
    # (12 §3). A caller may pass a pre-built core to supply a different OIDC
    # provider (AUTH-001's replaceable identity provider).
    if security is not None:
        app.state.security = security
    elif config is not None:
        app.state.security = build_security_core(config)
    else:
        raise RuntimeError(
            "create_app() requires either `config` or a pre-built `security` core"
        )

    return app
