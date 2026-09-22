"""Fixtures for the security-core suite.

17 §6 requires multi-user fixtures — "≥2 users, ≥1 shared graph, private +
shared resources per user — the substrate for every isolation test" — because a
single-user fixture cannot fail AZ-T1, the most important test in Track B. The
`world` fixture below is that substrate, and most tests here are assertions
about who can see what inside it.

Nothing here stubs a security component. The authorization engine, the
SecretStore, the grant service, and the risk policy are the production objects,
wired exactly as `build_security_core` wires them. The only substitution is the
OIDC provider, which mints real RS256 tokens with a locally generated key
(`tests/support.py`) — the same seam a future Microsoft/Apple adapter would use
(AUTH-001).
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio

from server.auth.device import build_device_proof
from server.capabilities.confirmation import ConfirmationService
from server.capabilities.grants import CapabilityGrantService
from server.capabilities.policy import FloorPolicyAdapter, RiskPolicyAdapter
from server.gateway.app import API_V1_PREFIX, create_app
from server.gateway.security import SecurityCore, build_security_core
from server.graph.authorization import AuthorizationEngine
from server.graph.repository import GraphRepository
from server.graph.resources import SecurityCoreResourceLoader
from server.graph.service import GraphService
from server.secrets.kek import resolve_kek
from server.secrets.store import EncryptedLocalSecretStore
from server.security.audit import AuditLogger
from server.storage import SQLAlchemyStorageBackend
from server.storage.models import FileResource, ScheduledJob, User
from shared.schemas.enums import GraphType, MembershipRole, UserStatus, Visibility
from tests.support import (
    TEST_KEK_ENV_VAR,
    LocalOIDCProvider,
    make_test_config,
    make_test_kek,
)


@pytest_asyncio.fixture
async def storage(tmp_path) -> AsyncIterator[SQLAlchemyStorageBackend]:
    db_path = tmp_path / f"sec_{uuid.uuid4().hex}.db"
    backend = SQLAlchemyStorageBackend(f"sqlite+aiosqlite:///{db_path}")
    await backend.init_models()
    try:
        yield backend
    finally:
        await backend.dispose()


@pytest.fixture
def kek_value(monkeypatch: pytest.MonkeyPatch) -> str:
    """A fresh KEK per test, published through the env var config names.

    Supplied the way an operator supplies it (12 §3: out-of-band, at unlock
    time) rather than by handing the store raw bytes, so the config→env→unlock
    path is exercised by every test that touches a secret.
    """

    value = make_test_kek()
    monkeypatch.setenv(TEST_KEK_ENV_VAR, value)
    return value


@pytest_asyncio.fixture
async def db(storage: SQLAlchemyStorageBackend) -> AsyncIterator:
    async with storage.session() as session:
        yield session


@pytest.fixture
def audit(db) -> AuditLogger:
    return AuditLogger(db, request_id=uuid.uuid4())


@pytest_asyncio.fixture
async def store(db, kek_value: str) -> EncryptedLocalSecretStore:
    """A bootstrapped, unlocked SecretStore."""

    secret_store = EncryptedLocalSecretStore()
    await secret_store.bootstrap(db, resolve_kek(f"env:{TEST_KEK_ENV_VAR}"))
    return secret_store


@pytest.fixture
def grants() -> CapabilityGrantService:
    return CapabilityGrantService()


@pytest.fixture
def confirmations() -> ConfirmationService:
    return ConfirmationService()


@pytest.fixture
def graph_repository() -> GraphRepository:
    return GraphRepository()


@pytest.fixture
def graph_service(graph_repository: GraphRepository) -> GraphService:
    return GraphService(repository=graph_repository)


@pytest.fixture
def engine(
    graph_repository: GraphRepository,
    grants: CapabilityGrantService,
    confirmations: ConfirmationService,
) -> AuthorizationEngine:
    """The production engine, wired as `build_security_core` wires it."""

    return AuthorizationEngine(
        memberships=graph_repository,
        resources=SecurityCoreResourceLoader(),
        capabilities=grants,
        risk=RiskPolicyAdapter(),
        floor=FloorPolicyAdapter(),
        confirmations=confirmations,
    )


# ── the multi-user substrate (17 §6) ────────────────────────────────────────


@dataclass
class World:
    """Two users, one shared graph, and a private + shared resource each.

    `outsider` is a third user in no graph at all, which is what makes the
    anti-enumeration assertions (AZ-T3) distinguishable from the
    member-but-not-owner ones (AZ-T1).
    """

    alice: User
    bob: User
    outsider: User
    graph_id: uuid.UUID
    alice_private_file: FileResource
    alice_shared_file: FileResource
    bob_private_file: FileResource
    alice_private_job: ScheduledJob


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


async def _make_user(db, *, subject: str) -> User:
    user = User(
        oidc_subject=subject,
        oidc_issuer="https://accounts.google.test",
        display_name=subject,
        status=UserStatus.ACTIVE,
        created_at=_now(),
    )
    db.add(user)
    await db.flush()
    return user


def _file(
    *,
    owner: User,
    graph_id: uuid.UUID | None,
    visibility: Visibility,
    name: str,
) -> FileResource:
    return FileResource(
        owner_user_id=owner.user_id,
        source_user_id=owner.user_id,
        graph_id=graph_id,
        visibility=visibility,
        sandbox_root=f"/sandbox/{owner.user_id}",
        relative_path=name,
        size_bytes=10,
        created_at=_now(),
    )


@pytest_asyncio.fixture
async def world(db, audit: AuditLogger, graph_service: GraphService) -> World:
    alice = await _make_user(db, subject="alice-subject")
    bob = await _make_user(db, subject="bob-subject")
    outsider = await _make_user(db, subject="outsider-subject")

    graph = await graph_service.create_graph(
        db,
        name="shared graph",
        graph_type=GraphType.SHARED,
        creator_user_id=alice.user_id,
        audit=audit,
    )
    await graph_service.approve_member(
        db,
        graph_id=graph.graph_id,
        approver_user_id=alice.user_id,
        user_id=bob.user_id,
        role=MembershipRole.MEMBER,
        audit=audit,
    )

    alice_private = _file(
        owner=alice, graph_id=graph.graph_id, visibility=Visibility.PRIVATE, name="alice-private.txt"
    )
    alice_shared = _file(
        owner=alice, graph_id=graph.graph_id, visibility=Visibility.GRAPH, name="alice-shared.txt"
    )
    bob_private = _file(
        owner=bob, graph_id=graph.graph_id, visibility=Visibility.PRIVATE, name="bob-private.txt"
    )
    job = ScheduledJob(
        owner_user_id=alice.user_id,
        source_user_id=alice.user_id,
        graph_id=graph.graph_id,
        visibility=Visibility.PRIVATE,
        task_reason="because the user asked",
        schedule="daily",
        created_at=_now(),
    )
    db.add_all([alice_private, alice_shared, bob_private, job])
    await db.flush()

    return World(
        alice=alice,
        bob=bob,
        outsider=outsider,
        graph_id=graph.graph_id,
        alice_private_file=alice_private,
        alice_shared_file=alice_shared,
        bob_private_file=bob_private,
        alice_private_job=job,
    )


# ── the HTTP harness ────────────────────────────────────────────────────────


@dataclass
class Api:
    client: httpx.AsyncClient
    provider: LocalOIDCProvider
    core: SecurityCore
    storage: SQLAlchemyStorageBackend

    async def oidc_login(self) -> str:
        """Drive the real login flow; return the bootstrap token.

        The `state` is read back out of the redirect URL rather than reached for
        in the database, so the test travels the same path a client does.
        """

        start = await self.client.get(f"{API_V1_PREFIX}/auth/oidc/start")
        assert start.status_code == 200, start.text
        state = parse_qs(urlparse(start.json()["redirect_url"]).query)["state"][0]

        callback = await self.client.get(
            f"{API_V1_PREFIX}/auth/oidc/callback",
            params={"code": "test-authorization-code", "state": state},
        )
        assert callback.status_code == 200, callback.text
        return callback.json()["bootstrap_token"]

    async def register_device(self, bootstrap_token: str) -> tuple[uuid.UUID, str]:
        resp = await self.client.post(
            f"{API_V1_PREFIX}/devices",
            json={"platform": "android"},
            headers={"Authorization": f"Bearer {bootstrap_token}"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        return uuid.UUID(body["device_id"]), body["device_credential"]

    async def issue_token(self, device_id: uuid.UUID, credential: str) -> httpx.Response:
        return await self.client.post(
            f"{API_V1_PREFIX}/sessions/token",
            json={
                "device_credential": build_device_proof(
                    device_id=device_id, device_credential=credential
                )
            },
        )

    async def onboard(self, subject: str = "onboard-subject") -> "Onboarded":
        """The whole happy path: login → register → token."""

        self.provider.subject = subject
        bootstrap = await self.oidc_login()
        device_id, credential = await self.register_device(bootstrap)
        token_resp = await self.issue_token(device_id, credential)
        assert token_resp.status_code == 200, token_resp.text
        return Onboarded(
            device_id=device_id,
            credential=credential,
            access_token=token_resp.json()["access_token"],
        )


@dataclass
class Onboarded:
    device_id: uuid.UUID
    credential: str
    access_token: str

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


@pytest_asyncio.fixture
async def api(
    storage: SQLAlchemyStorageBackend, kek_value: str
) -> AsyncIterator[Api]:
    provider = LocalOIDCProvider()
    config = make_test_config()
    core = build_security_core(config, oidc_provider=provider)

    # 12 §3's bootstrap step, performed the way 15 §3 documents it: before the
    # server starts serving, with the KEK supplied out-of-band.
    async with storage.session() as session:
        await core.secret_store.bootstrap(session, resolve_kek(f"env:{TEST_KEK_ENV_VAR}"))
        await session.commit()

    # `config=config` lets `create_app` build a `RuntimeCore` automatically
    # (the runtime branch's addition) — these tests exercise auth/graph/
    # capabilities endpoints, not the agent runtime, but `create_app` still
    # requires *some* way to construct `app.state.runtime`, and `config` is
    # already sitting right here from building `core` above.
    app = create_app(storage=storage, security=core, config=config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield Api(client=client, provider=provider, core=core, storage=storage)
