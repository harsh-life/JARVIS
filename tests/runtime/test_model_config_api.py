"""Pilot plan C7 (M2): the caller's own primary model and write-only model keys
(OD-PILOT-2 as confirmed 2026-10-13: one private workspace per user; no
graph-scoped model configuration).

Through the production composition root with the production OpenAI-compatible
adapter on a recording transport, so the test sees what goes on the wire: a
user's key is used only for that user's own tasks, reaches the provider only
in the `Authorization` header, and is never returned, logged, audited or
echoed by a validation error.
"""

from __future__ import annotations

import json
import logging
import uuid

import pytest

from server.composition.models import ConfiguredModelResolver
from server.gateway.app import API_V1_PREFIX
from server.storage.models import AgentConfiguration, AgentTask, AuditEvent, SecretReference, UsageEvent
from shared.schemas.enums import SecretClass, SecretOwnerScopeType
from tests.runtime.conftest import failure_of, final
from tests.runtime.test_secrets_and_providers import PAID, WireLog, _store_secret

CATALOG = f"{API_V1_PREFIX}/config/model/catalog"
MODEL = f"{API_V1_PREFIX}/config/model"
SECRETS = f"{API_V1_PREFIX}/secrets"


def _config() -> dict:
    return {
        "agent": {
            "provider": "openai_compatible", "model": "gpt-test", "endpoint": "https://llm.test/v1",
            "pricing": PAID, "bounds": {"per_task_budget": 5.0},
            "fallback": {"provider": "ollama", "model": "qwen-local"},
        },
        "security": {"budgets": {"per_user_daily_cost_limit": 50.0, "global_daily_cost_limit": 500.0}},
    }


async def _setup(make_harness, *answers):
    wire = WireLog(*answers)
    h = await make_harness(config=_config(), transport=wire.transport)
    operator_key = f"TEST-ONLY-operator-{uuid.uuid4().hex}"
    h.config.agent.secret_ref = f"secretstore:{await _store_secret(h, operator_key)}"
    return h, wire, operator_key


async def _key(h, actor, value: str) -> str:
    resp = await h.client.post(SECRETS, json={"value": value}, headers=actor.auth)
    assert resp.status_code == 201, resp.text
    assert set(resp.json()) == {"key_ref"}
    return resp.json()["key_ref"]


async def _persisted(h) -> str:
    blobs = []
    for model in (AuditEvent, UsageEvent, AgentTask, AgentConfiguration, SecretReference):
        for row in await h.rows(model):
            blobs.append(json.dumps({a.key: str(getattr(row, a.key)) for a in model.__mapper__.column_attrs}))
    return "\n".join(blobs)


async def test_the_catalog_is_the_operators_entries_without_endpoint_or_key(make_harness):
    h, _, _ = await _setup(make_harness)
    alice = await h.user("alice")
    resp = await h.client.get(CATALOG, headers=alice.auth)
    assert resp.status_code == 200, resp.text
    items = {i["id"]: i for i in resp.json()["items"]}
    assert set(items) == {"agent.primary", "agent.fallback"}
    assert items["agent.primary"] | {} == {
        "id": "agent.primary", "provider": "openai_compatible", "model": "gpt-test", "local": False,
        "pricing": PAID, "requires_user_key": True, "server_default": True,
    }
    assert items["agent.fallback"]["local"] is True and items["agent.fallback"]["requires_user_key"] is False
    assert "llm.test" not in resp.text and "secretstore" not in resp.text


async def test_a_users_key_serves_only_their_own_tasks_and_only_on_the_wire(make_harness, caplog):
    h, wire, operator_key = await _setup(make_harness, final("alice answered"), final("bob answered"))
    alice, bob = await h.user("alice"), await h.user("bob")
    secret = f"TEST-ONLY-alice-{uuid.uuid4().hex}"
    with caplog.at_level(logging.DEBUG):
        ref = await _key(h, alice, secret)
        put = await h.client.put(MODEL, json={"catalog_id": "agent.primary", "key_ref": ref}, headers=alice.auth)
        assert put.status_code == 200, put.text
        assert put.json() == {"source": "user", "catalog_id": "agent.primary", "provider": "openai_compatible",
                              "model": "gpt-test", "key_ref": ref, "key_usable": True}
        assert (await h.submit(alice, "two plus two")).status_code == 200
        assert (await h.submit(bob, "two plus three")).status_code == 200
    assert [r.headers["authorization"] for r in wire.requests] == [f"Bearer {secret}", f"Bearer {operator_key}"]
    assert all(str(r.url).startswith("https://llm.test/v1") for r in wire.requests)   # the operator's endpoint
    assert secret not in caplog.text
    assert secret not in await _persisted(h)
    got = await h.client.get(MODEL, headers=alice.auth)
    assert secret not in got.text and got.json()["key_ref"] == ref
    assert (await h.client.get(MODEL, headers=bob.auth)).json()["source"] == "server_default"


@pytest.mark.parametrize("body", [
    b'{"value": 12345}',
    b'{"value": "TEST-ONLY-echo-me", "scope": "server"}',
    b'{"value": "TEST-ONLY-echo-me", "class": "master_key"}',
    b'["TEST-ONLY-echo-me"]',
    b'TEST-ONLY-echo-me',
    b'{"value": "TEST-ONLY-echo me with space"}',
    b'{"value": "   "}',
])
async def test_a_malformed_key_request_never_echoes_the_value(make_harness, body, caplog):
    h, _, _ = await _setup(make_harness)
    alice = await h.user("alice")
    with caplog.at_level(logging.DEBUG):
        resp = await h.client.post(SECRETS, content=body,
                                   headers={**alice.auth, "Content-Type": "application/json"})
    assert resp.status_code == 422, resp.text
    assert "TEST-ONLY" not in resp.text and "12345" not in resp.text
    assert "TEST-ONLY" not in caplog.text
    assert not [r for r in await h.rows(SecretReference)
                if r.class_ is SecretClass.MODEL_API_KEY and r.owner_scope_type is SecretOwnerScopeType.USER]


async def test_only_the_callers_own_live_model_key_can_be_selected(make_harness):
    h, _, _ = await _setup(make_harness)
    alice, bob = await h.user("alice"), await h.user("bob")
    bobs = await _key(h, bob, f"TEST-ONLY-bob-{uuid.uuid4().hex}")
    server_ref = await _store_secret(h, "TEST-ONLY-server-owned")
    revoked = await _key(h, alice, f"TEST-ONLY-alice-{uuid.uuid4().hex}")
    assert (await h.client.delete(f"{SECRETS}/{revoked}", headers=alice.auth)).status_code == 204
    for ref in (bobs, server_ref, revoked, "env:HYPERMIND_KEK", "secretstore:" + uuid.uuid4().hex):
        resp = await h.client.put(MODEL, json={"catalog_id": "agent.primary", "key_ref": ref}, headers=alice.auth)
        assert resp.status_code == 404, (ref, resp.text)
    # Nor can another user's key be revoked through this API.
    assert (await h.client.delete(f"{SECRETS}/{bobs}", headers=alice.auth)).status_code == 404
    assert (await h.client.get(MODEL, headers=alice.auth)).json()["source"] == "server_default"


@pytest.mark.parametrize(("body", "status"), [
    ({"catalog_id": "agent.primary"}, 422),                                     # paid needs the user's own key
    ({"catalog_id": "agent.fallback", "key_ref": "secretstore:abc"}, 422),     # a local model takes no key
    ({"catalog_id": "models_as_tools.x"}, 422),                                 # not in the catalog
    ({"catalog_id": "agent.fallback", "endpoint": "https://evil.test/v1"}, 422),  # a user never names an endpoint
    ({"catalog_id": "agent.fallback", "graph_id": str(uuid.uuid4())}, 422),    # no graph-scoped config
    ({"catalog_id": "agent.fallback", "provider": "openai", "model": "x"}, 422),
])
async def test_what_a_selection_may_not_say(make_harness, body, status):
    h, _, _ = await _setup(make_harness)
    alice = await h.user("alice")
    resp = await h.client.put(MODEL, json=body, headers=alice.auth)
    assert resp.status_code == status, resp.text
    assert await h.rows(AgentConfiguration) == []


async def test_a_revoked_key_fails_closed_never_onto_the_operators_key(make_harness):
    h, wire, _ = await _setup(make_harness)
    alice = await h.user("alice")
    ref = await _key(h, alice, f"TEST-ONLY-alice-{uuid.uuid4().hex}")
    await h.client.put(MODEL, json={"catalog_id": "agent.primary", "key_ref": ref}, headers=alice.auth)
    assert (await h.client.delete(f"{SECRETS}/{ref}", headers=alice.auth)).status_code == 204
    assert (await h.client.get(MODEL, headers=alice.auth)).json()["key_usable"] is False
    resp = await h.submit(alice)
    assert resp.status_code == 503 and failure_of(resp) == "model_unavailable"
    # Neither the paid model nor the operator's key: only the operator's own
    # keyless local fallback is tried (the ordinary worker chain, 18 §4).
    assert not [r for r in wire.requests if r.url.host == "llm.test"]
    assert not [r for r in wire.requests if "authorization" in r.headers]
    # Back to the server default: the operator's model and key again.
    assert (await h.client.delete(MODEL, headers=alice.auth)).status_code == 204
    assert (await h.client.get(MODEL, headers=alice.auth)).json()["source"] == "server_default"
    assert await h.rows(AgentConfiguration) == []


async def test_a_local_selection_and_the_users_tools_are_unchanged(make_harness):
    h, _, _ = await _setup(make_harness)
    alice = await h.user("alice")
    resp = await h.client.put(MODEL, json={"catalog_id": "agent.fallback"}, headers=alice.auth)
    assert resp.status_code == 200, resp.text
    assert resp.json()["provider"] == "ollama" and resp.json()["key_ref"] is None
    # The row configures the model only: the user's tools stay the server's.
    async with h.storage.session() as s:
        resolver = ConfiguredModelResolver(config=h.config, session=s, graph_repository=h.core.graph_repository)
        from shared.schemas.authorization import Principal

        resolved = await resolver.resolve(
            principal=Principal(user_id=alice.user_id, device_id=alice.device_id, session_id=uuid.uuid4()),
            graph_id=None,
        )
    assert resolved.allowed_tool_ids is None


async def test_keys_are_capped_and_everything_needs_a_token(make_harness):
    h, _, _ = await _setup(make_harness)
    alice = await h.user("alice")
    for i in range(10):
        await _key(h, alice, f"TEST-ONLY-{i}-{uuid.uuid4().hex}")
    assert (await h.client.post(SECRETS, json={"value": "TEST-ONLY-eleven"}, headers=alice.auth)).status_code == 409
    for method, url in (("GET", CATALOG), ("GET", MODEL), ("PUT", MODEL), ("DELETE", MODEL), ("POST", SECRETS),
                        ("DELETE", f"{SECRETS}/secretstore:x")):
        assert (await h.client.request(method, url, json={})).status_code == 401, (method, url)
