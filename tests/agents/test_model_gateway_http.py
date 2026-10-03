"""docs/29 §12 — the HTTP Model Gateway (Phase 6, slice 6A).

The network face of the Model Gateway an external runtime (OD-AF-6: Browser
Use, P2) calls instead of holding a provider key: an OpenAI-compatible
`POST /v1/chat/completions` with `Authorization: Bearer <run token(model)>`.

It is a **separate internal listener** — never mounted on the public API —
off by default, bound only to an internal address (a Unix socket or
loopback; OD-AF-12 decides the container side). Every request passes the
same in-process core the native runtime uses (`AgentGateway.authenticate`,
`model_refusal`, the owner's usage policy, the agent's month), in the order
of §12.2:

    authenticate token → resolve run → alias = `agent-model` → the spec's
    selected profile, permitted to the owner now → bounds (model calls,
    deadline) → usage precheck (the run's budget, the owner's budget and
    rates, the agent's month) → the configured provider (its key resolved
    inside `server.models`) → a bounded, scrubbed response → UsageEvent +
    `agent_run_usage`

The runtime calling it is untrusted: nothing in a request names a provider,
an endpoint, a key, a principal or a budget, and nothing in a response
reveals which provider or model answered.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from server.agents.gateway.model_gateway import (
    AGENT_MODEL_ALIAS,
    bearer_token,
    chat_request_refusal,
    http_status,
)
from server.config.schema import AgentModelGatewayConfig, AppConfig
from server.models.provider import ModelUnavailable
from server.storage.models import (
    AgentConfiguration,
    AgentRunRow,
    AgentRunTokenRow,
    AgentRunUsageRow,
    AgentTask,
    AuditEvent,
    Session,
    UsageEvent,
)
from shared.schemas.agent_factory import AgentGatewayErrorCode, ChatCompletionRequest
from shared.schemas.enums import AgentConfigScopeType, UsageKind
from tests.agents.harness import AGENTS_ON, create_agent
from tests.agents.test_model_routing_runs import Wire
from tests.runtime.conftest import base_config_payload

COMPLETIONS = "/v1/chat/completions"
SOCKET = "unix:/run/jarvis/model-gateway.sock"
PRIMARY_KEY_ENV = "TEST_ONLY_HTTP_GATEWAY_KEY"


def _gateway_on(**gateway) -> dict:
    return {**AGENTS_ON, "agents": {**AGENTS_ON["agents"],
                                    "model_gateway": {"enabled": True, "listen": SOCKET, **gateway}}}


def _paid_config(**agents) -> dict:
    """The run's own model is a keyed, paid, OpenAI-compatible endpoint."""

    config = _gateway_on()
    config["agents"].update({"default_budget_per_run": 0.05, "default_budget_per_month": 1.0, **agents})
    config["agent"] = {"provider": "openai_compatible", "model": "primary-model",
                       "endpoint": "https://llm.test/v1", "secret_ref": f"env:{PRIMARY_KEY_ENV}",
                       "pricing": {"input_per_1k_tokens": 0.00001, "output_per_1k_tokens": 0.00002},
                       "bounds": {"per_task_budget": 5.0}}
    config["security"] = {"budgets": {"per_user_daily_cost_limit": 50.0, "global_daily_cost_limit": 500.0}}
    return config


def _factory(h):
    return h.app.state.agent_factory.factory


def _internal(h) -> httpx.AsyncClient:
    app = h.app.state.model_gateway_app
    assert app is not None
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://model-gateway")


def _body(**overrides) -> dict:
    return {"model": AGENT_MODEL_ALIAS, "messages": [{"role": "system", "content": "You browse."},
                                                     {"role": "user", "content": "What changed on the page?"}],
            **overrides}


async def _call(h, token: str | None, body=None, *, headers: dict | None = None) -> httpx.Response:
    hdrs = {"Authorization": f"Bearer {token}"} if token is not None else {}
    async with _internal(h) as client:
        content = body if isinstance(body, (bytes, str)) else json.dumps(_body() if body is None else body)
        return await client.post(COMPLETIONS, content=content,
                                 headers={"Content-Type": "application/json", **hdrs, **(headers or {})})


class ExternalRun:
    """A run started the way an external provider's `start_run` will (6D):
    the run record, its ordinary task as the present owner, and the two run
    tokens — handed to the runtime, never to anything else."""

    def __init__(self, run_id, task_id, model_token, tool_token):
        self.run_id, self.task_id = run_id, task_id
        self.model_token, self.tool_token = model_token, tool_token


async def _external_run(h, actor, agent_id: str) -> ExternalRun:
    factory = _factory(h)
    service = factory.service
    async with h.storage.session() as s:
        loaded = await service.load(s, uuid.UUID(agent_id), fresh=True)
        assert loaded is not None and loaded[1] is not None
        spec = loaded[1]
        session_id = (await s.execute(select(Session.session_id).where(Session.device_id == actor.device_id)
                                      .limit(1))).scalars().first()
        run_id, task_id = uuid.uuid4(), uuid.uuid4()
        run = await service.create_run(s, spec=spec, run_id=run_id)
        now = datetime.now(timezone.utc)
        s.add(AgentTask(task_id=task_id, user_id=actor.user_id, device_id=actor.device_id, session_id=session_id,
                        graph_id=spec.graph_id, status="running", mode=spec.run_mode.value,
                        created_at=now, updated_at=now))
        await s.flush()
        await service.run_started(s, run_id, task_id=task_id)
        issued = await factory.gateway.issue(s, run, deadline=factory.run_deadline(spec))
        await s.commit()
    return ExternalRun(run_id, task_id, issued.model, issued.tool)


async def _setup(make_harness, config=None, **harness):
    h = await make_harness(config=config or _gateway_on(), agent_tools=True, **harness)
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    run = await _external_run(h, alice, agent["agent_id"])
    return h, alice, agent, run


def _error(resp: httpx.Response) -> str:
    return resp.json()["error"]["code"]


# ── the listener: separate, internal, off by default ────────────────────────


def _config(agents: dict) -> AppConfig:
    payload = base_config_payload()
    payload["agents"] = agents
    return AppConfig.model_validate(payload)


def test_the_model_gateway_is_off_by_default() -> None:
    assert AgentModelGatewayConfig().enabled is False
    assert AgentModelGatewayConfig().listen is None
    assert _config({"enabled": True}).agents.model_gateway.enabled is False


@pytest.mark.parametrize("listen", ["unix:/run/jarvis/mg.sock", "127.0.0.1:8601", "127.0.0.9:9000", "[::1]:8601"])
def test_internal_bindings_are_accepted(listen: str) -> None:
    config = _config({"enabled": True, "model_gateway": {"enabled": True, "listen": listen}})
    assert config.agents.model_gateway.listen == listen


@pytest.mark.parametrize("listen", [
    "0.0.0.0:8601", "[::]:8601", "10.0.0.5:8601", "192.168.1.10:8601", "172.17.0.1:8601", "8.8.8.8:443",
    "localhost:8601", "gateway.internal:8601", "unix:relative.sock", "unix:", "127.0.0.1:0", "127.0.0.1:70000",
    "127.0.0.1", "http://127.0.0.1:8601", "unix:/run/../etc/mg.sock",
])
def test_anything_but_an_internal_binding_is_refused(listen: str) -> None:
    with pytest.raises(ValidationError):
        _config({"enabled": True, "model_gateway": {"enabled": True, "listen": listen}})


def test_enabling_it_needs_the_agent_factory_and_a_binding() -> None:
    with pytest.raises(ValidationError, match="agents.enabled"):
        _config({"enabled": False, "model_gateway": {"enabled": True, "listen": SOCKET}})
    with pytest.raises(ValidationError, match="listen"):
        _config({"enabled": True, "model_gateway": {"enabled": True}})


async def test_without_the_flag_there_is_no_model_gateway_at_all(make_harness) -> None:
    h = await make_harness(config=AGENTS_ON, agent_tools=True)
    assert h.app.state.model_gateway_app is None
    assert h.app.state.model_gateway_listener is None


async def test_it_is_never_mounted_on_the_public_api(make_harness) -> None:
    h = await make_harness(config=_gateway_on(), agent_tools=True)
    assert h.app.state.model_gateway_app is not None
    assert all("chat/completions" not in path for path in h.app.openapi()["paths"])
    resp = await h.client.post(COMPLETIONS, json=_body(), headers={"Authorization": "Bearer x"})
    assert resp.status_code == 404
    # ... and the internal app has nothing else: no docs, no schema, no other route.
    async with _internal(h) as client:
        for path in ("/docs", "/openapi.json", "/redoc", "/api/v1/health", "/v1/models"):
            assert (await client.get(path)).status_code == 404


async def test_the_listener_is_a_background_service_on_its_internal_binding(make_harness) -> None:
    h = await make_harness(config=_gateway_on(), agent_tools=True)
    listener = h.app.state.model_gateway_listener
    assert listener is not None
    assert listener.binding == SOCKET
    assert listener.app is h.app.state.model_gateway_app


# ── pure rules ──────────────────────────────────────────────────────────────


def test_bearer_token_parsing() -> None:
    assert bearer_token("Bearer abc") == "abc"
    assert bearer_token("bearer abc") == "abc"
    for bad in (None, "", "Bearer", "Bearer ", "Basic abc", "abc", "Bearer a b"):
        assert bearer_token(bad) is None


def test_every_refusal_has_its_documented_status() -> None:
    expected = {
        AgentGatewayErrorCode.INVALID_RUN_TOKEN: (401, "invalid_run_token"),
        AgentGatewayErrorCode.SCHEMA_INVALID: (400, "schema_invalid"),
        AgentGatewayErrorCode.MODEL_NOT_ALLOWED: (403, "model_not_allowed"),
        AgentGatewayErrorCode.RUN_NOT_RUNNING: (409, "run_not_running"),
        AgentGatewayErrorCode.AGENT_UNAVAILABLE: (409, "run_not_running"),
        AgentGatewayErrorCode.SPEC_CHANGED: (409, "run_not_running"),
        AgentGatewayErrorCode.BUDGET_EXCEEDED: (429, "budget_exceeded"),
        AgentGatewayErrorCode.AGENT_BUDGET_EXHAUSTED: (429, "agent_budget_exhausted"),
        AgentGatewayErrorCode.RATE_LIMITED: (429, "rate_limited"),
        AgentGatewayErrorCode.MAX_MODEL_CALLS: (429, "max_model_calls"),
        AgentGatewayErrorCode.DEPENDENCY_UNAVAILABLE: (503, "dependency_unavailable"),
    }
    for code, status in expected.items():
        assert http_status(code) == status, code
    # Codes the Model Gateway never answers with fail closed as a refusal of the token.
    for code in (AgentGatewayErrorCode.REPLAY, AgentGatewayErrorCode.STALE_REQUEST):
        assert http_status(code)[0] in (400, 401)


def test_the_request_shape_is_closed() -> None:
    ok = ChatCompletionRequest.model_validate(_body(temperature=0.2, max_tokens=256))
    assert chat_request_refusal(ok) is None
    assert chat_request_refusal(ChatCompletionRequest.model_validate(_body(stream=True))) == "stream_unsupported"
    assert chat_request_refusal(ChatCompletionRequest.model_validate(_body(n=2))) == "n_unsupported"
    for extra in ({"tools": []}, {"functions": []}, {"response_format": {"type": "json_object"}},
                  {"logprobs": True}, {"api_key": "x"}, {"base_url": "https://evil.test"}):
        with pytest.raises(ValidationError):
            ChatCompletionRequest.model_validate(_body(**extra))
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(_body(messages=[]))
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(_body(messages=[{"role": "tool", "content": "x"}]))
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(_body(messages=[
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:"}}]}]))
    parts = ChatCompletionRequest.model_validate(_body(messages=[
        {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}]))
    assert chat_request_refusal(parts) is None


# ── 200: the run's own model, metered and attributed ────────────────────────


async def test_a_live_runs_model_call_is_answered_metered_and_attributed(make_harness) -> None:
    h, alice, agent, run = await _setup(make_harness)
    h.model.push("The price dropped to 12.")

    resp = await _call(h, run.model_token)

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["object"] == "chat.completion"
    assert payload["model"] == AGENT_MODEL_ALIAS
    [choice] = payload["choices"]
    assert choice["message"] == {"role": "assistant", "content": "The price dropped to 12."}
    assert choice["finish_reason"] == "stop"
    assert payload["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    # The provider saw exactly the runtime's messages, nothing more.
    [seen] = h.model.seen
    assert [(m.role, m.content) for m in seen] == [("system", "You browse."), ("user", "What changed on the page?")]
    # Metered as the run's owner and attributed to the run.
    [usage] = await h.rows(UsageEvent, UsageEvent.kind == UsageKind.MODEL_CALL)
    assert usage.user_id == alice.user_id and usage.tokens_or_units == 15
    [link] = await h.rows(AgentRunUsageRow, AgentRunUsageRow.run_id == run.run_id)
    assert link.usage_id == usage.usage_id
    [task] = await h.rows(AgentTask, AgentTask.task_id == run.task_id)
    assert task.model_calls == 1
    # A live run's total is written when it ends; usage is read live meanwhile.
    [row] = await h.rows(AgentRunRow, AgentRunRow.run_id == run.run_id)
    assert row.cost_total == 0.0


async def test_nothing_in_the_answer_names_the_provider_or_the_model(make_harness, monkeypatch) -> None:
    key = f"TEST-ONLY-key-{uuid.uuid4().hex}"
    monkeypatch.setenv(PRIMARY_KEY_ENV, key)
    wire = Wire("ok")
    h, _alice, _agent, run = await _setup(make_harness, _paid_config(), transport=httpx.MockTransport(wire.handler))

    resp = await _call(h, run.model_token)

    assert resp.status_code == 200, resp.text
    [request] = wire.requests
    assert request.headers["authorization"] == f"Bearer {key}"   # the key reached the provider ...
    raw = resp.text + json.dumps(dict(resp.headers))
    for leaked in (key, "primary-model", "openai_compatible", "llm.test"):  # ... and nothing else
        assert leaked not in raw
    # The run token itself is never echoed either.
    assert run.model_token not in raw


async def test_an_oversized_answer_is_truncated_with_a_marker(make_harness) -> None:
    h, _alice, _agent, run = await _setup(make_harness, _gateway_on(max_completion_chars=1000))
    h.model.push("x" * 5000)

    resp = await _call(h, run.model_token)

    content = resp.json()["choices"][0]["message"]["content"]
    assert len(content) <= 1000 + 64 and content.startswith("x" * 100) and "truncated" in content
    assert resp.json()["choices"][0]["finish_reason"] == "length"


async def test_secret_shaped_text_in_an_answer_is_masked(make_harness) -> None:
    h, _alice, _agent, run = await _setup(make_harness)
    leaked = "sk-" + "A1b2C3d4" * 6
    h.model.push(f"here: {leaked}")

    resp = await _call(h, run.model_token)

    assert resp.status_code == 200 and leaked not in resp.text


# ── 401: the token ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer", "Bearer not-a-token"])
async def test_no_well_formed_token_is_401(make_harness, header) -> None:
    h, _alice, _agent, _run = await _setup(make_harness)
    async with _internal(h) as client:
        resp = await client.post(COMPLETIONS, json=_body(), headers={"Authorization": header} if header else {})
    assert resp.status_code == 401 and _error(resp) == "invalid_run_token"
    assert h.model.seen == []
    assert await h.rows(UsageEvent) == []


async def test_an_unknown_token_is_401_and_audited(make_harness) -> None:
    h, _alice, _agent, _run = await _setup(make_harness)
    resp = await _call(h, "A" * 43)
    assert resp.status_code == 401 and _error(resp) == "invalid_run_token"
    denied = [e for e in await h.rows(AuditEvent) if e.action == "agent.gateway.denied"]
    assert denied and denied[-1].resource.startswith("gateway:model:invalid_run_token")
    assert h.model.seen == []


async def test_the_runs_tool_token_is_not_a_model_token(make_harness) -> None:
    h, _alice, _agent, run = await _setup(make_harness)
    resp = await _call(h, run.tool_token)
    assert resp.status_code == 401 and _error(resp) == "invalid_run_token"
    assert h.model.seen == []


async def test_a_revoked_token_is_refused_on_the_very_next_request(make_harness) -> None:
    h, _alice, _agent, run = await _setup(make_harness)
    h.model.push("first", "second")
    assert (await _call(h, run.model_token)).status_code == 200

    async with h.storage.session() as s:
        assert await _factory(h).gateway.revoke_run(s, run.run_id, "stopped") == 2
        await s.commit()

    resp = await _call(h, run.model_token)
    assert resp.status_code == 401 and _error(resp) == "invalid_run_token"
    assert len(h.model.seen) == 1


async def test_an_expired_token_is_401(make_harness) -> None:
    h, _alice, _agent, run = await _setup(make_harness)
    async with h.storage.session() as s:
        await s.execute(update(AgentRunTokenRow).where(AgentRunTokenRow.run_id == run.run_id)
                        .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await s.commit()
    resp = await _call(h, run.model_token)
    assert resp.status_code == 401 and _error(resp) == "invalid_run_token"
    assert h.model.seen == []


# ── 400: the request ────────────────────────────────────────────────────────


async def test_streaming_is_refused_with_400_before_any_call(make_harness) -> None:
    h, _alice, _agent, run = await _setup(make_harness)
    resp = await _call(h, run.model_token, _body(stream=True))
    assert resp.status_code == 400 and _error(resp) == "schema_invalid"
    assert "stream" in resp.json()["error"]["message"]
    assert h.model.seen == [] and await h.rows(UsageEvent) == []


@pytest.mark.parametrize("body", [
    b"not json", b"[]", json.dumps(_body(tools=[{"type": "function"}])),
    json.dumps(_body(response_format={"type": "json_object"})), json.dumps({"model": AGENT_MODEL_ALIAS}),
    json.dumps(_body(n=3)),
])
async def test_a_malformed_or_unsupported_request_is_400(make_harness, body) -> None:
    h, _alice, _agent, run = await _setup(make_harness)
    resp = await _call(h, run.model_token, body)
    assert resp.status_code == 400 and _error(resp) == "schema_invalid"
    assert h.model.seen == []


async def test_an_oversized_body_is_refused_before_it_is_parsed(make_harness) -> None:
    h, _alice, _agent, run = await _setup(make_harness, _gateway_on(max_request_bytes=4096))
    big = _body(messages=[{"role": "user", "content": "y" * 10_000}])
    resp = await _call(h, run.model_token, big)
    assert resp.status_code == 413 and _error(resp) == "schema_invalid"
    assert h.model.seen == []


async def test_a_bad_request_without_a_valid_token_is_still_401(make_harness) -> None:
    """Authentication comes first: an unauthenticated caller learns nothing
    about what the gateway would accept."""

    h, _alice, _agent, _run = await _setup(make_harness)
    resp = await _call(h, "A" * 43, _body(stream=True))
    assert resp.status_code == 401


# ── 403: the model ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("model", ["primary-model", "scripted-primary", "gpt-4o", "model-tool:writer", "", "agent"])
async def test_any_model_but_the_runs_alias_is_403(make_harness, model) -> None:
    h, _alice, _agent, run = await _setup(make_harness)
    resp = await _call(h, run.model_token, _body(model=model))
    assert resp.status_code == 403 and _error(resp) == "model_not_allowed"
    assert h.model.seen == []


async def test_a_profile_no_longer_permitted_to_the_owner_is_403(make_harness) -> None:
    """The profile is not open to all, and the owner has since configured a
    model of their own: the run's approved profile is no longer theirs (OD-RT-3,
    read live on every call)."""

    config = _gateway_on()
    config["agents"]["model_profiles_open_to_all"] = []
    h, alice, _agent, run = await _setup(make_harness, config)
    async with h.storage.session() as s:
        s.add(AgentConfiguration(
            scope_type=AgentConfigScopeType.USER, scope_id=alice.user_id,
            primary_model={"provider": "ollama", "model": "her-own-model", "endpoint": "http://127.0.0.1:11434"},
            updated_at=datetime.now(timezone.utc)))
        await s.commit()
    resp = await _call(h, run.model_token)
    assert resp.status_code == 403 and _error(resp) == "model_not_allowed"
    assert h.model.seen == []


# ── 409: the run ────────────────────────────────────────────────────────────


async def test_a_finished_run_is_409(make_harness) -> None:
    h, _alice, _agent, run = await _setup(make_harness)
    async with h.storage.session() as s:
        await s.execute(update(AgentRunRow).where(AgentRunRow.run_id == run.run_id)
                        .values(status="completed", finished_at=datetime.now(timezone.utc)))
        await s.commit()
    resp = await _call(h, run.model_token)
    assert resp.status_code == 409 and _error(resp) == "run_not_running"
    assert h.model.seen == []


async def test_a_waiting_run_is_409(make_harness) -> None:
    h, _alice, _agent, run = await _setup(make_harness)
    async with h.storage.session() as s:
        await s.execute(update(AgentRunRow).where(AgentRunRow.run_id == run.run_id).values(status="waiting"))
        await s.commit()
    resp = await _call(h, run.model_token)
    assert resp.status_code == 409 and _error(resp) == "run_not_running"


async def test_a_run_whose_task_ended_is_409(make_harness) -> None:
    h, _alice, _agent, run = await _setup(make_harness)
    async with h.storage.session() as s:
        await s.execute(update(AgentTask).where(AgentTask.task_id == run.task_id).values(status="cancelled"))
        await s.commit()
    resp = await _call(h, run.model_token)
    assert resp.status_code == 409 and _error(resp) == "run_not_running"
    assert h.model.seen == []


async def test_a_paused_agent_is_409(make_harness) -> None:
    h, alice, agent, run = await _setup(make_harness)
    paused = await h.client.post(f"/api/v1/agents/{agent['agent_id']}/pause", json={}, headers=alice.auth)
    assert paused.status_code == 200, paused.text
    resp = await _call(h, run.model_token)
    assert resp.status_code in (401, 409)
    assert h.model.seen == []


async def test_a_run_past_its_deadline_is_409(make_harness) -> None:
    h, _alice, _agent, run = await _setup(make_harness)
    async with h.storage.session() as s:
        await s.execute(update(AgentRunRow).where(AgentRunRow.run_id == run.run_id)
                        .values(started_at=datetime.now(timezone.utc) - timedelta(days=1)))
        await s.commit()
    resp = await _call(h, run.model_token)
    assert resp.status_code == 409 and _error(resp) == "run_not_running"
    assert h.model.seen == []


async def test_the_global_stop_refuses_every_model_call(make_harness) -> None:
    h, _alice, _agent, run = await _setup(make_harness)
    h.app.state.model_gateway.latch.latched = True
    try:
        resp = await _call(h, run.model_token)
    finally:
        h.app.state.model_gateway.latch.latched = False
    assert resp.status_code == 409 and _error(resp) == "run_not_running"
    assert h.model.seen == []


# ── 429: bounds and budgets ─────────────────────────────────────────────────


async def test_the_runs_model_call_bound_is_429(make_harness) -> None:
    h, _alice, _agent, run = await _setup(make_harness)
    async with h.storage.session() as s:
        await s.execute(update(AgentTask).where(AgentTask.task_id == run.task_id).values(model_calls=10_000))
        await s.commit()
    resp = await _call(h, run.model_token)
    assert resp.status_code == 429 and _error(resp) == "max_model_calls"
    assert h.model.seen == []


async def test_concurrent_calls_cannot_overrun_the_model_call_bound(make_harness) -> None:
    h, _alice, _agent, run = await _setup(make_harness)
    async with h.storage.session() as s:
        [row] = (await s.execute(select(AgentRunRow).where(AgentRunRow.run_id == run.run_id))).scalars().all()
        spec = (await _factory(h).service.load(s, row.agent_id, fresh=True))[1]
    limit = min(h.config.agent.bounds.max_model_calls, spec.bounds.max_model_calls)
    async with h.storage.session() as s:
        await s.execute(update(AgentTask).where(AgentTask.task_id == run.task_id).values(model_calls=limit - 1))
        await s.commit()
    h.model.push("a", "b", "c")
    statuses = sorted(r.status_code for r in await asyncio.gather(*(_call(h, run.model_token) for _ in range(3))))
    assert statuses == [200, 429, 429]
    assert len(h.model.seen) == 1


async def test_the_runs_budget_is_429_budget_exceeded(make_harness, monkeypatch) -> None:
    monkeypatch.setenv(PRIMARY_KEY_ENV, "TEST-ONLY-k")
    wire = Wire("never")
    config = _paid_config()
    config["agent"]["bounds"] = {"per_task_budget": 0.000001}   # the operator's per-run ceiling
    h, _alice, _agent, run = await _setup(make_harness, config, transport=httpx.MockTransport(wire.handler))
    resp = await _call(h, run.model_token)
    assert resp.status_code == 429 and _error(resp) == "budget_exceeded"
    assert wire.requests == [] and await h.rows(UsageEvent) == []


async def test_the_owners_budget_is_429_budget_exceeded(make_harness, monkeypatch) -> None:
    monkeypatch.setenv(PRIMARY_KEY_ENV, "TEST-ONLY-k")
    wire = Wire("never")
    config = _paid_config()
    config["security"] = {"budgets": {"per_user_daily_cost_limit": 0.0000001, "global_daily_cost_limit": 500.0}}
    h, _alice, _agent, run = await _setup(make_harness, config, transport=httpx.MockTransport(wire.handler))
    resp = await _call(h, run.model_token)
    assert resp.status_code == 429 and _error(resp) == "budget_exceeded"
    assert wire.requests == []


async def test_the_agents_month_is_429_agent_budget_exhausted(make_harness, monkeypatch) -> None:
    monkeypatch.setenv(PRIMARY_KEY_ENV, "TEST-ONLY-k")
    wire = Wire("never")
    h, _alice, agent, run = await _setup(make_harness, _paid_config(), transport=httpx.MockTransport(wire.handler))
    async with h.storage.session() as s:
        # An earlier finished run spent the whole month.
        loaded = await _factory(h).service.load(s, uuid.UUID(agent["agent_id"]), fresh=True)
        earlier = await _factory(h).service.create_run(s, spec=loaded[1], run_id=uuid.uuid4())
        earlier.status, earlier.finished_at, earlier.cost_total = "completed", datetime.now(timezone.utc), 1.0
        await s.commit()
    resp = await _call(h, run.model_token)
    assert resp.status_code == 429 and _error(resp) == "agent_budget_exhausted"
    assert wire.requests == []


async def test_the_owners_rate_limit_is_429_rate_limited(make_harness) -> None:
    config = _gateway_on()
    config["security"] = {"rate_limits": {"per_user_requests_per_minute": 1}}
    h, _alice, _agent, run = await _setup(make_harness, config)
    h.model.push("one", "two")
    assert (await _call(h, run.model_token)).status_code == 200
    resp = await _call(h, run.model_token)
    assert resp.status_code == 429 and _error(resp) == "rate_limited"
    assert len(h.model.seen) == 1


# ── 503: the provider ───────────────────────────────────────────────────────


async def test_an_unavailable_provider_is_503_metered_as_zero(make_harness) -> None:
    h, _alice, _agent, run = await _setup(make_harness)
    secret = f"TEST-ONLY-{uuid.uuid4().hex}"
    h.model.push(ModelUnavailable(f"upstream said {secret} at http://10.0.0.7:11434"))

    resp = await _call(h, run.model_token)

    assert resp.status_code == 503 and _error(resp) == "dependency_unavailable"
    assert secret not in resp.text and "10.0.0.7" not in resp.text
    [usage] = await h.rows(UsageEvent, UsageEvent.kind == UsageKind.MODEL_CALL)
    assert usage.tokens_or_units == 0 and usage.estimated_cost == 0.0
    assert [r.usage_id for r in await h.rows(AgentRunUsageRow, AgentRunUsageRow.run_id == run.run_id)] == [
        usage.usage_id]


async def test_an_unexpected_provider_error_is_503_and_reveals_nothing(make_harness) -> None:
    h, _alice, _agent, run = await _setup(make_harness)
    secret = f"TEST-ONLY-{uuid.uuid4().hex}"
    h.model.push(RuntimeError(f"traceback with {secret}"))
    resp = await _call(h, run.model_token)
    assert resp.status_code == 503 and secret not in resp.text


async def test_a_disconnected_runtime_cancels_the_call_and_it_is_metered_as_zero(make_harness) -> None:
    h, _alice, _agent, run = await _setup(make_harness)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def slow(_messages):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "late"

    h.model.push(slow)
    gateway = h.app.state.model_gateway
    gone = asyncio.Event()

    async def disconnected() -> bool:
        return gone.is_set()

    call = asyncio.ensure_future(gateway.chat_completion(
        authorization=f"Bearer {run.model_token}", body=json.dumps(_body()).encode(), disconnected=disconnected))
    await asyncio.wait_for(started.wait(), 5)
    gone.set()
    reply = await asyncio.wait_for(call, 5)
    assert cancelled.is_set()
    assert reply.status == 499
    [usage] = await h.rows(UsageEvent, UsageEvent.kind == UsageKind.MODEL_CALL)
    assert usage.tokens_or_units == 0 and usage.estimated_cost == 0.0


# ── the runtime holds nothing that identifies a person ──────────────────────


async def test_an_external_runs_context_and_answers_carry_no_identity(make_harness) -> None:
    h, alice, _agent, run = await _setup(make_harness)
    h.model.push("ok")
    resp = await _call(h, run.model_token)
    text = resp.text
    for identity in (str(alice.user_id), str(alice.device_id), alice.token, alice.credential):
        assert identity not in text


# ── the real listener ───────────────────────────────────────────────────────


async def test_the_listener_serves_only_on_its_owner_only_unix_socket(make_harness, tmp_path) -> None:
    import os
    import stat

    sock = tmp_path / "mg.sock"
    h = await make_harness(config=_gateway_on(listen=f"unix:{sock}"), agent_tools=True)
    listener = h.app.state.model_gateway_listener
    await listener.start()
    try:
        assert stat.S_ISSOCK(os.lstat(sock).st_mode)
        assert stat.S_IMODE(os.lstat(sock).st_mode) == 0o600
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(sock)),
                                     base_url="http://model-gateway") as client:
            resp = await client.post(COMPLETIONS, json=_body())
            assert resp.status_code == 401 and _error(resp) == "invalid_run_token"
            assert "server" not in resp.headers
            assert (await client.get("/openapi.json")).status_code == 404
    finally:
        await listener.stop()
    assert not sock.exists()


async def test_the_listener_refuses_to_replace_a_file_that_is_not_its_socket(make_harness, tmp_path) -> None:
    path = tmp_path / "not-a-socket"
    path.write_text("keep me")
    h = await make_harness(config=_gateway_on(listen=f"unix:{path}"), agent_tools=True)
    with pytest.raises(RuntimeError, match="not a socket"):
        await h.app.state.model_gateway_listener.start()
    assert path.read_text() == "keep me"


# ── an unattended run (docs/29 §15) ─────────────────────────────────────────


async def test_an_unattended_runs_model_calls_carry_no_device_or_session_and_keep_its_budget(
        make_harness, monkeypatch) -> None:
    from tests.agents.harness import UNATTENDED_DRAFT, UNATTENDED_ON, grant_delegation
    from server.storage.models import StandingDelegationRow

    monkeypatch.setenv(PRIMARY_KEY_ENV, "TEST-ONLY-k")
    # Each answer costs 1000 prompt + 1000 completion tokens: 0.00003.
    wire = Wire("first", "never", usage=(1000, 1000))
    config = _paid_config()
    config["agents"].update({k: v for k, v in UNATTENDED_ON["agents"].items()
                             if k in ("unattended_enabled", "standing_delegation_ratified")})
    h = await make_harness(config=config, agent_tools=True, transport=httpx.MockTransport(wire.handler))
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=UNATTENDED_DRAFT)
    # The delegation allows far less per run than the spec does.
    await grant_delegation(h, alice, agent["agent_id"],
                           {"max_runs_per_day": 2, "budget_per_run": 0.00003, "budget_per_month": 0.2})
    [delegation] = await h.rows(StandingDelegationRow, StandingDelegationRow.status == "active")

    factory = _factory(h)
    async with h.storage.session() as s:
        spec = (await factory.service.load(s, uuid.UUID(agent["agent_id"]), fresh=True))[1]
        run_id, task_id = uuid.uuid4(), uuid.uuid4()
        run = await factory.service.create_run(s, spec=spec, run_id=run_id, delegation=delegation,
                                               occurrence_at=datetime.now(timezone.utc))
        now = datetime.now(timezone.utc)
        s.add(AgentTask(task_id=task_id, user_id=alice.user_id, delegation_id=delegation.delegation_id,
                        graph_id=spec.graph_id, status="running", mode=spec.run_mode.value,
                        created_at=now, updated_at=now))
        await s.flush()
        await factory.service.run_started(s, run_id, task_id=task_id)
        issued = await factory.gateway.issue(s, run, deadline=factory.run_deadline(spec))
        await s.commit()

    first = await _call(h, issued.model)
    assert first.status_code == 200, first.text
    [usage] = await h.rows(UsageEvent, UsageEvent.kind == UsageKind.MODEL_CALL)
    assert usage.user_id == alice.user_id
    assert usage.device_id is None and usage.session_id is None
    # The first call spent part of the delegation's per-run budget: the next
    # projected call no longer fits it, though it would fit the spec's.
    second = await _call(h, issued.model)
    assert second.status_code == 429 and _error(second) == "budget_exceeded"
    assert len(wire.requests) == 1


async def test_a_model_tool_alias_is_403_even_when_the_spec_may_invoke_that_tool(make_harness, monkeypatch) -> None:
    """`model-tool:<id>` is the in-process alias for a model reached as a
    tool, through the ordinary tool path. An external runtime asks only for
    `agent-model`: a model-tool alias is refused here even when the spec's
    envelope holds `model.invoke` for exactly that tool."""

    from tests.agents.test_model_routing_runs import KEY_ENV, MODEL_DRAFT, _config, _with_model_tool_ability

    monkeypatch.setenv(KEY_ENV, "TEST-ONLY-writer")
    wire = Wire("never")
    config = _config()
    config["agents"]["model_gateway"] = {"enabled": True, "listen": SOCKET}
    h = await make_harness(config=config, agent_tools=True, transport=httpx.MockTransport(wire.handler))
    _with_model_tool_ability(h)
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=MODEL_DRAFT)
    run = await _external_run(h, alice, agent["agent_id"])

    resp = await _call(h, run.model_token, _body(model="model-tool:writer"))

    assert resp.status_code == 403 and _error(resp) == "model_not_allowed"
    assert wire.requests == [] and h.model.seen == []


async def test_a_task_that_is_not_the_runs_owners_is_409(make_harness) -> None:
    h, _alice, _agent, run = await _setup(make_harness)
    bob = await h.user("bob")
    async with h.storage.session() as s:
        await s.execute(update(AgentTask).where(AgentTask.task_id == run.task_id)
                        .values(user_id=bob.user_id, device_id=bob.device_id,
                                session_id=(await s.execute(select(Session.session_id).where(
                                    Session.device_id == bob.device_id).limit(1))).scalars().first()))
        await s.commit()
    resp = await _call(h, run.model_token)
    assert resp.status_code == 409 and _error(resp) == "run_not_running"
    assert h.model.seen == []
