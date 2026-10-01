"""docs/29 §12 — the Model Gateway (Phase 3, slice 3B).

Every model call of an agent run — the run's own model and any model it
reaches as a tool — is a request to the Model Gateway, and passes, in order:

    1. the run context: its model token, the run, the agent (slice 3A)
    2–3. the permitted profile: the alias resolves to the spec's selected
       profile (or, for a model tool, one the spec's `model.invoke` reaches),
       still enabled, at the approved version, for this runtime, permitted to
       the owner *now*, and exactly the configured model the call goes to
    4–5. a model tool also needs `model.invoke` in the envelope, the owner's
       grant and the engine (the ordinary tool path — Phase 2)
    6–7. budgets: the owner's (13), the run's, and the agent's month — read
       live from attributed usage, so concurrent runs cannot overspend it
    8–9. usage attributed to the run; traced
    10–11. the configured provider (the key resolved inside `server.models`),
       a bounded result

A role or a preference only orders eligible profiles; nothing in a request can
name a provider, an endpoint or a key, and nothing the gateway answers widens
the envelope.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest

from server.agents.gateway.model_gateway import (
    AGENT_MODEL_ALIAS,
    budget_refusal,
    model_refusal,
    model_tool_alias,
)
from server.storage.models import (
    AgentConfiguration,
    AgentGatewayNonceRow,
    AgentInboxItemRow,
    AgentRunRow,
    AgentRunTokenRow,
    AgentRunUsageRow,
    AgentTask,
    AuditEvent,
    UsageEvent,
)
from shared.schemas.enums import AgentConfigScopeType, UsageKind
from tests.agents.harness import AGENTS_ON, create_agent, run_agent
from tests.agents.test_compiler import compile_, compiled
from tests.agents.test_future_interfaces import _bumped_research, _spec_with_model_tool
from tests.agents.test_model_routing_runs import (
    KEY_ENV,
    MODEL_DRAFT,
    Wire,
    _config,
    _everything_persisted,
    _routed,
    _setup,
    key,  # noqa: F401 — the fixture
)
from tests.runtime.conftest import ask, call, final

OWNER = "agent.primary"


# ── 2–3: the profile, the selection, the owner's policy, the model ───────


def _with_profile(reg, profile_id: str, **update):
    resolved = reg.model_profiles[profile_id]
    changed = dataclasses.replace(resolved, profile=resolved.profile.model_copy(update=update))
    return dataclasses.replace(reg, model_profiles={**reg.model_profiles, profile_id: changed})


@pytest.fixture
def reg():
    return _bumped_research()


@pytest.fixture
def spec(reg):
    return _spec_with_model_tool(reg)


def refusal(spec, reg, alias=AGENT_MODEL_ALIAS, *, provider="ollama", model="qwen2.5:3b-instruct", owner=OWNER):
    return model_refusal(spec, reg, alias=alias, provider=provider, model=model, owner_primary_model_ref=owner)


def test_the_runs_own_model_is_admitted_exactly_as_selected(spec, reg):
    assert spec.selection.model_profile_id == "general-agentic"
    assert refusal(spec, reg) is None


@pytest.mark.parametrize("provider,model", [
    ("openai_compatible", "qwen2.5:3b-instruct"),   # another provider (endpoint)
    ("ollama", "another-model"),                    # another model
    ("openai", "gpt-anything"),
])
def test_any_other_model_or_provider_is_refused(spec, reg, provider, model):
    assert refusal(spec, reg, provider=provider, model=model) == "model_mismatch"


@pytest.mark.parametrize("update,why", [
    ({"enabled": False}, "profile_unavailable"),
    ({"version": 2}, "profile_unavailable"),
    ({"supported_runtimes": ["letta"]}, "profile_unavailable"),
])
def test_a_profile_the_operator_changed_is_refused(spec, reg, update, why):
    assert refusal(spec, _with_profile(reg, "general-agentic", **update)) == why


def test_a_profile_the_owner_may_no_longer_use_is_refused(spec, reg):
    closed = dataclasses.replace(reg, open_to_all=frozenset({"writer", "painter"}))
    assert refusal(spec, closed) is None                                   # still the owner's primary
    assert refusal(spec, closed, owner="user-configuration:x") == "not_permitted"


@pytest.mark.parametrize("alias", ["", "gpt-4", "agent-model ", "model-tool:", "profile:writer", "models_as_tools.writer"])
def test_an_unknown_alias_is_refused(spec, reg, alias):
    assert refusal(spec, reg, alias) in {"unknown_alias", "profile_unavailable"}


def test_a_model_tool_needs_model_invoke_in_the_envelope(reg):
    web_only = compiled(compile_(reg=reg))
    assert refusal(web_only, reg, model_tool_alias("writer"), provider=None, model=None) == \
        "model_invoke_not_in_envelope"


def test_a_model_tool_is_admitted_only_if_permitted_and_configured(spec, reg):
    assert refusal(spec, reg, model_tool_alias("writer"), provider=None, model=None) is None
    assert refusal(spec, reg, model_tool_alias("painter"), provider=None, model=None) is None
    # Not open to all, and not the owner's primary: no role or preference changes that.
    assert refusal(spec, reg, model_tool_alias("private"), provider=None, model=None) == "not_permitted"
    assert refusal(spec, reg, model_tool_alias("nope"), provider=None, model=None) == "profile_unavailable"
    disabled = _with_profile(reg, "writer", enabled=False)
    assert refusal(spec, disabled, model_tool_alias("writer"), provider=None, model=None) == "profile_unavailable"
    assert refusal(spec, reg, model_tool_alias("writer"), provider="openai", model="writer-2") == "model_mismatch"


def test_nothing_in_a_request_can_name_a_provider_endpoint_or_key():
    """The gateway's inputs are an alias and the facts of what the caller is
    about to call — never a preference, a role, an endpoint or a key."""

    import inspect

    params = set(inspect.signature(model_refusal).parameters)
    assert params == {"spec", "registries", "alias", "provider", "model", "owner_primary_model_ref"}


# ── 6–7: the agent's month, live ─────────────────────────────────────────


@pytest.mark.parametrize("projected,spent,budget,expected", [
    (0.0, 10.0, 0.0, None),                    # a free (local) call is never refused
    (0.01, 0.0, 0.0, "agent_budget_exhausted"),  # a zero month allows only free calls
    (0.01, 0.02, 0.03, None),
    (0.01, 0.025, 0.03, "agent_budget_exhausted"),
    (0.01, 0.03, 0.03, "agent_budget_exhausted"),
])
def test_the_month_budget(projected, spent, budget, expected):
    assert budget_refusal(projected_cost=projected, month_spent=spent, month_budget=budget) == expected


# ── end to end ───────────────────────────────────────────────────────────

PRIMARY_KEY_ENV = "TEST_ONLY_PRIMARY_KEY"


def _keyed_primary_config(**agents) -> dict:
    """The run's own model is a keyed, paid, OpenAI-compatible endpoint."""

    base = dict(AGENTS_ON["agents"])
    base.update({"default_budget_per_run": 0.05, "default_budget_per_month": 1.0, **agents})
    return {
        **AGENTS_ON,
        "agents": base,
        "agent": {"provider": "openai_compatible", "model": "primary-model", "endpoint": "https://llm.test/v1",
                  "secret_ref": f"env:{PRIMARY_KEY_ENV}",
                  "pricing": {"input_per_1k_tokens": 0.00001, "output_per_1k_tokens": 0.00002},
                  "bounds": {"per_task_budget": 5.0}},
        "security": {"budgets": {"per_user_daily_cost_limit": 50.0, "global_daily_cost_limit": 500.0}},
    }


def _answer(content: str) -> str:
    return json.dumps({"type": "final_answer", "content": content})


async def test_the_provider_key_reaches_the_providers_auth_header_and_nothing_else(make_harness, monkeypatch,
                                                                                   caplog):
    key = f"TEST-ONLY-primary-key-{uuid.uuid4().hex}"
    monkeypatch.setenv(PRIMARY_KEY_ENV, key)
    wire = Wire(_answer("Two critical advisories."))
    h = await make_harness(config=_keyed_primary_config(), agent_tools=True,
                           transport=httpx.MockTransport(wire.handler))
    contexts: list[str] = []
    from server.agents.providers import native

    original = native.NativeRuntimeProvider.start_run

    async def start_run(self, ctx):
        contexts.append(repr(ctx) + ctx.model_dump_json())
        return await original(self, ctx)

    monkeypatch.setattr(native.NativeRuntimeProvider, "start_run", start_run)
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    resp = await run_agent(h, alice, agent["agent_id"])
    assert resp.status_code == 202 and resp.json()["status"] == "completed", resp.text

    [request] = wire.requests
    assert request.headers["authorization"] == f"Bearer {key}"
    assert key not in request.content.decode()           # not in what the model is shown
    persisted = await _everything_persisted(h)
    for model in (AgentRunTokenRow, AgentGatewayNonceRow, AgentInboxItemRow):
        persisted += "\n".join(json.dumps({c.name: str(getattr(r, c.name)) for c in model.__table__.columns})
                               for r in await h.rows(model))
    assert key not in persisted and key not in resp.text and key not in caplog.text
    assert contexts and all(key not in text for text in contexts)
    # The model call was attributed to the run and metered at its price.
    [usage] = [e for e in await h.rows(UsageEvent) if e.model == "primary-model"]
    assert usage.estimated_cost > 0
    [attributed] = await h.rows(AgentRunUsageRow)
    assert attributed.usage_id == usage.usage_id


async def test_a_model_the_owner_may_no_longer_use_stops_the_run_at_its_next_call(make_harness):
    config = {**AGENTS_ON, "agents": {**AGENTS_ON["agents"], "model_profiles_open_to_all": []}}
    h = await make_harness(config=config, agent_tools=True)
    alice = await h.user("alice")
    agent = await create_agent(h, alice)   # permitted: the profile is her primary

    async def her_own_model_now(messages):
        async with h.storage.session() as s:
            s.add(AgentConfiguration(
                scope_type=AgentConfigScopeType.USER, scope_id=alice.user_id,
                primary_model={"provider": "ollama", "model": "her-own-model", "endpoint": "http://127.0.0.1:11434"},
                updated_at=datetime.now(timezone.utc)))
            await s.commit()
        return call("no.such.tool", "x")

    h.model.push(her_own_model_now, final("never reached"))
    resp = await run_agent(h, alice, agent["agent_id"])
    view = resp.json()
    assert view["status"] == "failed" and view["failure_code"] == "agent_unavailable", view
    assert len(h.model.seen) == 1
    [denial] = await h.rows(AuditEvent, AuditEvent.action == "agent.gateway.denied")
    assert denial.resource.endswith(":model:model_not_allowed:not_permitted")


async def test_the_agents_month_is_enforced_live_across_concurrent_runs(make_harness, key):
    """Another run of the same agent, still running, has already spent most of
    the month: this run's first paid call is refused before the provider is
    called — the start-time check alone could not see that spend."""

    wire = Wire(usage=(5000, 2500))
    config = _config()
    # A routed call is projected at ~0.004 before it is made.
    config["models_as_tools"][0]["pricing"] = {"input_per_1k_tokens": 0.001, "output_per_1k_tokens": 0.002}
    config["agents"]["default_budget_per_month"] = 0.032
    h, alice, agent, _ = await _setup(make_harness, wire, config=config)
    now = datetime.now(timezone.utc)
    other_run, usage_id = uuid.uuid4(), uuid.uuid4()
    async with h.storage.session() as s:
        s.add(AgentRunRow(run_id=other_run, agent_id=uuid.UUID(agent["agent_id"]), owner_user_id=alice.user_id,
                          version=1, spec_hash="0" * 64, kind="on_demand", status="running", cost_total=0.0,
                          started_at=now))
        s.add(UsageEvent(usage_id=usage_id, request_id=uuid.uuid4(), user_id=alice.user_id,
                         kind=UsageKind.MODEL_CALL, provider="openai_compatible", model="writer-model",
                         tokens_or_units=7500, estimated_cost=0.03, timestamp=now))
        await s.flush()
        s.add(AgentRunUsageRow(run_id=other_run, usage_id=usage_id))
        await s.commit()
    h.model.push(ask("model.invoke"), _routed(), final("never reached"))
    resp = await run_agent(h, alice, agent["agent_id"])
    view = resp.json()
    assert view["status"] == "failed" and view["failure_code"] == "agent_budget_exhausted", view
    assert wire.requests == []
    limits = await h.rows(AuditEvent, AuditEvent.action == "usage.limit.exceeded")
    assert [a.resource for a in limits] == ["limit:agent_monthly_budget"]


async def test_a_routed_model_call_is_a_model_gateway_request_too(make_harness, key):
    """The specialist call needs the run's *model* token: revoked while the
    worker was answering, the routed call never reaches the provider."""

    wire = Wire("never sent")
    h, alice, agent, _ = await _setup(make_harness, wire)

    async def revoke_model_token_then_route(messages):
        from sqlalchemy import update

        async with h.storage.session() as s:
            await s.execute(update(AgentRunTokenRow).where(AgentRunTokenRow.purpose == "model")
                            .values(revoked_at=datetime.now(timezone.utc), revoked_reason="breaker"))
            await s.commit()
        return _routed()

    h.model.push(ask("model.invoke"), revoke_model_token_then_route, final("never reached"))
    view = (await run_agent(h, alice, agent["agent_id"])).json()
    assert view["status"] == "failed" and view["failure_code"] == "agent_unavailable", view
    assert wire.requests == []
    [denial] = await h.rows(AuditEvent, AuditEvent.action == "agent.gateway.denied")
    assert denial.resource.endswith(":model:invalid_run_token:revoked")


async def test_a_routed_call_is_admitted_by_the_model_gateway_and_attributed(make_harness, key):
    wire = Wire("A two-line summary.")
    h, alice, agent, _ = await _setup(make_harness, wire)
    h.model.push(ask("model.invoke"), _routed(), final("written"))
    view = (await run_agent(h, alice, agent["agent_id"])).json()
    assert view["status"] == "completed", view
    assert len(wire.requests) == 1
    nonces = await h.rows(AgentGatewayNonceRow)
    tokens = {t.token_id: t.purpose for t in await h.rows(AgentRunTokenRow)}
    by_purpose = sorted(tokens[n.token_id] for n in nonces)
    # 3 worker calls + 1 routed call on the model gateway; 1 `agent.model` request on the tool gateway.
    assert by_purpose == ["model"] * 4 + ["tool"]
    routed = [e for e in await h.rows(UsageEvent) if e.model == "writer-model"]
    attributed = {a.usage_id for a in await h.rows(AgentRunUsageRow)}
    assert len(routed) == 1 and routed[0].usage_id in attributed


async def test_usage_is_attributed_to_exactly_the_run_that_caused_it(make_harness):
    h = await make_harness(config=AGENTS_ON, agent_tools=True)
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    h.model.push(call("no.such.tool", "x"), final("first"), final("second"))
    first = (await run_agent(h, alice, agent["agent_id"])).json()
    second = (await run_agent(h, alice, agent["agent_id"])).json()
    usage = {u.usage_id for u in await h.rows(UsageEvent)}
    by_run: dict[str, set] = {}
    for row in await h.rows(AgentRunUsageRow):
        by_run.setdefault(str(row.run_id), set()).add(row.usage_id)
    assert set(by_run) == {first["run_id"], second["run_id"]}
    assert not by_run[first["run_id"]] & by_run[second["run_id"]]
    assert by_run[first["run_id"]] | by_run[second["run_id"]] == usage
    tasks = {str(t.task_id): t for t in await h.rows(AgentTask)}
    assert len(by_run[first["run_id"]]) == tasks[first["task_id"]].model_calls == 2
    assert len(by_run[second["run_id"]]) == tasks[second["task_id"]].model_calls == 1


async def test_a_preference_never_widens_the_envelope(make_harness, key):
    """No `model.invoke` in the envelope: whatever role or preference the
    agent names, no model is routed and no model gateway request is made."""

    wire = Wire("never sent")
    h = await make_harness(config=_config(), agent_tools=True, transport=httpx.MockTransport(wire.handler))
    alice = await h.user("alice")
    agent = await create_agent(h, alice)   # web only
    await h.grant(alice, "model.invoke")
    h.model.push(*[_routed(role, preference=p) for role, p in
                   (("writing", "thorough"), ("long_context", "cheaper"), (None, "faster"))], final("done"))
    view = (await run_agent(h, alice, agent["agent_id"])).json()
    assert view["status"] == "completed"
    assert wire.requests == []
    tokens = {t.token_id: t.purpose for t in await h.rows(AgentRunTokenRow)}
    model_requests = [n for n in await h.rows(AgentGatewayNonceRow) if tokens[n.token_id] == "model"]
    assert len(model_requests) == 4    # the worker's own calls only
