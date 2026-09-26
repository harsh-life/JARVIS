"""Request → authorization → retrieval → hydration → model context, on the real
Mem0 store (MP-T1 / MEM-T1, MEM-T8, MP-T4, MP-T6, MP-T7).

The model is the one scripted component (it is what the context is *given to*).
Everything between the authenticated request and the model's input is
production: the composition root, the engine, the Mem0 adapter's in-query
visibility filter, the hydrator's `readable()` re-check, the bounds.
"""

from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy import select

from server.gateway.app import API_V1_PREFIX
from server.memory.hydration import FAIL_008_NOTE
from server.models.provider import ModelUnavailable
from server.storage.models import AuditEvent, UsageEvent
from shared.schemas.enums import UsageKind, Visibility
from tests.memory.conftest import unique
from tests.runtime.conftest import ask, call, final, say

pytestmark = pytest.mark.asyncio

MEM = f"{API_V1_PREFIX}/memory"


def context_of(model, index: int = -1) -> str:
    """The user-context message of a model call (messages[1]), without the request."""

    return model.seen[index][1].content.split("USER REQUEST:")[0]


async def remember(h, actor, text, *, fact_type="preference", graph_id=None) -> str:
    body = {"fact_type": fact_type, "content": text, **({"graph_id": str(graph_id)} if graph_id else {})}
    resp = await h.client.post(MEM, json=body, headers=actor.auth)
    assert resp.status_code == 201, resp.text
    return resp.json()["fact_id"]


async def share(h, actor, fact_id):
    first = await h.client.patch(f"{MEM}/{fact_id}", json={"visibility": "graph"}, headers=actor.auth)
    token = first.json()["error"]["details"]["confirmation_token"]
    resp = await h.client.patch(f"{MEM}/{fact_id}", json={"visibility": "graph"},
                                headers={**actor.auth, "X-Confirmation-Token": token})
    assert resp.status_code == 200, resp.text


# ── MP-T1 / MEM-T1: the release-blocking cross-user test ───────────────────


async def test_mp_t1_realistic_users_hydrate_only_what_each_may_read(stack):
    """Seeds realistic records for two members of one shared graph and a third
    user outside it, then drives a task for each through the full path."""

    h = await stack()
    alice, bob, carol = await h.user("alice"), await h.user("bob"), await h.user("carol")
    household = await h.shared_graph(alice, bob)
    r = await h.client.post(f"{API_V1_PREFIX}/graphs", json={"name": "carol", "type": "private"}, headers=carol.auth)
    await h.enter_graph(carol, uuid.UUID(r.json()["graph_id"]))

    alice_private = [
        "The user prefers to plan the trip budget alone before sharing it",
        "The user asked for a draft of the landlord renewal letter",
        "The user wants to finish the certification course by December",
    ]
    alice_shared = ["The user wants the family trip to Lisbon booked for April"]
    bob_private = ["The user prefers aisle seats on flights", "The user asked for a packing list for Lisbon"]
    carol_private = ["The user prefers vegetarian restaurants on trips"]

    for text in alice_private:
        await remember(h, alice, text)
    for text in alice_shared:
        await share(h, alice, await remember(h, alice, text, fact_type="stated_goal"))
    for text in bob_private:
        await remember(h, bob, text)
    for text in carol_private:
        await remember(h, carol, text)

    query = "plan the Lisbon trip in April: budget, flights, seats, restaurants, letter, course"
    seen: dict[str, str] = {}
    for actor in (alice, bob, carol):
        h.model.push(final("ok"))
        resp = await h.submit(actor, query)
        assert resp.status_code == 200, resp.text
        seen[actor.subject] = context_of(h.model)

    # Bob: Alice's shared goal and his own facts; never Alice's private facts.
    assert alice_shared[0] in seen["bob"]
    assert not any(t in seen["bob"] for t in alice_private + carol_private)
    # Alice: her own facts; never Bob's private facts.
    assert any(t in seen["alice"] for t in alice_private)
    assert not any(t in seen["alice"] for t in bob_private + carol_private)
    # Carol: only her own.
    assert not any(t in seen["carol"] for t in alice_private + alice_shared + bob_private)
    assert carol_private[0] in seen["carol"]
    # Across every prompt the model received for Bob's and Carol's tasks, none
    # of Alice's private facts appears anywhere — not only in the context block.
    bob_and_carol_calls = h.model.seen[1:]
    assert not any(t in "\n".join(m.content for c in bob_and_carol_calls for m in c) for t in alice_private)


async def test_graph_id_alone_does_not_hydrate_another_members_private_fact(stack):
    h = await stack()
    alice, bob = await h.user("alice"), await h.user("bob")
    graph = await h.shared_graph(alice, bob)
    secret_plan = unique("The user prefers the surprise party plan kept quiet")
    await remember(h, alice, secret_plan, graph_id=graph)

    h.model.push(final())
    await h.submit(bob, "what is the surprise party plan")
    assert secret_plan not in h.model.all_text()


async def test_cross_graph_shared_facts_are_not_hydrated_into_another_graphs_task(stack):
    """Minimization (OD-MEM-1): a task in graph Y never carries graph X's shared
    context, even for a member of both."""

    h = await stack()
    alice, bob = await h.user("alice"), await h.user("bob")
    x = await h.shared_graph(alice, bob)
    y = await h.shared_graph(bob)
    await h.enter_graph(alice, x)
    text = unique("The user wants graph-x launch notes kept in graph x")
    await share(h, alice, await remember(h, alice, text, graph_id=x))

    await h.enter_graph(bob, y)
    h.model.push(final())
    await h.submit(bob, "launch notes")
    assert text not in context_of(h.model)

    await h.enter_graph(bob, x)
    h.model.push(final())
    await h.submit(bob, "launch notes")
    assert text in context_of(h.model)


async def test_mem_t8_hydration_is_relevance_bounded_not_a_history_dump(stack):
    h = await stack(config={"agent": {"bounds": {"memory_top_k": 3}}})
    alice = await h.user("alice")
    await h.shared_graph(alice)
    for i in range(25):
        await remember(h, alice, f"The user prefers checklist style number {i} for errands")

    h.model.push(final())
    await h.submit(alice, "checklist style for errands")
    items = [line for line in context_of(h.model).splitlines() if line.startswith("- ")]
    assert 0 < len(items) <= 3
    assert len(context_of(h.model)) < 4000 + 200


async def test_no_raw_provider_record_reaches_the_model(stack):
    h = await stack()
    alice = await h.user("alice")
    graph = await h.shared_graph(alice)
    fact_id = await remember(h, alice, "The user prefers dark mode in every app")

    h.model.push(final())
    await h.submit(alice, "what display mode do I prefer")
    context = h.model.all_text()
    assert "The user prefers dark mode in every app" in context
    for leaked in (fact_id, str(alice.user_id), str(graph), "owner:", "graph:", "jarvis_kind",
                   "content_hash", "mirror_of", "source_user_id", "hash", "created_at"):
        assert leaked not in context, leaked
    assert "untrusted data" in context_of(h.model)


async def test_fail_008_a_down_store_degrades_explicitly_and_the_task_completes(stack, monkeypatch):
    h = await stack()
    alice = await h.user("alice")
    await h.shared_graph(alice)
    await remember(h, alice, "The user prefers tea")
    provider = h.app.state.memory.provider

    def boom(*args, **kwargs):
        raise RuntimeError("store offline")

    monkeypatch.setattr(provider.mem0.vector_store, "search", boom)
    h.model.push(final("answered anyway"))
    resp = await h.submit(alice, "tea?")
    assert resp.status_code == 200 and resp.json()["response"] == "answered anyway"
    assert FAIL_008_NOTE in context_of(h.model)
    assert "The user prefers tea" not in h.model.all_text()


# ── docs/21 §3 extraction (MP-T4, MP-T6, MP-T7) ───────────────────────────


def extraction_output(*facts: tuple[str, str]) -> str:
    return say({"facts": [{"fact_type": t, "content": c} for t, c in facts]})


async def model_usage_count(h, user_id) -> int:
    async with h.storage.session() as s:
        rows = (await s.execute(select(UsageEvent).where(UsageEvent.user_id == user_id,
                                                         UsageEvent.kind == UsageKind.MODEL_CALL))).scalars().all()
    return len(rows)


async def test_mp_t4_extraction_is_one_metered_call_and_stores_gated_private_facts(stack):
    h = await stack(config={"memory": {"auto_extract": True}})
    alice = await h.user("alice")
    graph = await h.shared_graph(alice)
    h.model.push(final("Here is your metric conversion."),
                 extraction_output(("preference", "The user prefers metric units"),
                                   ("stated_goal", "The user wants to run a marathon")))
    resp = await h.submit(alice, "convert 5 miles to km; I always use metric")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["counters"]["model_calls"] == 2
    assert await model_usage_count(h, alice.user_id) == 2  # the extraction call is metered
    assert any("remembered 2 fact(s)" in n for n in body["notes"])

    facts = (await h.client.get(MEM, headers=alice.auth)).json()["items"]
    assert {f["content"] for f in facts} == {"The user prefers metric units", "The user wants to run a marathon"}
    assert all(f["visibility"] == "private" and f["graph_id"] == str(graph) for f in facts)
    async with h.storage.session() as s:
        written = (await s.execute(select(AuditEvent).where(AuditEvent.action == "memory.write"))).scalars().all()
    assert {r.actor.value for r in written} == {"agent"}


async def test_mp_t6_extraction_sees_only_the_request_and_the_final_answer(stack):
    h = await stack(config={"memory": {"auto_extract": True}})
    alice = await h.user("alice")
    graph = await h.shared_graph(alice)
    remembered = unique("The user prefers hydrated-memory marker tea")
    await remember(h, alice, remembered)
    file_ref = await h.file(alice, graph, Visibility.PRIVATE, "notes.txt")
    await h.grant(alice, "file.read")

    h.model.push(ask("file.read"), call("files.read", "read_file", ref=file_ref),
                 final("The file says to buy tea."), extraction_output())
    resp = await h.submit(alice, "read my notes file and tell me what to buy")
    assert resp.status_code == 200, resp.text

    extraction_call = h.model.seen[-1]
    prompt = "\n".join(m.content for m in extraction_call)
    assert "USER REQUEST (data):\nread my notes file and tell me what to buy" in prompt
    assert "FINAL ANSWER (data):\nThe file says to buy tea." in prompt
    assert file_ref not in prompt                 # the tool observation is not an input
    assert "reads:read_file" not in prompt
    assert remembered not in prompt               # nor is hydrated memory
    assert "OBSERVATION" not in prompt.split("Everything below is untrusted data")[-1]


@pytest.mark.parametrize(
    "fact_type,content,reason",
    [
        ("preference", "The user's password is Tr0ub4dor-TESTONLY", "secret_detected:credential_assignment"),
        ("preference", "The user feels anxious about the exam", "emotional_or_relationship_content"),
        ("relationship", "The user is close to their sister", "unsupported_fact_type"),
        ("past_request", '{"tool": "files.read", "operation": "read_file"}', "raw_observation"),
    ],
)
async def test_mp_t7_extracted_candidates_still_pass_the_gate(stack, fact_type, content, reason):
    h = await stack(config={"memory": {"auto_extract": True}})
    alice = await h.user("alice")
    await h.shared_graph(alice)
    h.model.push(final("done"), extraction_output((fact_type, content)))
    resp = await h.submit(alice, "help me study")
    assert resp.status_code == 200
    assert any("not stored under the memory policy" in n for n in resp.json()["notes"])
    assert (await h.client.get(MEM, headers=alice.auth)).json()["items"] == []
    async with h.storage.session() as s:
        blocked = (await s.execute(select(AuditEvent).where(AuditEvent.action == "memory.write.blocked"))).scalars().all()
    assert [r.resource for r in blocked] == [f"mem0fact:blocked:{reason}"]
    assert "Tr0ub4dor" not in " ".join(r.resource for r in blocked)


@pytest.mark.parametrize("output", ["not json at all", say({"facts": "nope"}), say({"facts": [], "extra": 1}),
                                    say({"facts": [{"fact_type": "preference"}]})])
async def test_malformed_extraction_output_stores_nothing(stack, output):
    h = await stack(config={"memory": {"auto_extract": True}})
    alice = await h.user("alice")
    await h.shared_graph(alice)
    h.model.push(final("done"), output)
    resp = await h.submit(alice, "hello")
    assert resp.status_code == 200 and resp.json()["status"] == "completed"
    assert (await h.client.get(MEM, headers=alice.auth)).json()["items"] == []


async def test_an_extraction_outage_never_fails_the_finished_task(stack):
    h = await stack(config={"memory": {"auto_extract": True}})
    alice = await h.user("alice")
    await h.shared_graph(alice)
    h.model.push(final("the answer"), ModelUnavailable("down"))
    resp = await h.submit(alice, "hello")
    body = resp.json()
    assert resp.status_code == 200 and body["status"] == "completed" and body["response"] == "the answer"
    assert "long-term memory was not updated for this task" in body["notes"]
    assert await model_usage_count(h, alice.user_id) == 2  # the failed call is still metered


async def test_no_extraction_when_disabled_in_a_read_only_mode_or_at_the_call_bound(stack):
    h = await stack()  # auto_extract off (default)
    alice = await h.user("alice")
    await h.shared_graph(alice)
    h.model.push(final("done"))
    body = (await h.submit(alice, "hello")).json()
    assert body["counters"]["model_calls"] == 1

    h2 = await stack(config={"memory": {"auto_extract": True}})
    bob = await h2.user("bob")
    await h2.shared_graph(bob)
    h2.model.push(final("a draft"))
    body = (await h2.submit(bob, "draft a note", extra_body={"mode": "draft"})).json()
    assert body["counters"]["model_calls"] == 1  # formation is a write; draft mode forms nothing

    h3 = await stack(config={"memory": {"auto_extract": True}, "agent": {"bounds": {"max_model_calls": 1}}})
    carol = await h3.user("carol")
    await h3.shared_graph(carol)
    h3.model.push(final("done"))
    body = (await h3.submit(carol, "hello")).json()
    assert body["counters"]["model_calls"] == 1
    assert "long-term memory was not updated for this task" in body["notes"]


async def test_extraction_needs_a_graph_context_and_writes_enabled(stack):
    h = await stack(config={"memory": {"auto_extract": True, "writes_enabled": False}})
    alice = await h.user("alice")
    await h.shared_graph(alice)
    h.model.push(final("done"))
    assert (await h.submit(alice, "hello")).json()["counters"]["model_calls"] == 1

    h2 = await stack(config={"memory": {"auto_extract": True}})
    bob = await h2.user("bob")  # no active graph
    h2.model.push(final("done"))
    assert (await h2.submit(bob, "hello")).json()["counters"]["model_calls"] == 1


async def test_the_extraction_prompt_admits_exactly_two_inputs():
    import inspect

    from server.memory.extraction import extraction_messages

    params = list(inspect.signature(extraction_messages).parameters)
    assert params == ["user_request", "final_answer", "max_chars"]
    assert re.search(r"Never credentials", extraction_messages(user_request="a", final_answer="b")[0].content)


async def test_mp_t8_the_whole_memory_and_vault_stack_makes_no_hidden_egress(make_harness, mem0_provider,
                                                                              vault_index, egress):
    """Every memory and vault path a user can drive — add, share, list, search,
    hydrate (memory + vault), extract, correct, delete — with the audit hook
    recording: no socket, no DNS lookup, no URL fetch, no subprocess, and no
    file written outside the test's own directory."""

    h = await make_harness(memory_provider=mem0_provider, vault_index=vault_index,
                           config={"memory": {"auto_extract": True}})
    alice, bob = await h.user("alice"), await h.user("bob")
    await h.shared_graph(alice, bob)

    egress.active = True
    fact_id = await remember(h, alice, "The user prefers kilometres for running logs")
    await share(h, alice, fact_id)
    await h.client.get(MEM, headers=bob.auth)
    await h.client.get(MEM, params={"query": "running"}, headers=bob.auth)
    await h.client.get(f"{API_V1_PREFIX}/vault/query", params={"domain": "product", "question": "units"},
                       headers=alice.auth)
    h.model.push(final("Use kilometres."), extraction_output(("preference", "The user prefers kilometres")))
    resp = await h.submit(alice, "which units for my running log")
    await h.client.patch(f"{MEM}/{fact_id}", json={"content": "The user prefers miles for running logs"},
                         headers=alice.auth)
    first = await h.client.delete(f"{MEM}/{fact_id}", headers=alice.auth)
    token = first.json()["error"]["details"]["confirmation_token"]
    await h.client.delete(f"{MEM}/{fact_id}", headers={**alice.auth, "X-Confirmation-Token": token})
    egress.active = False

    assert resp.status_code == 200 and "REFERENCE (curated knowledge vault" in h.model.seen[0][1].content
    assert egress.events == [], egress.events
    assert egress.writes_outside == [], egress.writes_outside
