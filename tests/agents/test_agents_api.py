"""docs/29 §23.2 — the owner's `/api/v1/agents` endpoints (Phase 1).

`02` conventions: Bearer auth, owner-only, anti-enumeration `404`,
`403 confirmation_required` with a single-use token bound to the exact action,
retried with `X-Confirmation-Token`. A malformed draft is `422` naming fields,
never echoing values. Without `agents.enabled`, `503 dependency_unavailable`.
"""

from __future__ import annotations

import uuid

import pytest

from server.storage.models import AgentDefinitionRow, AgentSpecVersionRow
from tests.agents.harness import AGENTS_ON, ALLOWED_HOST, DRAFT
from tests.runtime.conftest import _deep_merge

API = "/api/v1/agents"
HEADER = "X-Confirmation-Token"


@pytest.fixture
async def h(make_harness):
    return await make_harness(config=AGENTS_ON, agent_tools=True)


async def compile_(h, actor, draft=None, agent_id=None):
    params = {"agent_id": agent_id} if agent_id else None
    return await h.client.post(f"{API}/compile", json=draft or DRAFT, params=params, headers=actor.auth)


async def create(h, actor, *, draft=None) -> dict:
    compiled = (await compile_(h, actor, draft)).json()
    assert compiled["kind"] == "compiled", compiled
    first = await h.client.post(API, json={"compile_id": compiled["compile_id"]}, headers=actor.auth)
    assert first.status_code == 403, first.text
    token = first.json()["error"]["details"]["confirmation_token"]
    second = await h.client.post(API, json={"compile_id": compiled["compile_id"]},
                                 headers={**actor.auth, HEADER: token})
    assert second.status_code == 201, second.text
    return second.json()


# ── compile ──────────────────────────────────────────────────────────────


async def test_compiling_returns_a_bound_preview_with_a_deterministic_card(h):
    alice = await h.user("alice")
    resp = await compile_(h, alice)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "compiled" and body["compile_id"]
    card = body["spec_preview"]["card"]
    assert card["title"] == 'Create agent "Security advisory digest" (v1)'
    assert "read the web from approved sites" in card["can"]
    assert "create agents" in card["cannot"] and "run shell commands" in card["cannot"]
    assert card["results_go"] == "your JARVIS inbox only"
    assert await h.rows(AgentDefinitionRow) == []


async def test_a_malformed_draft_is_422_naming_fields_only(h):
    alice = await h.user("alice")
    secret = "sk-" + "never-echo-this"
    resp = await compile_(h, alice, {**DRAFT, "secret_ref": secret, "graph_id": str(uuid.uuid4())})
    assert resp.status_code == 422, resp.text
    assert secret not in resp.text
    assert set(resp.json()["error"]["details"]["fields"]) >= {"secret_ref", "graph_id"}


async def test_a_clarification_is_returned_not_guessed(h):
    alice = await h.user("alice")
    resp = await compile_(h, alice, {**DRAFT, "sources": [{"kind": "url", "value": "https://elsewhere.example"}]})
    body = resp.json()
    assert resp.status_code == 200 and body["kind"] == "needs_clarification"
    assert "source_not_allowlisted" in body["reason_codes"]


# ── create: confirmation of the exact spec ───────────────────────────────


async def test_creation_needs_a_confirmation_bound_to_that_preview(h):
    alice = await h.user("alice")
    a = (await compile_(h, alice)).json()
    b = (await compile_(h, alice, {**DRAFT, "name": "Other"})).json()
    first = await h.client.post(API, json={"compile_id": a["compile_id"]}, headers=alice.auth)
    assert first.status_code == 403
    details = first.json()["error"]["details"]
    assert details["action"] == "create_agent" and details["risk_category"] == "consequential"
    assert details["card"]["title"].startswith('Create agent "Security advisory digest"')
    # A token for preview A does not create preview B.
    other = await h.client.post(API, json={"compile_id": b["compile_id"]},
                                headers={**alice.auth, HEADER: details["confirmation_token"]})
    assert other.status_code == 403 and other.json()["error"]["code"] == "confirmation_required"
    assert await h.rows(AgentDefinitionRow) == []
    ok = await h.client.post(API, json={"compile_id": a["compile_id"]},
                             headers={**alice.auth, HEADER: details["confirmation_token"]})
    assert ok.status_code == 201, ok.text
    view = ok.json()
    assert view["status"] == "active" and view["current_version"] == 1
    assert view["template_id"] == "research_digest" and "create agents" in view["cannot"]
    # Single use: the preview is consumed.
    again = await h.client.post(API, json={"compile_id": a["compile_id"]},
                                headers={**alice.auth, HEADER: details["confirmation_token"]})
    assert again.status_code == 404


async def test_the_owner_and_graph_come_from_the_session(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    graph_id = await h.shared_graph(alice, bob)
    view = await create(h, alice)
    [row] = await h.rows(AgentDefinitionRow)
    assert (row.owner_user_id, row.graph_id, str(row.agent_id)) == (alice.user_id, graph_id, view["agent_id"])


# ── AGENT-T9 over HTTP ───────────────────────────────────────────────────


async def test_agent_t9_another_user_cannot_see_use_change_or_delete(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    compiled = (await compile_(h, alice)).json()
    # Bob cannot redeem Alice's preview, nor render its card.
    assert (await h.client.post(API, json={"compile_id": compiled["compile_id"]}, headers=bob.auth)).status_code == 404
    assert (await h.client.get(f"{API}/previews/{compiled['compile_id']}", headers=bob.auth)).status_code == 404
    view = await create(h, alice)
    agent = view["agent_id"]
    assert (await h.client.get(f"{API}/{agent}", headers=bob.auth)).status_code == 404
    assert (await h.client.delete(f"{API}/{agent}", headers=bob.auth)).status_code == 404
    patch = await h.client.patch(f"{API}/{agent}", json={"compile_id": str(uuid.uuid4())}, headers=bob.auth)
    assert patch.status_code == 404
    assert (await compile_(h, bob, agent_id=agent)).status_code == 404
    assert (await h.client.get(API, headers=bob.auth)).json()["items"] == []
    # Identical to an agent that does not exist.
    assert (await h.client.get(f"{API}/{uuid.uuid4()}", headers=bob.auth)).status_code == 404


# ── read, update, delete ─────────────────────────────────────────────────


async def test_list_and_get(h):
    alice = await h.user("alice")
    view = await create(h, alice)
    listed = (await h.client.get(API, headers=alice.auth)).json()["items"]
    assert [v["agent_id"] for v in listed] == [view["agent_id"]]
    detail = (await h.client.get(f"{API}/{view['agent_id']}", headers=alice.auth)).json()
    assert detail["agent"]["agent_id"] == view["agent_id"]
    assert detail["spec"]["card"]["results_go"] == "your JARVIS inbox only"


async def test_update_is_a_confirmed_new_version(h):
    alice = await h.user("alice")
    view = await create(h, alice)
    agent = view["agent_id"]
    recompiled = (await compile_(h, alice, {**DRAFT, "name": "Digest v2"}, agent_id=agent)).json()
    assert recompiled["spec_preview"]["card"]["title"] == 'Update agent "Digest v2" to v2'
    first = await h.client.patch(f"{API}/{agent}", json={"compile_id": recompiled["compile_id"]}, headers=alice.auth)
    assert first.status_code == 403
    token = first.json()["error"]["details"]["confirmation_token"]
    ok = await h.client.patch(f"{API}/{agent}", json={"compile_id": recompiled["compile_id"]},
                              headers={**alice.auth, HEADER: token})
    assert ok.status_code == 200, ok.text
    assert (ok.json()["current_version"], ok.json()["name"]) == (2, "Digest v2")
    assert len(await h.rows(AgentSpecVersionRow)) == 2


async def test_a_create_preview_cannot_update_an_agent(h):
    alice = await h.user("alice")
    view = await create(h, alice)
    fresh = (await compile_(h, alice)).json()
    resp = await h.client.patch(f"{API}/{view['agent_id']}", json={"compile_id": fresh["compile_id"]},
                                headers=alice.auth)
    assert resp.status_code == 404


async def test_delete_is_confirmed(h):
    alice = await h.user("alice")
    agent = (await create(h, alice))["agent_id"]
    first = await h.client.delete(f"{API}/{agent}", headers=alice.auth)
    assert first.status_code == 403
    token = first.json()["error"]["details"]["confirmation_token"]
    ok = await h.client.delete(f"{API}/{agent}", headers={**alice.auth, HEADER: token})
    assert ok.status_code == 204
    assert (await h.client.get(f"{API}/{agent}", headers=alice.auth)).status_code == 404
    [row] = await h.rows(AgentDefinitionRow)
    assert row.status == "deleted" and row.name is None


# ── no credential ever leaves the configuration ──────────────────────────


async def test_provider_credentials_never_reach_a_spec_or_a_response(make_harness):
    endpoint = "https://api.provider.example.invalid/v1"
    handle = "secretstore:writer-model-key"
    config = _deep_merge(AGENTS_ON, {
        "models_as_tools": [{"id": "writer", "provider": "openai", "model": "writer-1", "endpoint": endpoint,
                             "secret_ref": handle,
                             "pricing": {"input_per_1k_tokens": 0.0, "output_per_1k_tokens": 0.0}}],
    })
    config["agents"]["model_profiles"].append({
        "profile_id": "writer", "model_ref": "models_as_tools.writer", "context_window": 8192,
        "features": ["structured_output", "writing"], "supported_runtimes": ["native"],
    })
    config["agents"]["model_profiles_open_to_all"] = ["general-agentic", "writer"]
    h = await make_harness(config=config, agent_tools=True)
    alice = await h.user("alice")
    view = await create(h, alice, draft={**DRAFT, "preferred_model_features": ["writing"]})
    detail = await h.client.get(f"{API}/{view['agent_id']}", headers=alice.auth)
    assert "writer" in detail.text
    [spec] = await h.rows(AgentSpecVersionRow)
    for text in (detail.text, repr(spec.spec_json)):
        assert handle not in text and "api.provider.example.invalid" not in text


# ── agents.enabled: false ────────────────────────────────────────────────


@pytest.mark.parametrize("method,path", [
    ("post", f"{API}/compile"), ("post", API), ("get", API), ("get", f"{API}/{uuid.uuid4()}"),
    ("patch", f"{API}/{uuid.uuid4()}"), ("delete", f"{API}/{uuid.uuid4()}"),
    ("get", f"{API}/previews/{uuid.uuid4()}"),
])
async def test_disabled_every_endpoint_is_503(make_harness, method, path):
    h = await make_harness()
    alice = await h.user("alice")
    kwargs = {"json": DRAFT if path.endswith("compile") else {"compile_id": str(uuid.uuid4())}} \
        if method in ("post", "patch") else {}
    resp = await getattr(h.client, method)(path, headers=alice.auth, **kwargs)
    assert resp.status_code == 503, resp.text
    assert resp.json()["error"]["details"]["dependency"] == "agents"


async def test_unauthenticated_is_refused(h):
    assert (await h.client.get(API)).status_code == 401
    assert ALLOWED_HOST  # the harness allows one host
