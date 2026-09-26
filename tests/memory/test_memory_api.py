"""02 §7 memory endpoints, end to end on the real stack (MEM-T1/T2/T5/T10, API-T1..T10).

Production composition root, real OIDC → device → token onboarding, the real
`AuthorizationEngine` (with the `mem0fact` loader registered), real single-use
confirmation tokens, and the real Mem0 store. Nothing on the authorization or
storage path is substituted.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from server.gateway.app import API_V1_PREFIX
from server.storage.models import AuditEvent
from tests.memory.conftest import unique

pytestmark = pytest.mark.asyncio

MEM = f"{API_V1_PREFIX}/memory"
FAKE_KEY = "sk-proj-TESTONLY" + "q" * 24


async def remember(h, actor, text, *, fact_type="preference", graph_id=None, headers=None):
    body = {"fact_type": fact_type, "content": text}
    if graph_id is not None:
        body["graph_id"] = str(graph_id)
    return await h.client.post(MEM, json=body, headers={**actor.auth, **(headers or {})})


async def listed(h, actor, query=None) -> list[dict]:
    resp = await h.client.get(MEM, params={"query": query} if query else {}, headers=actor.auth)
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


async def confirmed(h, actor, method, url, **kwargs):
    """Drive the confirmation flow: first call → token, second call with it."""

    first = await h.client.request(method, url, headers=actor.auth, **kwargs)
    assert first.status_code == 403, first.text
    err = first.json()["error"]
    assert err["code"] == "confirmation_required"
    token = err["details"]["confirmation_token"]
    second = await h.client.request(method, url, headers={**actor.auth, "X-Confirmation-Token": token}, **kwargs)
    return second, token


async def share(h, actor, fact_id):
    resp, _ = await confirmed(h, actor, "PATCH", f"{MEM}/{fact_id}", json={"visibility": "graph"})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def audit_rows(h, action: str) -> list[AuditEvent]:
    async with h.storage.session() as s:
        return list((await s.execute(select(AuditEvent).where(AuditEvent.action == action))).scalars().all())


async def all_audit_text(h) -> str:
    async with h.storage.session() as s:
        rows = (await s.execute(select(AuditEvent))).scalars().all()
    return "\n".join(f"{r.action} {r.resource}" for r in rows)


@pytest.fixture
async def world(stack):
    h = await stack()
    alice, bob, carol = await h.user("alice"), await h.user("bob"), await h.user("carol")
    graph = await h.shared_graph(alice, bob)  # carol is not a member
    resp = await h.client.post(f"{API_V1_PREFIX}/graphs", json={"name": "carol's", "type": "private"},
                               headers=carol.auth)
    await h.enter_graph(carol, uuid.UUID(resp.json()["graph_id"]))
    return h, alice, bob, carol, graph


# ── write ──────────────────────────────────────────────────────────────────


async def test_mem_t2_an_explicit_add_is_the_callers_private_fact_in_the_active_graph(world):
    h, alice, _, _, graph = world
    text = unique("The user prefers metric units")
    resp = await remember(h, alice, text)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["visibility"] == "private"
    assert body["owner_user_id"] == body["source_user_id"] == str(alice.user_id)
    assert body["graph_id"] == str(graph)
    assert body["content"] == text
    written = await audit_rows(h, "memory.write")
    assert [r.resource for r in written] == [f"mem0fact:{body['fact_id']}"]
    assert text not in await all_audit_text(h)


async def test_a_body_cannot_assert_owner_source_or_visibility(world):
    h, alice, bob, _, _ = world
    resp = await h.client.post(MEM, json={
        "fact_type": "preference", "content": "The user prefers tea",
        "owner_user_id": str(bob.user_id), "visibility": "graph",
    }, headers=alice.auth)
    assert resp.status_code == 422


@pytest.mark.parametrize("fact_type", ["emotion", "relationship", "secret", "mood"])
async def test_mem_t3_an_unsupported_fact_type_is_422_and_stores_nothing(world, fact_type):
    h, alice, *_ = world
    resp = await remember(h, alice, "The user prefers tea", fact_type=fact_type)
    assert resp.status_code == 422
    assert await listed(h, alice) == []


async def test_mp_t5_a_secret_is_rejected_audited_without_the_value_and_never_stored(world):
    h, alice, *_ = world
    resp = await remember(h, alice, f"The user's API key is {FAKE_KEY}")
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["reason"] == "secret_detected:openai_style_key"
    assert FAKE_KEY not in resp.text
    blocked = await audit_rows(h, "memory.write.blocked")
    assert [r.resource for r in blocked] == ["mem0fact:blocked:secret_detected:openai_style_key"]
    assert FAKE_KEY not in await all_audit_text(h)
    assert await listed(h, alice) == []
    every_record = h.app.state.memory.provider.mem0.vector_store.list(filters=None, top_k=10_000)[0]
    assert all(FAKE_KEY not in str(row.payload) for row in every_record)


async def test_mem_t4_emotional_content_is_rejected(world):
    h, alice, *_ = world
    resp = await remember(h, alice, "The user feels lonely at night")
    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["reason"] == "emotional_or_relationship_content"
    assert await listed(h, alice) == []


async def test_writing_into_a_graph_the_caller_is_not_in_is_404_and_stores_nothing(world):
    """API-T2: `graph_id` in a body is a claim the engine checks (D1)."""

    h, alice, _, carol, graph = world
    resp = await remember(h, carol, unique("The user prefers espionage"), graph_id=graph)
    assert resp.status_code == 404
    assert await listed(h, carol) == []
    assert all("espionage" not in f["content"] for f in await listed(h, alice))


async def test_a_write_needs_a_graph_context(stack):
    h = await stack()
    dave = await h.user("dave")  # never entered a graph
    resp = await remember(h, dave, "The user prefers tea")
    assert resp.status_code == 422


async def test_an_idempotent_retry_does_not_store_twice(world):
    h, alice, *_ = world
    text = unique("The user prefers retry-safe tea")
    first = await remember(h, alice, text, headers={"Idempotency-Key": "k-1"})
    again = await remember(h, alice, text, headers={"Idempotency-Key": "k-1"})
    assert first.status_code == again.status_code == 201
    assert first.json()["fact_id"] == again.json()["fact_id"]
    assert [f["content"] for f in await listed(h, alice)].count(text) == 1


async def test_an_idempotent_replay_keeps_no_copy_of_the_fact_text(world):
    from server.storage.models import IdempotencyKey

    h, alice, *_ = world
    text = unique("The user prefers replay-safe cocoa")
    first = await remember(h, alice, text, headers={"Idempotency-Key": "k-2"})
    fact_id = first.json()["fact_id"]
    async with h.storage.session() as s:
        stored = (await s.execute(select(IdempotencyKey))).scalars().all()
    assert all(text not in str(row.response_body) for row in stored)
    assert [row.response_body for row in stored if fact_id in str(row.response_body)] == [{"fact_id": fact_id}]

    gone, _ = await confirmed(h, alice, "DELETE", f"{MEM}/{fact_id}")
    assert gone.status_code == 204
    replay = await remember(h, alice, text, headers={"Idempotency-Key": "k-2"})
    assert replay.status_code == 404  # the deleted fact is not resurrected from a stored response


async def test_writes_can_be_turned_off_while_recall_and_deletion_stay_available(stack):
    h = await stack(config={"memory": {"writes_enabled": False}})
    alice = await h.user("alice")
    await h.shared_graph(alice)
    resp = await remember(h, alice, "The user prefers tea")
    assert resp.status_code == 409
    assert await listed(h, alice) == []


# ── read: RAUTH-003 through the API (MEM-T1) ───────────────────────────────


async def test_mem_t1_private_is_owner_only_and_shared_reaches_active_members_only(world):
    h, alice, bob, carol, graph = world
    private = unique("The user prefers salary talks in private")
    shared_text = unique("The user wants the project shipped Friday")
    await remember(h, alice, private)
    shared_id = (await remember(h, alice, shared_text, fact_type="stated_goal")).json()["fact_id"]
    await share(h, alice, shared_id)

    alice_sees = {f["content"] for f in await listed(h, alice)}
    bob_sees = {f["content"] for f in await listed(h, bob)}
    carol_sees = {f["content"] for f in await listed(h, carol)}
    assert {private, shared_text} <= alice_sees
    assert shared_text in bob_sees and private not in bob_sees
    assert private not in carol_sees and shared_text not in carol_sees

    # The same with a semantic query.
    bob_query = {f["content"] for f in await listed(h, bob, query="salary project shipped")}
    assert shared_text in bob_query and private not in bob_query
    assert {f["content"] for f in await listed(h, carol, query="salary project shipped")}.isdisjoint(
        {private, shared_text})


async def test_graph_membership_is_read_live(world):
    h, alice, bob, _, graph = world
    text = unique("The user wants the roadmap reviewed")
    fact_id = (await remember(h, alice, text)).json()["fact_id"]
    await share(h, alice, fact_id)
    assert text in {f["content"] for f in await listed(h, bob)}
    r = await h.client.delete(f"{API_V1_PREFIX}/graphs/{graph}/members/{bob.user_id}", headers=alice.auth)
    assert r.status_code == 204
    assert text not in {f["content"] for f in await listed(h, bob)}


async def test_cross_graph_shared_memory_does_not_leak(stack):
    h = await stack()
    alice, bob, carol = await h.user("alice"), await h.user("bob"), await h.user("carol")
    g1 = await h.shared_graph(alice, bob)
    g2 = await h.shared_graph(carol, bob)
    await h.enter_graph(alice, g1)
    text = unique("The user wants g1-only knowledge kept in g1")
    fact_id = (await remember(h, alice, text, graph_id=g1)).json()["fact_id"]
    await share(h, alice, fact_id)
    assert text in {f["content"] for f in await listed(h, bob)}      # member of g1
    assert text not in {f["content"] for f in await listed(h, carol)}  # member of g2 only
    assert g2 != g1


# ── owner-only correction, sharing, deletion (MEM-T5, MEM-T10) ─────────────


async def test_mem_t5_only_the_owner_corrects_content(world):
    h, alice, bob, carol, _ = world
    fact_id = (await remember(h, alice, "The user prefers the board in list view")).json()["fact_id"]
    await share(h, alice, fact_id)

    by_member = await h.client.patch(f"{MEM}/{fact_id}", json={"content": "The user prefers nothing"},
                                     headers=bob.auth)
    assert by_member.status_code == 403  # bob can see it; he cannot re-govern it
    by_outsider = await h.client.patch(f"{MEM}/{fact_id}", json={"content": "x y z"}, headers=carol.auth)
    assert by_outsider.status_code == 404
    by_owner = await h.client.patch(f"{MEM}/{fact_id}", json={"content": "The user prefers the board in kanban view"},
                                    headers=alice.auth)
    assert by_owner.status_code == 200, by_owner.text
    assert by_owner.json()["content"] == "The user prefers the board in kanban view"
    assert [r.resource for r in await audit_rows(h, "memory.corrected")] == [f"mem0fact:{fact_id}"]


async def test_a_correction_goes_through_the_write_gate(world):
    h, alice, *_ = world
    fact_id = (await remember(h, alice, "The user prefers tea")).json()["fact_id"]
    resp = await h.client.patch(f"{MEM}/{fact_id}", json={"content": f"key {FAKE_KEY}"}, headers=alice.auth)
    assert resp.status_code == 422
    assert [f["content"] for f in await listed(h, alice) if f["fact_id"] == fact_id] == ["The user prefers tea"]


async def test_a_private_fact_is_404_to_everyone_but_its_owner(world):
    h, alice, bob, carol, _ = world
    fact_id = (await remember(h, alice, unique("The user prefers private tea"))).json()["fact_id"]
    for actor in (bob, carol):
        for method, kwargs in (("PATCH", {"json": {"content": "The user prefers x"}}),
                               ("PATCH", {"json": {"visibility": "graph"}}),
                               ("DELETE", {})):
            resp = await h.client.request(method, f"{MEM}/{fact_id}", headers=actor.auth, **kwargs)
            assert resp.status_code == 404, (actor.subject, method, resp.text)
    unknown = await h.client.delete(f"{MEM}/{uuid.uuid4()}", headers=alice.auth)
    assert unknown.status_code == 404
    assert unknown.json() == {**unknown.json(), "error": {**unknown.json()["error"], "code": "not_found"}}


async def test_mem_t10_sharing_needs_a_bound_confirmation_and_is_audited(world):
    h, alice, bob, _, graph = world
    text = unique("The user wants the demo recorded")
    fact_id = (await remember(h, alice, text)).json()["fact_id"]
    other_id = (await remember(h, alice, unique("The user prefers another tea"))).json()["fact_id"]

    first = await h.client.patch(f"{MEM}/{fact_id}", json={"visibility": "graph"}, headers=alice.auth)
    assert first.status_code == 403
    token = first.json()["error"]["details"]["confirmation_token"]
    # Not shared yet: nothing happened on the unconfirmed call.
    assert text not in {f["content"] for f in await listed(h, bob)}

    # The token is bound to this exact action: not another fact, not a delete.
    wrong_fact = await h.client.patch(f"{MEM}/{other_id}", json={"visibility": "graph"},
                                      headers={**alice.auth, "X-Confirmation-Token": token})
    assert wrong_fact.status_code == 403 and wrong_fact.json()["error"]["code"] == "confirmation_required"

    first = await h.client.patch(f"{MEM}/{fact_id}", json={"visibility": "graph"}, headers=alice.auth)
    token = first.json()["error"]["details"]["confirmation_token"]
    ok = await h.client.patch(f"{MEM}/{fact_id}", json={"visibility": "graph"},
                              headers={**alice.auth, "X-Confirmation-Token": token})
    assert ok.status_code == 200 and ok.json()["visibility"] == "graph"
    replay = await h.client.patch(f"{MEM}/{fact_id}", json={"visibility": "private"},
                                  headers={**alice.auth, "X-Confirmation-Token": token})
    assert replay.status_code == 403  # single-use, and bound to "graph"

    shared_rows = await audit_rows(h, "resource.visibility.shared")
    assert [(r.resource, r.graph_id) for r in shared_rows] == [(f"mem0fact:{fact_id}", graph)]
    assert text in {f["content"] for f in await listed(h, bob)}

    unshare, _ = await confirmed(h, alice, "PATCH", f"{MEM}/{fact_id}", json={"visibility": "private"})
    assert unshare.status_code == 200
    assert [r.resource for r in await audit_rows(h, "resource.visibility.unshared")] == [f"mem0fact:{fact_id}"]
    assert text not in {f["content"] for f in await listed(h, bob)}


async def test_a_share_token_is_bound_to_the_exact_combined_change(world):
    """PATCH {content, visibility}: the confirmation covers the content too, so a
    token minted for one correction cannot carry a different one, and nothing is
    applied before the confirmed call."""

    h, alice, bob, *_ = world
    fact_id = (await remember(h, alice, "The user prefers the draft agenda")).json()["fact_id"]
    first = await h.client.patch(f"{MEM}/{fact_id}", json={"content": "The user prefers agenda A",
                                                          "visibility": "graph"}, headers=alice.auth)
    assert first.status_code == 403
    token = first.json()["error"]["details"]["confirmation_token"]
    assert [f["content"] for f in await listed(h, alice) if f["fact_id"] == fact_id] == ["The user prefers the draft agenda"]

    swapped = await h.client.patch(f"{MEM}/{fact_id}", json={"content": "The user prefers agenda B",
                                                            "visibility": "graph"},
                                   headers={**alice.auth, "X-Confirmation-Token": token})
    assert swapped.status_code == 403 and swapped.json()["error"]["code"] == "confirmation_required"
    assert [f["content"] for f in await listed(h, alice) if f["fact_id"] == fact_id] == ["The user prefers the draft agenda"]
    assert all(f["fact_id"] != fact_id for f in await listed(h, bob))


async def test_a_member_cannot_share_or_unshare_another_members_fact(world):
    h, alice, bob, *_ = world
    fact_id = (await remember(h, alice, unique("The user wants shared notes"))).json()["fact_id"]
    await share(h, alice, fact_id)
    resp = await h.client.patch(f"{MEM}/{fact_id}", json={"visibility": "private"}, headers=bob.auth)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "unauthorized"


async def test_owner_deletion_needs_confirmation_and_removes_the_fact(world):
    h, alice, bob, _, _ = world
    text = unique("The user wants this forgotten")
    fact_id = (await remember(h, alice, text)).json()["fact_id"]
    await share(h, alice, fact_id)

    by_member = await h.client.delete(f"{MEM}/{fact_id}", headers=bob.auth)
    assert by_member.status_code == 403
    gone, _ = await confirmed(h, alice, "DELETE", f"{MEM}/{fact_id}")
    assert gone.status_code == 204
    assert text not in {f["content"] for f in await listed(h, alice)}
    assert text not in {f["content"] for f in await listed(h, bob)}
    assert [r.resource for r in await audit_rows(h, "memory.deleted")] == [f"mem0fact:{fact_id}"]
    again = await h.client.delete(f"{MEM}/{fact_id}", headers=alice.auth)
    assert again.status_code == 404


# ── lifecycle through the facade (LIFE-003, MEM-T9, MP-T11) ────────────────


async def test_account_and_graph_deletion_hooks(world):
    from server.security.audit import AuditLogger

    h, alice, bob, _, graph = world
    a_private = (await remember(h, alice, unique("The user prefers private tea"))).json()["fact_id"]
    a_shared = (await remember(h, alice, unique("The user wants shared tea"))).json()["fact_id"]
    await share(h, alice, a_shared)
    b_private = (await remember(h, bob, unique("The user prefers bob tea"))).json()["fact_id"]

    facade = h.app.state.memory
    async with h.storage.session() as s:
        removed = await facade.delete_graph_shared(graph_id=graph, audit=AuditLogger(s, request_id=uuid.uuid4()))
        await s.commit()
    assert removed == 2
    alice_left = {f["fact_id"] for f in await listed(h, alice)}
    assert a_private in alice_left and a_shared not in alice_left
    assert b_private in {f["fact_id"] for f in await listed(h, bob)}

    async with h.storage.session() as s:
        await facade.delete_all_for_user(user_id=alice.user_id, audit=AuditLogger(s, request_id=uuid.uuid4()))
        await s.commit()
    assert await listed(h, alice) == []
    assert b_private in {f["fact_id"] for f in await listed(h, bob)}
    assert await audit_rows(h, "memory.graph.purged") and await audit_rows(h, "memory.user.purged")


# ── failure states (02 §13, FAIL-008, API-T1/T10) ──────────────────────────


async def test_api_t1_every_memory_endpoint_requires_a_token(world):
    h, *_ = world
    fid = uuid.uuid4()
    for method, url, kwargs in (("GET", MEM, {}), ("POST", MEM, {"json": {"fact_type": "preference", "content": "x"}}),
                                ("PATCH", f"{MEM}/{fid}", {"json": {"content": "x"}}), ("DELETE", f"{MEM}/{fid}", {}),
                                ("GET", f"{MEM}/status", {})):
        resp = await h.client.request(method, url, **kwargs)
        assert resp.status_code == 401, (method, url)


async def test_api_t10_a_down_store_is_503_mem0_on_every_endpoint(world, monkeypatch):
    h, alice, *_ = world
    fact_id = (await remember(h, alice, "The user prefers tea")).json()["fact_id"]
    provider = h.app.state.memory.provider

    def boom(*args, **kwargs):
        raise RuntimeError("store offline")

    monkeypatch.setattr(provider.mem0.vector_store.collection, "count", boom)
    monkeypatch.setattr(provider.mem0.vector_store, "list", boom)
    monkeypatch.setattr(provider.mem0.vector_store, "search", boom)
    for method, url, kwargs in (("GET", MEM, {}),
                                ("POST", MEM, {"json": {"fact_type": "preference", "content": "The user prefers x"}}),
                                ("PATCH", f"{MEM}/{fact_id}", {"json": {"content": "The user prefers y"}}),
                                ("DELETE", f"{MEM}/{fact_id}", {})):
        resp = await h.client.request(method, url, headers=alice.auth, **kwargs)
        assert resp.status_code == 503, (method, resp.text)
        err = resp.json()["error"]
        assert err["code"] == "dependency_unavailable" and err["details"]["dependency"] == "mem0"
        assert err["retryable"] is True
        assert "store offline" not in resp.text
    status = await h.client.get(f"{MEM}/status", headers=alice.auth)
    assert status.json() == {**status.json(), "enabled": True, "available": False}


async def test_without_a_memory_provider_the_endpoints_say_so(h):
    alice = await h.user("alice")
    resp = await h.client.get(MEM, headers=alice.auth)
    assert resp.status_code == 503 and resp.json()["error"]["details"]["dependency"] == "mem0"
    status = await h.client.get(f"{MEM}/status", headers=alice.auth)
    assert status.json() == {"enabled": False, "available": False}
