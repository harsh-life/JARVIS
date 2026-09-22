"""Runtime × memory (11 §2/§3, MEM-T1/T8, RAUTH-003, owner decisions §4/§5).

The chain under test: server-derived principal → visibility pushed into the store
query → re-check with the engine's `readable()` → relevance bound → model
context. `InMemoryMemoryStore` stands in for `11`'s Mem0 store; `LeakyMemoryStore`
is a broken one that ignores the filter, to prove the re-check holds anyway.
"""

from __future__ import annotations

import pytest

from server.memory.hydration import FAIL_008_NOTE
from shared.schemas.enums import Visibility
from tests.runtime.conftest import Fact, LeakyMemoryStore, final

pytestmark = pytest.mark.asyncio


def _context_of(model) -> str:
    return model.seen[-1][1].content


async def test_the_same_user_sees_the_same_memory_from_every_device(h):
    """Owner §4: same-user continuity. Memory is keyed on the user, so every one
    of the user's devices hydrates the same authorized state."""

    alice = await h.user("alice")
    h.memory.add(Fact("Alice prefers metric units", alice.user_id, Visibility.PRIVATE, None))
    second_device = await h.user("alice")
    assert second_device.device_id != alice.device_id

    h.model.push(final())
    await h.submit(alice, "convert the units please")
    first = _context_of(h.model)
    h.model.push(final())
    await h.submit(second_device, "convert the units please")
    second = _context_of(h.model)

    assert "Alice prefers metric units" in first
    assert "Alice prefers metric units" in second


async def test_mem_t1_another_member_never_receives_a_private_fact(h):
    """MEM-T1 (release-blocking): Bob, a member of Alice's shared graph, gets her
    graph-shared fact but never her private one — through hydration or at all."""

    alice, bob = await h.user("alice"), await h.user("bob")
    graph = await h.shared_graph(alice, bob)
    h.memory.add(Fact("Alice private: salary details", alice.user_id, Visibility.PRIVATE, graph))
    h.memory.add(Fact("Alice shared: project deadline friday", alice.user_id, Visibility.GRAPH, graph))

    h.model.push(final())
    await h.submit(bob, "what is the project deadline and salary")

    context = _context_of(h.model)
    assert "project deadline friday" in context
    assert "salary" not in context.split("USER REQUEST:")[0]
    assert h.memory.queries[-1]["owner"] == bob.user_id
    assert h.memory.queries[-1]["graphs"] == {graph}


async def test_a_store_that_ignores_the_filter_still_cannot_leak(make_harness):
    """Defense in depth for MEM-T1: even if the store returns everything, the
    hydrator's RAUTH-004 re-check drops what the principal may not read."""

    leaky = LeakyMemoryStore()
    h = await make_harness(memory=leaky)
    alice, bob = await h.user("alice"), await h.user("bob")
    graph = await h.shared_graph(alice, bob)
    leaky.add(Fact("ALICE-PRIVATE-MARKER", alice.user_id, Visibility.PRIVATE, graph))
    leaky.add(Fact("ALICE-OTHER-PRIVATE", alice.user_id, Visibility.PRIVATE, None))
    leaky.add(Fact("bob's own note", bob.user_id, Visibility.PRIVATE, None))

    h.model.push(final())
    await h.submit(bob, "anything")

    everything = h.model.all_text()
    assert "ALICE-PRIVATE-MARKER" not in everything
    assert "ALICE-OTHER-PRIVATE" not in everything
    assert "bob's own note" in everything


async def test_hydration_is_relevance_bounded_not_a_history_dump(make_harness):
    """MEM-002 / MEM-T8: top-K by relevance, never the whole history."""

    h = await make_harness(config={"agent": {"bounds": {"memory_top_k": 2}}})
    alice = await h.user("alice")
    for i in range(10):
        h.memory.add(Fact(f"unrelated trivia number {i}", alice.user_id, Visibility.PRIVATE, None))
    h.memory.add(Fact("the garage door code is managed by the landlord", alice.user_id, Visibility.PRIVATE, None))

    h.model.push(final())
    await h.submit(alice, "who manages the garage door")

    context = _context_of(h.model).split("USER REQUEST:")[0]
    assert context.count("\n- ") <= 2
    assert "garage door" in context
    assert h.memory.queries[-1]["limit"] == 2


async def test_graph_shared_context_does_not_cross_graphs(h):
    """Minimization (`[PROPOSED]`, OD-MEM-1): a task in graph X is hydrated with
    graph-visible facts from X only, never another graph's shared context."""

    alice, bob = await h.user("alice"), await h.user("bob")
    x = await h.shared_graph(alice, bob)
    y = await h.shared_graph(alice, bob)
    await h.enter_graph(bob, x)
    h.memory.add(Fact("from graph Y only", alice.user_id, Visibility.GRAPH, y))
    h.memory.add(Fact("from graph X", alice.user_id, Visibility.GRAPH, x))

    h.model.push(final())
    await h.submit(bob, "graph")

    context = _context_of(h.model)
    assert "from graph X" in context
    assert "from graph Y only" not in context


async def test_leaving_a_graph_ends_access_to_its_shared_memory(h):
    """GRAPH-006: membership is re-read live — a stale active graph on the
    session does not keep the shared context flowing."""

    alice, bob = await h.user("alice"), await h.user("bob")
    graph = await h.shared_graph(alice, bob)
    h.memory.add(Fact("shared plan alpha", alice.user_id, Visibility.GRAPH, graph))
    removed = await h.client.delete(f"/api/v1/graphs/{graph}/members/{bob.user_id}", headers=alice.auth)
    assert removed.status_code == 204, removed.text

    h.model.push(final())
    await h.submit(bob, "plan")

    assert "shared plan alpha" not in h.model.all_text()


async def test_the_model_cannot_browse_memory(h):
    """Owner §5: the model receives hydrated items, never a store handle, a
    listing, or a tool to page through memory."""

    alice = await h.user("alice")
    tools = (await h.client.get("/api/v1/config/tools", headers=alice.auth)).json()["items"]
    assert not [t for t in tools if "memory" in t["tool_id"] or t["required_capability"].startswith("memory")]


async def test_fail_008_without_a_store_the_task_degrades_explicitly(make_harness):
    """FAIL-008 / P3: with no Mem0 store configured (this branch's production
    state) the task proceeds on session context and says so."""

    h = await make_harness(memory=None)
    alice = await h.user("alice")
    h.model.push(final("answered"))

    resp = await h.submit(alice)

    assert resp.status_code == 200
    assert FAIL_008_NOTE in resp.json()["notes"]
    assert FAIL_008_NOTE in _context_of(h.model)
