"""Harness for the runtime suite.

Nothing security-relevant is stubbed. The app is built by the production
composition root (`build_application`) around the production Security Core; users
are onboarded through the real OIDC → device → token flow; every tool operation
is authorized by the real `AuthorizationEngine`; confirmations are real
`ConfirmationService` tokens; usage is the real ledger.

Two things are substituted, through the same seams a later branch uses:

* the **model** — a scripted `ModelProvider`, so a test controls exactly what the
  agent proposes (including adversarial proposals), and
* the **tool adapters** — in-memory fakes registered through the production
  `ToolRegistry.register`, standing in for the `09`/`08` adapters that do not
  exist yet. They record every invocation, which is how a test proves an action
  did *not* run.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from server.auth.device import build_device_proof
from server.composition import build_application
from server.config.schema import AppConfig
from server.gateway.app import API_V1_PREFIX
from server.gateway.security import SecurityCore, build_security_core
from server.memory.hydration import MemoryCandidate
from server.models.factory import build_provider
from server.models.provider import ChatMessage, ModelResult, ModelSpec, ModelUnavailable
from server.secrets.kek import resolve_kek
from server.storage import SQLAlchemyStorageBackend
from server.storage.models import Device, FileResource
from server.tools.registry import ToolDefinition
from shared.schemas.agent import ExecutionPlatform, OperationSpec, ToolInvocation, ToolOutput
from shared.schemas.agent_config import ToolContract
from shared.schemas.authorization import Operation, ResourceType
from shared.schemas.enums import RiskCategory, Visibility
from tests.support import TEST_ISSUER, TEST_KEK_ENV_VAR, LocalOIDCProvider, make_test_kek, TEST_CLIENT_ID

pytestmark = pytest.mark.asyncio


# ── the scripted model ─────────────────────────────────────────────────────


def say(payload: dict | str) -> str:
    return payload if isinstance(payload, str) else json.dumps(payload)


def final(text: str = "done") -> str:
    return say({"type": "final_answer", "content": text})


def call(tool: str, operation: str, *, ref: str | None = None, args: dict | None = None,
         platform: str | None = None, **extra: Any) -> str:
    payload: dict[str, Any] = {"type": "tool_call", "tool": tool, "operation": operation,
                               "arguments": args or {}}
    if ref is not None:
        payload["resource_ref"] = ref
    if platform is not None:
        payload["platform"] = platform
    payload.update(extra)
    return say(payload)


def ask(*capabilities: str, scope: dict | None = None) -> str:
    return say({
        "type": "request_capabilities",
        "capabilities": [{"capability": c, **({"resource_scope": scope} if scope else {})}
                         for c in capabilities],
    })


class ScriptedModel:
    """A `ModelProvider` whose outputs a test controls.

    Each entry in the script is a string (returned as the model's text), an
    exception (raised), or a callable taking the messages (for side effects such
    as revoking a grant between steps). An exhausted script answers — it never
    loops silently.
    """

    def __init__(self, name: str = "scripted-primary") -> None:
        self.spec = ModelSpec(provider="ollama", model=name)
        self.script: deque = deque()
        self.seen: list[list[ChatMessage]] = []

    def push(self, *entries: Any) -> "ScriptedModel":
        self.script.extend(entries)
        return self

    async def invoke(self, messages: Sequence[ChatMessage], *, timeout: float) -> ModelResult:
        self.seen.append(list(messages))
        entry = self.script.popleft() if self.script else final("(script exhausted)")
        if isinstance(entry, BaseException):
            raise entry
        if callable(entry):
            entry = entry(messages)
            if hasattr(entry, "__await__"):
                entry = await entry
        return ModelResult(content=entry, prompt_tokens=10, completion_tokens=5)

    async def health(self) -> bool:
        return True

    def all_text(self) -> str:
        return "\n".join(m.content for call_ in self.seen for m in call_)


# ── in-memory tool adapters ────────────────────────────────────────────────


class RecordingAdapter:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[ToolInvocation] = []

    async def execute(self, invocation: ToolInvocation) -> ToolOutput:
        self.calls.append(invocation)
        detail = invocation.resource_ref or json.dumps(dict(invocation.arguments), sort_keys=True)
        return ToolOutput(ok=True, content=f"{self.name}:{invocation.operation}:{detail}")

    def operations(self) -> list[str]:
        return [c.operation for c in self.calls]


def contract(tool_id: str, capability: str, risk: RiskCategory, confirm: bool, **overrides) -> ToolContract:
    payload = dict(
        tool_id=tool_id, version="1", description=f"test tool {tool_id}",
        input_schema={"type": "object"}, output_schema={"type": "string"},
        required_capability=capability, network={}, filesystem={},
        risk_category=risk, timeout_seconds=5, confirmation_required=confirm,
        failure_behavior="observation", audit="every invocation",
    )
    payload.update(overrides)
    return ToolContract(**payload)


FILE = ResourceType.FILERESOURCE.value
ACTION = ResourceType.TOOL_ACTION.value


def file_tools(read_adapter: RecordingAdapter, write_adapter: RecordingAdapter) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            contract=contract("files.read", "file.read", RiskCategory.LOW_READ, False),
            operations={
                "read_file": OperationSpec(FILE, Operation.READ.value, requires_resource_ref=True),
                "list_directory": OperationSpec(ACTION, Operation.CREATE.value),
            },
            adapters={ExecutionPlatform.SERVER: read_adapter},
        ),
        ToolDefinition(
            contract=contract("files.write", "file.write", RiskCategory.HIGH_IRREVERSIBLE, True),
            operations={
                "create_file": OperationSpec(FILE, Operation.CREATE.value),
                "write_file": OperationSpec(FILE, Operation.WRITE.value, requires_resource_ref=True),
                "delete_file": OperationSpec(FILE, Operation.DELETE.value, requires_resource_ref=True),
                "bulk_delete": OperationSpec(ACTION, Operation.CREATE.value),
            },
            adapters={ExecutionPlatform.SERVER: write_adapter},
        ),
    ]


def android_ui_tool(adapter: RecordingAdapter) -> ToolDefinition:
    return ToolDefinition(
        contract=contract("ui.app", "app.interact", RiskCategory.CONSEQUENTIAL, True),
        operations={
            "tap": OperationSpec(ACTION, Operation.CREATE.value),
            "input_text": OperationSpec(ACTION, Operation.CREATE.value),
            "read_screen_element": OperationSpec(ACTION, Operation.CREATE.value),
        },
        adapters={ExecutionPlatform.ANDROID: adapter},
    )


# ── in-memory Mem0 stand-in ────────────────────────────────────────────────


@dataclass
class Fact:
    content: str
    owner: uuid.UUID
    visibility: Visibility
    graph_id: uuid.UUID | None
    fact_id: str = field(default_factory=lambda: str(uuid.uuid4()))


class InMemoryMemoryStore:
    """Implements `MemoryStore` honestly: the visibility filter is in the query."""

    def __init__(self) -> None:
        self.facts: list[Fact] = []
        self.queries: list[dict] = []

    def add(self, fact: Fact) -> Fact:
        self.facts.append(fact)
        return fact

    def _score(self, query: str, content: str) -> float:
        words = {w.lower().strip(".,") for w in query.split()}
        return float(sum(1 for w in content.lower().split() if w.strip(".,") in words))

    async def search(self, *, query, owner_user_id, readable_graph_ids, limit):
        self.queries.append({"owner": owner_user_id, "graphs": set(readable_graph_ids), "limit": limit})
        matches = [
            f for f in self.facts
            if f.owner == owner_user_id
            or (f.visibility is Visibility.GRAPH and f.graph_id in readable_graph_ids)
        ]
        ranked = sorted(matches, key=lambda f: self._score(query, f.content), reverse=True)[:limit]
        return [
            MemoryCandidate(fact_id=f.fact_id, content=f.content, owner_user_id=f.owner,
                            visibility=f.visibility, graph_id=f.graph_id,
                            score=self._score(query, f.content))
            for f in ranked
        ]


class LeakyMemoryStore(InMemoryMemoryStore):
    """A broken store that ignores the visibility filter entirely — the case the
    hydrator's `readable()` re-check exists for."""

    async def search(self, *, query, owner_user_id, readable_graph_ids, limit):
        return [
            MemoryCandidate(fact_id=f.fact_id, content=f.content, owner_user_id=f.owner,
                            visibility=f.visibility, graph_id=f.graph_id, score=1.0)
            for f in self.facts
        ]


# ── the harness ────────────────────────────────────────────────────────────


@dataclass
class Actor:
    subject: str
    user_id: uuid.UUID
    device_id: uuid.UUID
    credential: str
    token: str

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def _deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class Harness:
    client: httpx.AsyncClient
    oidc: LocalOIDCProvider
    core: SecurityCore
    storage: SQLAlchemyStorageBackend
    config: AppConfig
    model: ScriptedModel
    models: dict[str, Any]
    reads: RecordingAdapter
    writes: RecordingAdapter
    ui: RecordingAdapter
    memory: InMemoryMemoryStore | None
    app: Any

    @property
    def runtime(self):
        return self.app.state.agent_tasks.runtime

    # identity
    async def user(self, subject: str) -> Actor:
        self.oidc.subject = subject
        start = await self.client.get(f"{API_V1_PREFIX}/auth/oidc/start")
        from urllib.parse import parse_qs, urlparse

        state = parse_qs(urlparse(start.json()["redirect_url"]).query)["state"][0]
        callback = await self.client.get(
            f"{API_V1_PREFIX}/auth/oidc/callback", params={"code": "c", "state": state}
        )
        bootstrap = callback.json()["bootstrap_token"]
        reg = await self.client.post(
            f"{API_V1_PREFIX}/devices", json={"platform": "android"},
            headers={"Authorization": f"Bearer {bootstrap}"},
        )
        assert reg.status_code == 201, reg.text
        device_id = uuid.UUID(reg.json()["device_id"])
        credential = reg.json()["device_credential"]
        token = await self.fresh_token(device_id, credential)
        async with self.storage.session() as s:
            user_id = (await s.get(Device, device_id)).user_id
        return Actor(subject, user_id, device_id, credential, token)

    async def fresh_token(self, device_id: uuid.UUID, credential: str) -> str:
        resp = await self.client.post(
            f"{API_V1_PREFIX}/sessions/token",
            json={"device_credential": build_device_proof(device_id=device_id, device_credential=credential)},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["access_token"]

    async def shared_graph(self, owner: Actor, *members: Actor) -> uuid.UUID:
        resp = await self.client.post(f"{API_V1_PREFIX}/graphs", json={"name": "g", "type": "shared"},
                                      headers=owner.auth)
        assert resp.status_code == 201, resp.text
        graph_id = uuid.UUID(resp.json()["graph_id"])
        for member in members:
            r = await self.client.post(f"{API_V1_PREFIX}/graphs/{graph_id}/access-requests", json={},
                                       headers=member.auth)
            assert r.status_code in (200, 201), r.text
            r = await self.client.post(f"{API_V1_PREFIX}/graphs/{graph_id}/members",
                                       json={"user_id": str(member.user_id), "role": "member"},
                                       headers=owner.auth)
            assert r.status_code in (200, 201), r.text
        for actor in (owner, *members):
            await self.enter_graph(actor, graph_id)
        return graph_id

    async def enter_graph(self, actor: Actor, graph_id: uuid.UUID) -> None:
        r = await self.client.post(f"{API_V1_PREFIX}/sessions/active-graph",
                                   json={"graph_id": str(graph_id)}, headers=actor.auth)
        assert r.status_code == 200, r.text

    async def grant(self, actor: Actor, capability: str, *, resource_scope: dict | None = None) -> str:
        r = await self.client.post(
            f"{API_V1_PREFIX}/capabilities",
            json={"capability": capability, "scope_type": "user", "scope_id": str(actor.user_id),
                  **({"resource_scope": resource_scope} if resource_scope else {})},
            headers=actor.auth,
        )
        assert r.status_code == 201, r.text
        return r.json()["grant_id"]

    async def revoke(self, actor: Actor, grant_id: str) -> None:
        r = await self.client.delete(f"{API_V1_PREFIX}/capabilities/{grant_id}", headers=actor.auth)
        assert r.status_code == 204, r.text

    async def file(self, owner: Actor, graph_id: uuid.UUID | None, visibility: Visibility, name: str) -> str:
        async with self.storage.session() as s:
            row = FileResource(
                owner_user_id=owner.user_id, source_user_id=owner.user_id, graph_id=graph_id,
                visibility=visibility, sandbox_root=f"/sandbox/{owner.user_id}", relative_path=name,
                size_bytes=1, created_at=datetime.now(timezone.utc),
            )
            s.add(row)
            await s.commit()
            return str(row.file_id)

    # tasks
    async def submit(self, actor: Actor, text: str = "please help", *, key: str | None = None,
                     extra_body: dict | None = None) -> httpx.Response:
        return await self.client.post(
            f"{API_V1_PREFIX}/agent/tasks", json={"input": text, **(extra_body or {})},
            headers={**actor.auth, "Idempotency-Key": key or uuid.uuid4().hex},
        )

    async def confirm(self, actor: Actor, task_id: str, token: str, approve: bool = True) -> httpx.Response:
        return await self.client.post(
            f"{API_V1_PREFIX}/agent/tasks/{task_id}/confirm",
            json={"confirmation_token": token, "approve": approve}, headers=actor.auth,
        )

    async def get(self, actor: Actor, task_id: str) -> httpx.Response:
        return await self.client.get(f"{API_V1_PREFIX}/agent/tasks/{task_id}", headers=actor.auth)

    async def rows(self, model, *where) -> list:
        async with self.storage.session() as s:
            return list((await s.execute(select(model).where(*where))).scalars().all())


def base_config_payload() -> dict:
    return {
        "server": {"base_url": "http://test.invalid"},
        "agent": {"provider": "ollama", "model": "scripted-primary"},
        "security": {"oidc": {"client_id": TEST_CLIENT_ID, "issuer": TEST_ISSUER}},
        "secrets": {"store": "encrypted_local", "kek_source": f"env:{TEST_KEK_ENV_VAR}"},
        "database_url": f"sqlite+aiosqlite:///./unused_{uuid.uuid4().hex}.db",
    }


@pytest_asyncio.fixture
async def make_harness(tmp_path, monkeypatch) -> AsyncIterator[Callable]:
    monkeypatch.setenv(TEST_KEK_ENV_VAR, make_test_kek())
    opened: list = []

    async def _make(
        *,
        config: dict | None = None,
        memory: InMemoryMemoryStore | None | bool = True,
        models: dict[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        extra_tools: list[ToolDefinition] | None = None,
        use_real_execution_tools: bool = False,
    ) -> Harness:
        storage = SQLAlchemyStorageBackend(f"sqlite+aiosqlite:///{tmp_path / uuid.uuid4().hex}.db")
        await storage.init_models()
        payload = _deep_merge(base_config_payload(), config or {})
        app_config = AppConfig.model_validate(payload)
        oidc = LocalOIDCProvider()
        core = build_security_core(app_config, oidc_provider=oidc)
        async with storage.session() as s:
            await core.secret_store.bootstrap(s, resolve_kek(f"env:{TEST_KEK_ENV_VAR}"))
            await s.commit()

        model = ScriptedModel()
        named: dict[str, Any] = {"scripted-primary": model, **(models or {})}

        def factory(spec: ModelSpec, key_provider=None):
            scripted = named.get(spec.model)
            if scripted is not None:
                scripted.spec = spec
                return scripted
            return build_provider(spec, key_provider=key_provider, transport=transport)

        reads, writes, ui = RecordingAdapter("reads"), RecordingAdapter("writes"), RecordingAdapter("ui")
        if extra_tools is not None:
            tools = extra_tools
        elif use_real_execution_tools:
            # The execution branch's own integration test seam: the real
            # server.fs/server.net/server.execution tools, built from this
            # harness's own app_config (so e.g. execution.filesystem.base_root
            # is whatever the test configured, never the production default),
            # run through the full authorization chain exactly like every
            # other tool — proving the wiring end to end rather than through
            # a fake that merely records what it was asked to do.
            from server.composition.execution_tools import build_execution_tools

            tools = build_execution_tools(app_config)
        else:
            tools = [*file_tools(reads, writes), android_ui_tool(ui)]
        memory_store = InMemoryMemoryStore() if memory is True else (memory or None)

        app = build_application(
            app_config, storage=storage, security=core, provider_factory=factory,
            extra_tools=tools, memory_store=memory_store,
        )
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        opened.append((client, storage))
        return Harness(client=client, oidc=oidc, core=core, storage=storage, config=app_config,
                       model=model, models=named, reads=reads, writes=writes, ui=ui,
                       memory=memory_store, app=app)

    yield _make
    for client, storage in opened:
        await client.aclose()
        await storage.dispose()


@pytest_asyncio.fixture
async def h(make_harness) -> Harness:
    return await make_harness()


def pending_of(resp: httpx.Response) -> dict:
    assert resp.status_code == 403, resp.text
    err = resp.json()["error"]
    assert err["code"] == "confirmation_required", err
    return err["details"]


def failure_of(resp: httpx.Response) -> str:
    return resp.json()["error"]["details"]["failure_code"]


__all__ = [
    "Actor", "Fact", "Harness", "InMemoryMemoryStore", "LeakyMemoryStore", "RecordingAdapter",
    "ScriptedModel", "ask", "call", "contract", "failure_of", "final", "pending_of", "say",
    "ModelUnavailable",
]
