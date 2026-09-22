"""Fixtures for the runtime-branch suite.

Deliberately wires the **real** Security Core primitives — the same
`AuthorizationEngine`, `ConfirmationService`, `CapabilityGrantService`, and
`SecretStore` `build_security_core` assembles — exactly as
`tests/security_core/conftest.py` does, and for the same reason stated there:
"Nothing here stubs a security component." A test proving "the model cannot
bypass the authorization engine" is only meaningful if the engine in the test
is the real one.

The only things faked here are the pieces this branch's own instructions say
belong to *other*, not-yet-implemented branches: the model itself
(`ScriptedModelInvoker`) and a concrete tool's execution primitive
(`RecordingToolExecutor`). Everything between the fake model and the fake
tool — parsing, authorization, confirmation, dispatch, bounds — is the real
runtime code under test.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
import pytest_asyncio

from server.capabilities.confirmation import ConfirmationService
from server.capabilities.grants import CapabilityGrantService
from server.capabilities.policy import FloorPolicyAdapter, RiskPolicyAdapter
from server.gateway.runtime import (
    EngineAuthorizer,
    GatewayEventRecorder,
    RegistryToolCatalog,
    RegistryToolDispatcher,
)
from server.graph.authorization import AuthorizationEngine
from server.graph.repository import GraphRepository
from server.graph.resources import SecurityCoreResourceLoader
from server.graph.service import GraphService
from server.memory.hydrator import NullMemoryHydrator
from server.secrets.kek import resolve_kek
from server.secrets.store import EncryptedLocalSecretStore
from server.security.audit import AuditLogger
from server.storage import SQLAlchemyStorageBackend
from server.storage.models import User
from server.tools.executor import ToolExecutor
from server.tools.registry import ToolRegistry
from shared.schemas.agent_config import ToolConfiguration, ToolContract
from shared.schemas.authorization import Principal
from shared.schemas.enums import RiskCategory, UserStatus
from shared.schemas.runtime import (
    GenerationPolicy,
    ModelMessage,
    ModelResult,
    ModelUnavailable,
    RuntimeBounds,
    ToolInvocationRequest,
    ToolResult,
)
from tests.support import TEST_KEK_ENV_VAR, make_test_kek

pytestmark = pytest.mark.asyncio


# ── Security Core wiring (identical shape to tests/security_core/conftest.py) ──


@pytest_asyncio.fixture
async def storage(tmp_path) -> AsyncIterator[SQLAlchemyStorageBackend]:
    db_path = tmp_path / f"rt_{uuid.uuid4().hex}.db"
    backend = SQLAlchemyStorageBackend(f"sqlite+aiosqlite:///{db_path}")
    await backend.init_models()
    try:
        yield backend
    finally:
        await backend.dispose()


@pytest.fixture
def kek_value(monkeypatch: pytest.MonkeyPatch) -> str:
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
    """The production engine — identical wiring to `build_security_core`."""

    return AuthorizationEngine(
        memberships=graph_repository,
        resources=SecurityCoreResourceLoader(),
        capabilities=grants,
        risk=RiskPolicyAdapter(),
        floor=FloorPolicyAdapter(),
        confirmations=confirmations,
    )


@pytest.fixture
def authorizer(engine: AuthorizationEngine, confirmations: ConfirmationService, db, audit) -> EngineAuthorizer:
    """The real runtime-facing `Authorizer` adapter — this is the actual
    class `server/gateway/runtime.py` builds per request, not a test double."""

    return EngineAuthorizer(engine=engine, confirmations=confirmations, session=db, audit=audit)


async def make_user(db, *, subject: str) -> User:
    import datetime

    user = User(
        oidc_subject=subject,
        oidc_issuer="https://accounts.google.test",
        display_name=subject,
        status=UserStatus.ACTIVE,
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(user)
    await db.flush()
    return user


@pytest_asyncio.fixture
async def alice(db) -> User:
    return await make_user(db, subject="alice-subject")


@pytest_asyncio.fixture
async def bob(db) -> User:
    return await make_user(db, subject="bob-subject")


def principal_for(user: User, *, graph_id: uuid.UUID | None = None) -> Principal:
    """Mirrors `tests/security_core/test_authorization.py`'s helper: a
    principal for a user with synthetic device/session ids — how a principal
    is *established* is security-core's own suite's subject, not this one's.
    """

    return Principal(
        user_id=user.user_id,
        device_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        active_graph_id=graph_id,
    )


# ── runtime-only pieces ──────────────────────────────────────────────────────


@pytest.fixture
def small_bounds() -> RuntimeBounds:
    """Small enough that a runaway test completes in well under a second."""

    return RuntimeBounds(
        max_iterations=4,
        max_tool_calls=3,
        max_model_calls=4,
        max_model_tool_nesting_depth=1,
        max_parse_retries=1,
        wall_clock_timeout_seconds=30.0,
        model_call_timeout_seconds=5.0,
        max_cost=0.0,
    )


@dataclass
class ScriptedModelInvoker:
    """A deterministic `ModelInvoker` test double — the one component this
    branch's own instructions say the runtime must never trust, so tests
    control it explicitly rather than calling a real model.

    `responses` is consumed one per call; a `ModelUnavailable` instance in
    the list is *raised* rather than returned, so a test can script a mid-task
    provider outage.
    """

    responses: list[str | Exception] = field(default_factory=list)
    calls: list[list[ModelMessage]] = field(default_factory=list)

    async def invoke(
        self, messages: list[ModelMessage], policy: GenerationPolicy, timeout: float
    ) -> ModelResult:
        self.calls.append(list(messages))
        if not self.responses:
            raise ModelUnavailable("ScriptedModelInvoker ran out of scripted responses")
        next_item = self.responses.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return ModelResult(content=next_item, tokens_used=10)

    async def health(self) -> bool:
        return True


class RecordingToolExecutor(ToolExecutor):
    """A `ToolExecutor` test double for one concrete tool — the one piece
    this branch's own instructions say belongs to the Execution branch, so
    tests script its behaviour rather than exercising a real filesystem/
    network/device primitive."""

    def __init__(self, *, result: ToolResult | Exception | None = None) -> None:
        self.result = result
        self.requests: list[ToolInvocationRequest] = []

    async def execute(self, request: ToolInvocationRequest) -> ToolResult:
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        if self.result is not None:
            return self.result
        return ToolResult(tool_id=request.tool_id, success=True, output="ok")


def build_registry(*, tool_id: str, capability: str, executor: ToolExecutor) -> ToolRegistry:
    """A `ToolRegistry` with one registered+enabled test tool, gated by
    `capability` — a real capability from `server.capabilities.registry`'s
    closed allow-list, never an invented one."""

    registry = ToolRegistry()
    contract = ToolContract(
        tool_id=tool_id,
        version="1",
        description="test tool",
        input_schema={"type": "object"},
        output_schema={"type": "string"},
        required_capability=capability,
        network={},
        filesystem={},
        risk_category=RiskCategory.LOW_WRITE,
        timeout_seconds=5,
        confirmation_required=False,
        failure_behavior="observation",
        audit="every invocation",
    )
    registry.register(contract, ToolConfiguration(tool_id=tool_id, enabled=True), executor)
    return registry


def build_tool_dispatcher(
    *, registry: ToolRegistry, allowed_tool_ids: frozenset[str], db, audit, principal: Principal
) -> RegistryToolDispatcher:
    return RegistryToolDispatcher(
        registry=registry, allowed_tool_ids=allowed_tool_ids, session=db, audit=audit, principal=principal
    )


def build_tool_catalog(*, registry: ToolRegistry, allowed_tool_ids: frozenset[str]) -> RegistryToolCatalog:
    return RegistryToolCatalog(registry=registry, allowed_tool_ids=allowed_tool_ids)


def build_event_recorder(*, audit, principal: Principal) -> GatewayEventRecorder:
    return GatewayEventRecorder(audit=audit, principal=principal)


@pytest.fixture
def memory_hydrator() -> NullMemoryHydrator:
    return NullMemoryHydrator()
