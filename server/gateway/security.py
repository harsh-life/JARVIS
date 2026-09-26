"""The security composition root.

This is the one place the independent halves of the engine are wired together.
`server/graph` (the decision half) and `server/capabilities` (the policy half)
never import each other (16 §5) — they meet here, through the Protocols in
`server/graph/ports.py`. Keeping the wiring in a single function means there is
one place to read to know what the engine actually consults, and no module can
quietly substitute its own policy.

Startup order matters and is deliberate (12 §3, 15 §3):

1. the app is built;
2. the SecretStore is constructed **locked**;
3. an operator supplies the KEK out-of-band, and `unlock_secret_store` unwraps
   the DEK.

Until step 3, every secret resolution fails closed — so an endpoint that needs a
credential returns an explicit failure rather than running in a degraded mode
(12 §8). Nothing auto-unlocks from disk, including after a crash.
"""

from __future__ import annotations

from dataclasses import dataclass

from server.auth.device import DeviceService
from server.auth.login import OIDCLoginFlow
from server.auth.oidc import GoogleOIDCProvider, OIDCProvider
from server.auth.repository import AuthRepository
from server.auth.sessions import SessionService
from server.capabilities.confirmation import ConfirmationService
from server.capabilities.grants import CapabilityGrantService
from server.capabilities.policy import FloorPolicyAdapter, RiskPolicyAdapter
from server.config import AppConfig
from server.graph.authorization import AuthorizationEngine
from server.graph.repository import GraphRepository
from server.graph.resources import SecurityCoreResourceLoader
from server.graph.service import GraphService
from server.secrets.kek import resolve_kek
from server.secrets.store import EncryptedLocalSecretStore
from server.storage import StorageBackend

OIDC_CALLBACK_PATH = "/api/v1/auth/oidc/callback"


@dataclass(frozen=True)
class SecurityCore:
    """Every deterministic security service, assembled.

    Later branches receive this object rather than constructing their own
    engine — which is how §19 of the security-core scope holds: a future tool
    asks `engine.authorize(...)`, it does not re-implement authorization.
    """

    secret_store: EncryptedLocalSecretStore
    auth_repository: AuthRepository
    login_flow: OIDCLoginFlow
    devices: DeviceService
    sessions: SessionService
    graphs: GraphService
    graph_repository: GraphRepository
    capability_grants: CapabilityGrantService
    confirmations: ConfirmationService
    engine: AuthorizationEngine
    kek_source: str
    # The engine's resource loader, exposed so the composition root can register
    # a loader for a store outside this layer (`mem0fact`). Additive only.
    resource_loader: SecurityCoreResourceLoader


def build_security_core(
    config: AppConfig, *, oidc_provider: OIDCProvider | None = None
) -> SecurityCore:
    """Assemble the security core from configuration.

    `oidc_provider` is injectable so tests can drive the full login flow against
    a provider with locally generated keys. There is no in-tree fake and no
    "test mode" flag: the production path is the only path, and a test supplies a
    different provider the same way a future Microsoft/Apple adapter would
    (AUTH-001).
    """

    secret_store = EncryptedLocalSecretStore()
    auth_repository = AuthRepository()
    graph_repository = GraphRepository()
    capability_grants = CapabilityGrantService()
    confirmations = ConfirmationService()

    provider = oidc_provider or GoogleOIDCProvider(
        client_id=config.security.oidc.client_id,
        issuer=config.security.oidc.issuer,
    )

    resource_loader = SecurityCoreResourceLoader()
    engine = AuthorizationEngine(
        memberships=graph_repository,
        resources=resource_loader,
        capabilities=capability_grants,
        risk=RiskPolicyAdapter(),
        floor=FloorPolicyAdapter(),
        confirmations=confirmations,
    )

    return SecurityCore(
        secret_store=secret_store,
        auth_repository=auth_repository,
        login_flow=OIDCLoginFlow(
            provider,
            redirect_uri=f"{config.server.base_url.rstrip('/')}{OIDC_CALLBACK_PATH}",
            repository=auth_repository,
        ),
        devices=DeviceService(secret_store=secret_store, repository=auth_repository),
        sessions=SessionService(repository=auth_repository),
        graphs=GraphService(repository=graph_repository),
        graph_repository=graph_repository,
        capability_grants=capability_grants,
        confirmations=confirmations,
        engine=engine,
        kek_source=config.secrets.kek_source,
        resource_loader=resource_loader,
    )


async def unlock_secret_store(
    core: SecurityCore, storage: StorageBackend, *, bootstrap: bool = False
) -> None:
    """Perform 12 §3's unlock step using the configured KEK source.

    `bootstrap=True` creates the store's DEK if it does not exist yet — the
    first-run path from 15 §3. It is idempotent, so re-running bootstrap on an
    initialized store is an unlock and never replaces the key existing ciphertext
    was encrypted under.

    The KEK is read here, used to unwrap the DEK, and not retained: it is a local
    that goes out of scope. `SecurityCore` has a `kek_source` (the *descriptor*)
    and never the key.
    """

    kek = resolve_kek(core.kek_source)
    async with storage.session() as session:
        if bootstrap:
            await core.secret_store.bootstrap(session, kek)
        else:
            await core.secret_store.unlock(session, kek)
        await session.commit()
