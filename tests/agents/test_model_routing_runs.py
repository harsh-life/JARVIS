"""docs/29 §12 — a model as a pluggable tool inside an agent run (Phase 2 D).

The agent asks for *a kind of model* — `agent.model` with a role, a
preference and a prompt. JARVIS, not the agent, picks one of the operator's
configured model tools the spec permits (`route_model_call`), and the call is
then an ordinary `model.invoke` tool call scoped to that model tool: the
envelope gate, the owner's activation and grant, the engine and metering all
apply exactly as for any tool. What must hold:

* the role is a routing preference, never a permission;
* the request can name no provider, model, profile, endpoint or key;
* a model tool cannot be called directly, around the routing;
* outside the envelope (no `model.invoke`) there is no model routing at all;
* a profile the owner may not use, or one that does not fit the run budget,
  is never chosen;
* a revoked grant stops the routed call — routing never authorizes;
* the provider key reaches the provider's auth header and nothing else: not
  the agent's prompt, the run, the task, the audit, the usage ledger.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import uuid

import httpx
import pytest

from server.storage.models import (
    AgentDefinitionRow,
    AgentRunRow,
    AgentSpecVersionRow,
    AgentTask,
    AuditEvent,
    UsageEvent,
)
from shared.schemas.agent_factory import AbilityName
from shared.schemas.enums import UsageKind
from tests.agents.harness import AGENTS_ON, DRAFT, create_agent, run_agent
from tests.runtime.conftest import ask, call, final

KEY_ENV = "TEST_ONLY_WRITER_KEY"
WRITER_PROFILE = {
    "profile_id": "writer", "version": 1, "model_ref": "models_as_tools.writer",
    "features": ["writing"], "context_window": 4096, "supported_runtimes": ["native"],
    "display_name": "Writing model",
}


def _config(**overrides) -> dict:
    agents = dict(AGENTS_ON["agents"])
    agents["model_profiles"] = [*AGENTS_ON["agents"]["model_profiles"], {**WRITER_PROFILE, **overrides}]
    agents["model_profiles_open_to_all"] = ["general-agentic", "writer"]
    # The operator's agent budget policy (default 0: no paid call at all).
    agents["default_budget_per_run"], agents["default_budget_per_month"] = 0.05, 1.0
    return {
        **AGENTS_ON,
        "agents": agents,
        # A paid model tool must be priced, and paid calls need budgets (13 §3).
        "models_as_tools": [{"id": "writer", "provider": "openai_compatible", "model": "writer-model",
                             "endpoint": "https://llm.test/v1", "secret_ref": f"env:{KEY_ENV}",
                             "description": "a writing model", "enabled": True,
                             "pricing": {"input_per_1k_tokens": 0.00001, "output_per_1k_tokens": 0.00002}}],
        "agent": {"bounds": {"per_task_budget": 5.0}},
        "security": {"budgets": {"per_user_daily_cost_limit": 50.0, "global_daily_cost_limit": 500.0}},
    }


class Wire:
    """A fake OpenAI-compatible endpoint for the model tool."""

    def __init__(self, *answers: str, usage: tuple[int, int] = (10, 5)) -> None:
        self.answers = list(answers)
        self.usage = usage
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        content = self.answers.pop(0) if self.answers else "wire default"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                                         "usage": {"prompt_tokens": self.usage[0],
                                                   "completion_tokens": self.usage[1]}})


def _with_model_tool_ability(h) -> None:
    """An operator template that holds `invoke_model_tool` (none of the four
    v1 templates does): research_digest with the ability added."""

    service = h.app.state.agent_factory._factory.service
    registries = service.registries
    template = registries.templates["research_digest"]
    widened = template.model_copy(update={"abilities": (*template.abilities, AbilityName.INVOKE_MODEL_TOOL)})
    service._registries = dataclasses.replace(
        registries,
        templates={**registries.templates, "research_digest": widened},
        enabled_templates={**registries.enabled_templates, "research_digest": widened},
    )


MODEL_DRAFT = {**DRAFT, "requested_abilities": ["read_web_allowlisted", "invoke_model_tool"]}


@pytest.fixture
def key(monkeypatch) -> str:
    value = f"TEST-ONLY-writer-key-{uuid.uuid4().hex}"
    monkeypatch.setenv(KEY_ENV, value)
    return value


async def _setup(make_harness, wire: Wire, *, grant: bool = True, config: dict | None = None):
    h = await make_harness(config=config or _config(), agent_tools=True,
                           transport=httpx.MockTransport(wire.handler))
    _with_model_tool_ability(h)
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=MODEL_DRAFT)
    grant_id = await h.grant(alice, "model.invoke") if grant else None
    return h, alice, agent, grant_id


async def _run(h, actor, agent_id: str) -> dict:
    resp = await run_agent(h, actor, agent_id)
    assert resp.status_code == 202, resp.text
    return resp.json()


async def _everything_persisted(h) -> str:
    blobs = []
    for model in (AuditEvent, UsageEvent, AgentTask, AgentRunRow, AgentDefinitionRow, AgentSpecVersionRow):
        for row in await h.rows(model):
            blobs.append(json.dumps({c.name: str(getattr(row, c.name)) for c in model.__table__.columns}))
    return "\n".join(blobs)


def _routed(role: str | None = "writing", **extra) -> str:
    args = {"prompt": "Write a two-line summary of the advisories.", **({"role": role} if role else {}), **extra}
    return call("agent.model", "invoke", args=args)


async def test_a_routed_model_call_is_an_ordinary_authorized_metered_tool_call(make_harness, key, caplog):
    wire = Wire("Two advisories are critical.")
    h, alice, agent, _ = await _setup(make_harness, wire)
    h.model.push(ask("model.invoke"), _routed(), final("digest written"))

    with caplog.at_level(logging.DEBUG):
        view = await _run(h, alice, agent["agent_id"])

    assert view["status"] == "completed", view
    [request] = wire.requests
    assert request.headers["authorization"] == f"Bearer {key}"
    assert key not in request.content.decode()
    # The model tool's output came back to the agent as an observation.
    assert "Two advisories are critical." in h.model.all_text()
    # Metered like any model tool call, attributed to the provider and model.
    usage = [u for u in await h.rows(UsageEvent) if u.model == "writer-model"]
    assert [(u.kind, u.provider) for u in usage] == [(UsageKind.MODEL_CALL, "openai_compatible")]
    # The key is nowhere but the auth header.
    assert key not in json.dumps(view) and key not in h.model.all_text()
    assert key not in await _everything_persisted(h)
    assert key not in caplog.text


async def test_the_worker_is_offered_agent_model_and_never_a_model_tool(make_harness, key):
    wire = Wire()
    h, alice, agent, _ = await _setup(make_harness, wire)
    h.model.push(final("ok"))
    await _run(h, alice, agent["agent_id"])
    system = h.model.seen[0][0].content
    assert "agent.model" in system
    assert "writer:" not in system and "writer-model" not in system and "llm.test" not in system


async def test_a_model_tool_cannot_be_called_around_the_routing(make_harness, key):
    wire = Wire()
    h, alice, agent, _ = await _setup(make_harness, wire)
    h.model.push(ask("model.invoke"), call("writer", "invoke", args={"prompt": "hi"}), final("no"))
    view = await _run(h, alice, agent["agent_id"])
    assert view["status"] == "completed"
    assert wire.requests == []
    assert "agent.model" in h.model.all_text()


@pytest.mark.parametrize("extra", [
    {"provider": "openai_compatible"}, {"model": "gpt-other"}, {"profile_id": "writer"},
    {"endpoint": "https://evil.test/v1"}, {"model_ref": "agent.primary"}, {"api_key": "x"},
    {"model_tool_id": "writer"},
])
async def test_a_request_cannot_name_a_provider_model_profile_endpoint_or_key(make_harness, key, extra):
    wire = Wire()
    h, alice, agent, _ = await _setup(make_harness, wire)
    h.model.push(ask("model.invoke"), _routed(**extra), final("no"))
    view = await _run(h, alice, agent["agent_id"])
    assert view["status"] == "completed"
    assert wire.requests == []
    assert "invalid_request" in h.model.all_text()


async def test_a_role_no_permitted_model_serves_is_refused(make_harness, key):
    wire = Wire()
    h, alice, agent, _ = await _setup(make_harness, wire)
    h.model.push(ask("model.invoke"), _routed(role="vision"), final("no"))
    await _run(h, alice, agent["agent_id"])
    assert wire.requests == []
    assert "no_model_for_role" in h.model.all_text()


async def test_a_profile_the_owner_may_not_use_is_never_chosen(make_harness, key):
    wire = Wire()
    h, alice, agent, _ = await _setup(make_harness, wire)
    service = h.app.state.agent_factory._factory.service
    service._registries = dataclasses.replace(service.registries, open_to_all=frozenset({"general-agentic"}))
    h.model.push(ask("model.invoke"), _routed(), final("no"))
    await _run(h, alice, agent["agent_id"])
    assert wire.requests == []
    assert "no_model_for_role" in h.model.all_text()


async def test_with_no_operator_budget_a_paid_model_is_never_chosen(make_harness, key):
    wire = Wire()
    config = _config()
    config["agents"]["default_budget_per_run"] = 0.0
    h, alice, agent, _ = await _setup(make_harness, wire, config=config)
    h.model.push(ask("model.invoke"), _routed(), final("no"))
    await _run(h, alice, agent["agent_id"])
    assert wire.requests == []
    assert "no_model_for_role" in h.model.all_text()


async def test_a_model_over_the_run_budget_is_never_chosen(make_harness, key):
    wire = Wire()
    config = _config()
    config["models_as_tools"][0]["pricing"] = {"input_per_1k_tokens": 10.0, "output_per_1k_tokens": 10.0}
    h, alice, agent, _ = await _setup(make_harness, wire, config=config)
    h.model.push(ask("model.invoke"), _routed(), final("no"))
    await _run(h, alice, agent["agent_id"])
    assert wire.requests == []
    assert "no_model_for_role" in h.model.all_text()


async def test_routed_calls_spend_the_agents_own_run_budget(make_harness, key):
    """No free or invisible agent calls: each routed call is prechecked against
    the run's budget — the tighter of the server's per-task budget (5.0) and
    the agent's per-run budget (0.02) — and its metered cost is the run's."""

    wire = Wire(usage=(5000, 2500))     # 0.01 per call at the configured prices
    config = _config()
    config["models_as_tools"][0]["pricing"] = {"input_per_1k_tokens": 0.001, "output_per_1k_tokens": 0.002}
    config["agents"]["default_budget_per_run"] = 0.02
    h, alice, agent, _ = await _setup(make_harness, wire, config=config)
    h.model.push(ask("model.invoke"), *[_routed() for _ in range(6)], final("never reached"))
    view = await _run(h, alice, agent["agent_id"])
    assert view["status"] == "failed" and view["failure_code"] == "budget_exceeded"
    assert len(wire.requests) == 2
    [run] = await h.rows(AgentRunRow)
    assert run.cost_total == pytest.approx(0.02)


async def test_routing_never_authorizes_a_revoked_grant_stops_the_call(make_harness, key):
    wire = Wire()
    h, alice, agent, grant_id = await _setup(make_harness, wire)

    async def revoke_then_route(messages):
        await h.revoke(alice, grant_id)
        return _routed()

    h.model.push(ask("model.invoke"), revoke_then_route, final("no"))
    await _run(h, alice, agent["agent_id"])
    assert wire.requests == []
    assert "Not permitted" in h.model.all_text()


async def test_without_model_invoke_in_the_envelope_there_is_no_routing(make_harness, key):
    wire = Wire()
    h = await make_harness(config=_config(), agent_tools=True, transport=httpx.MockTransport(wire.handler))
    alice = await h.user("alice")
    agent = await create_agent(h, alice)            # research_digest, no invoke_model_tool
    await h.grant(alice, "model.invoke")
    h.model.push(_routed(), ask("model.invoke"), final("no"))
    await _run(h, alice, agent["agent_id"])
    assert wire.requests == []
    assert "agent.model" not in h.model.seen[0][0].content
    denied = await h.rows(AuditEvent, AuditEvent.action == "agent.envelope.denied")
    assert {d.resource for d in denied} == {"tool:agent.model.invoke", "capability:model.invoke"}


async def test_an_ordinary_task_cannot_use_agent_model(make_harness, key):
    wire = Wire()
    h = await make_harness(config=_config(), agent_tools=True, transport=httpx.MockTransport(wire.handler))
    alice = await h.user("alice")
    await h.grant(alice, "model.invoke")
    h.model.push(ask("model.invoke"), _routed(), final("plain"))
    resp = await h.submit(alice, "hello")
    assert resp.status_code == 200
    assert wire.requests == []
    assert "Tool 'agent.model' is not available." in h.model.all_text()
