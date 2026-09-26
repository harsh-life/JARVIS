"""Mutation tests for the memory authorization guards.

A security test that still passes when the guard it protects is removed proves
nothing. For each critical guard, this module (1) runs a detector scenario
against the real stack and requires it to report "safe", then (2) applies one
mutant — the guard disabled — and requires the *same* detector to report the
breach. A mutant the detector did not catch fails the test.

Two mutants are expected to survive on their own, and the tests say so
explicitly: they are guarded twice (defence in depth), and are killed only when
both guards are removed together.
"""

from __future__ import annotations

import uuid
from typing import Awaitable, Callable

import pytest

from server.gateway.app import API_V1_PREFIX
from server.memory import mem0_provider as adapter
from shared.schemas.enums import FactType, Visibility
from shared.schemas.memory import Mem0Fact
from tests.memory.conftest import unique
from tests.runtime.conftest import final

MEM = f"{API_V1_PREFIX}/memory"


async def _remember(h, actor, text, **body):
    resp = await h.client.post(MEM, json={"fact_type": "preference", "content": text, **body}, headers=actor.auth)
    assert resp.status_code == 201, resp.text
    return resp.json()["fact_id"]


async def _share(h, actor, fact_id):
    first = await h.client.patch(f"{MEM}/{fact_id}", json={"visibility": "graph"}, headers=actor.auth)
    token = first.json()["error"]["details"]["confirmation_token"]
    ok = await h.client.patch(f"{MEM}/{fact_id}", json={"visibility": "graph"},
                              headers={**actor.auth, "X-Confirmation-Token": token})
    assert ok.status_code == 200, ok.text


def leaky(provider):
    """A store bug: search/list ignore the owner and graph filters entirely."""

    real_rows = provider._rows

    async def search(*, query, owner_user_id, readable_graph_ids, limit):
        rows = provider.mem0.vector_store.list(filters={"jarvis_kind": "fact"}, top_k=1000)[0]
        facts = [adapter._fact_from(r.id, r.payload.get("data"), r.payload) for r in rows]
        return [adapter._candidate(f, 1.0) for f in facts if f is not None][:limit]

    async def list_facts(*, owner_user_id, readable_graph_ids, limit):
        rows = real_rows({"jarvis_kind": "fact"})
        return [f for f in (adapter._fact_from(r.id, r.payload.get("data"), r.payload) for r in rows) if f][:limit]

    return search, list_facts


# ── detectors: each returns True when the system is SAFE ───────────────────


async def hydration_is_safe(stack, monkeypatch, *, leaky_store: bool) -> bool:
    h = await stack()
    alice, bob = await h.user("alice"), await h.user("bob")
    await h.shared_graph(alice, bob)
    private = unique("The user prefers the private budget plan")
    await _remember(h, alice, private)
    if leaky_store:
        search, _ = leaky(h.app.state.memory.provider)
        monkeypatch.setattr(h.app.state.memory.provider, "search", search)
    h.model.push(final())
    await h.submit(bob, "budget plan")
    return private not in h.model.all_text()


async def listing_is_safe(stack, monkeypatch, *, leaky_store: bool) -> bool:
    h = await stack()
    alice, bob = await h.user("alice"), await h.user("bob")
    await h.shared_graph(alice, bob)
    private = unique("The user prefers the private listing")
    await _remember(h, alice, private)
    if leaky_store:
        _, list_facts = leaky(h.app.state.memory.provider)
        monkeypatch.setattr(h.app.state.memory.provider, "list_facts", list_facts)
    items = (await h.client.get(MEM, headers=bob.auth)).json()["items"]
    return all(item["content"] != private for item in items)


async def owner_only_is_safe(stack) -> bool:
    h = await stack()
    alice, bob = await h.user("alice"), await h.user("bob")
    await h.shared_graph(alice, bob)
    fact_id = await _remember(h, alice, "The user prefers the shared board")
    await _share(h, alice, fact_id)
    resp = await h.client.patch(f"{MEM}/{fact_id}", json={"content": "The user prefers vandalism"}, headers=bob.auth)
    return resp.status_code in (403, 404)


async def private_delete_is_safe(stack) -> bool:
    h = await stack()
    alice, bob = await h.user("alice"), await h.user("bob")
    await h.shared_graph(alice, bob)
    fact_id = await _remember(h, alice, unique("The user prefers private tea"))
    first = await h.client.delete(f"{MEM}/{fact_id}", headers=bob.auth)
    if first.status_code == 403 and first.json()["error"]["code"] == "confirmation_required":
        token = first.json()["error"]["details"]["confirmation_token"]
        first = await h.client.delete(f"{MEM}/{fact_id}", headers={**bob.auth, "X-Confirmation-Token": token})
    return first.status_code == 404


async def gate_is_safe(stack, content: str) -> bool:
    h = await stack()
    alice = await h.user("alice")
    await h.shared_graph(alice)
    resp = await h.client.post(MEM, json={"fact_type": "preference", "content": content}, headers=alice.auth)
    return resp.status_code == 422


async def unshare_is_safe(mem0_provider) -> bool:
    a, b, g = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    text = unique("The user prefers unshared tea")
    fact_id = await mem0_provider.add(Mem0Fact(owner_user_id=a, source_user_id=a, graph_id=g,
                                               fact_type=FactType.PREFERENCE, content=text))
    await mem0_provider.set_visibility(fact_id, Visibility.GRAPH)
    await mem0_provider.set_visibility(fact_id, Visibility.PRIVATE)
    found = await mem0_provider.search(query="unshared tea", owner_user_id=b, readable_graph_ids=frozenset({g}),
                                       limit=10)
    return all(c.content != text for c in found)


async def killed(detector: Callable[[], Awaitable[bool]], apply_mutant: Callable[[], None]) -> bool:
    assert await detector(), "detector must report the unmutated system as safe"
    apply_mutant()
    return not await detector()


# ── mutants ────────────────────────────────────────────────────────────────


async def test_hydration_recheck_mutant_is_killed(stack, monkeypatch):
    import server.memory.hydration as hydration

    assert await hydration_is_safe(stack, monkeypatch, leaky_store=True)  # re-check alone holds a leaky store
    monkeypatch.setattr(hydration, "readable", lambda **kwargs: True)
    assert not await hydration_is_safe(stack, monkeypatch, leaky_store=True)


async def test_provider_query_filter_mutant_survives_alone_and_dies_with_the_recheck(stack, monkeypatch):
    """Defence in depth: a store that ignores the visibility filter is caught by
    the hydrator's re-check; only removing both leaks."""

    import server.memory.hydration as hydration

    assert await hydration_is_safe(stack, monkeypatch, leaky_store=False)
    assert await hydration_is_safe(stack, monkeypatch, leaky_store=True)  # mutant survives alone …
    monkeypatch.setattr(hydration, "readable", lambda **kwargs: True)
    assert not await hydration_is_safe(stack, monkeypatch, leaky_store=True)  # … and dies with the re-check


async def test_listing_recheck_mutant_is_killed(stack, monkeypatch):
    from server.composition.memory import MemoryFacade

    assert await listing_is_safe(stack, monkeypatch, leaky_store=True)
    monkeypatch.setattr(MemoryFacade, "_visible", lambda self, principal, fact, member_of: True)
    assert not await listing_is_safe(stack, monkeypatch, leaky_store=True)


async def test_engine_owner_only_mutant_is_killed(stack, monkeypatch):
    import server.graph.authorization as authorization

    assert await killed(lambda: owner_only_is_safe(stack),
                        lambda: monkeypatch.setattr(authorization, "_OWNER_ONLY_OPERATIONS", frozenset()))


async def test_engine_visibility_mutant_is_killed(stack, monkeypatch):
    import server.graph.authorization as authorization

    assert await killed(lambda: private_delete_is_safe(stack),
                        lambda: monkeypatch.setattr(authorization, "readable", lambda **kwargs: True))


async def test_resource_loader_projection_mutant_is_killed(stack, monkeypatch):
    """The engine decides on the loader's projection; a loader that reported the
    caller as owner would hand a stranger the fact."""

    from server.composition import memory as composition_memory

    real = composition_memory.MemoryFactLoader.load

    async def lying_load(self, session, resource_type, resource_ref):
        descriptor = await real(self, session, resource_type, resource_ref)
        if descriptor is None:
            return None
        from dataclasses import replace

        from shared.schemas.enums import Visibility as V

        return replace(descriptor, visibility=V.GRAPH)

    assert await killed(lambda: private_delete_is_safe(stack),
                        lambda: monkeypatch.setattr(composition_memory.MemoryFactLoader, "load", lying_load))


@pytest.mark.parametrize("content,target,replacement", [
    ("The user's key is " + "sk-proj-" + "TESTONLY" + "m" * 20, "find_secret", lambda text: None),
    ("The user feels lonely", "_EMOTIONAL_RE", None),
])
async def test_write_gate_mutants_are_killed(stack, monkeypatch, content, target, replacement):
    import re

    import server.memory.gate as gate

    def mutate():
        if target == "_EMOTIONAL_RE":
            monkeypatch.setattr(gate, "_EMOTIONAL_RE", re.compile(r"(?!x)x"))
        else:
            monkeypatch.setattr(gate, target, replacement)

    assert await killed(lambda: gate_is_safe(stack, content), mutate)


async def test_unshare_mutants(mem0_provider, monkeypatch):
    """Un-share is guarded twice: mirrors are deleted, and search re-validates
    every mirror against its canonical record. Removing one survives; removing
    both leaks."""

    assert await unshare_is_safe(mem0_provider)
    monkeypatch.setattr(adapter.Mem0MemoryProvider, "_delete_rows", lambda self, rows: 0)
    assert await unshare_is_safe(mem0_provider)  # survives: the canonical record decides

    real_canonical = adapter.Mem0MemoryProvider._canonical

    def trusting_canonical(self, fact_id):
        fact = real_canonical(self, fact_id)
        return fact.model_copy(update={"visibility": Visibility.GRAPH}) if fact else None

    monkeypatch.setattr(adapter.Mem0MemoryProvider, "_canonical", trusting_canonical)
    assert not await unshare_is_safe(mem0_provider)
