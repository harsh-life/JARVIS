"""The Mem0 adapter against a real, self-hosted Mem0 + Chroma store (docs/21 §1–§2).

No mocks on the storage path: every test here drives `build_mem0_provider` — the
production adapter — over a real Chroma database in `tmp_path` with the real
offline bge-small embedder. The provider's in-query visibility filter is tested
on its own here (it is defence in depth); the engine re-check on top of it is
tested in `test_memory_api.py` and `test_memory_hydration_real.py`.
"""

from __future__ import annotations

import inspect
import logging
import uuid
from pathlib import Path

import pytest

from server.memory.mem0_provider import KIND_MIRROR, Mem0MemoryProvider, graph_scope, owner_scope
from server.memory.provider import MemoryProvider, MemoryProviderUnavailable
from shared.schemas.enums import FactType, Visibility
from shared.schemas.memory import Mem0Fact
from tests.memory.conftest import unique

pytestmark = pytest.mark.asyncio

A, B, C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
G, H = uuid.uuid4(), uuid.uuid4()


def fact(owner: uuid.UUID, text: str, *, graph: uuid.UUID = G, fact_type: FactType = FactType.PREFERENCE,
         visibility: Visibility = Visibility.PRIVATE) -> Mem0Fact:
    return Mem0Fact(owner_user_id=owner, source_user_id=owner, graph_id=graph, fact_type=fact_type,
                    content=text, visibility=visibility)


async def contents(provider, *, owner, graphs=frozenset(), query="coffee tea project units"):
    found = await provider.search(query=query, owner_user_id=owner, readable_graph_ids=frozenset(graphs), limit=20)
    return {c.content for c in found}


def mirrors(provider: Mem0MemoryProvider, fact_id) -> list:
    return provider.mem0.vector_store.list(filters={"jarvis_kind": KIND_MIRROR, "mirror_of": str(fact_id)},
                                           top_k=100)[0]


# ── the contract ───────────────────────────────────────────────────────────


async def test_the_adapter_implements_every_memory_provider_method(mem0_provider):
    for name, member in inspect.getmembers(MemoryProvider, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        implemented = getattr(mem0_provider, name, None)
        assert implemented is not None, name
        assert inspect.iscoroutinefunction(implemented), name
        assert list(inspect.signature(implemented).parameters) == [
            p for p in inspect.signature(member).parameters if p != "self"
        ], name


async def test_add_then_get_round_trips_a_typed_private_fact(mem0_provider):
    text = unique("The user prefers oat milk")
    fact_id = await mem0_provider.add(fact(A, text, fact_type=FactType.STATED_GOAL))
    got = await mem0_provider.get(fact_id)
    assert got is not None
    assert (got.fact_id, got.owner_user_id, got.source_user_id, got.graph_id) == (fact_id, A, A, G)
    assert (got.fact_type, got.content, got.visibility) == (FactType.STATED_GOAL, text, Visibility.PRIVATE)
    assert await mem0_provider.get(uuid.uuid4()) is None


async def test_mem_t2_a_new_fact_is_stored_private_whatever_the_caller_passed(mem0_provider):
    fact_id = await mem0_provider.add(fact(A, unique("The user prefers tea"), visibility=Visibility.GRAPH))
    assert (await mem0_provider.get(fact_id)).visibility is Visibility.PRIVATE
    assert mirrors(mem0_provider, fact_id) == []
    assert await contents(mem0_provider, owner=B, graphs={G}) == set()


async def test_an_exact_duplicate_within_one_owner_is_the_same_fact(mem0_provider):
    first = await mem0_provider.add(fact(A, "The user prefers  dark roast coffee"))
    again = await mem0_provider.add(fact(A, "the user prefers dark roast coffee "))
    assert first == again
    other_type = await mem0_provider.add(fact(A, "the user prefers dark roast coffee", fact_type=FactType.STATED_GOAL))
    assert other_type != first


async def test_mp_t3_identical_text_from_two_owners_is_never_merged(mem0_provider):
    text = "The user prefers green tea"
    a_id = await mem0_provider.add(fact(A, text))
    b_id = await mem0_provider.add(fact(B, text))
    assert a_id != b_id
    assert (await mem0_provider.get(a_id)).owner_user_id == A
    assert (await mem0_provider.get(b_id)).owner_user_id == B
    await mem0_provider.update_content(a_id, "The user prefers jasmine tea")
    assert (await mem0_provider.get(b_id)).content == text


# ── visibility inside the query (11 §2) ────────────────────────────────────


async def test_private_memory_is_visible_to_its_owner_only(mem0_provider):
    text = unique("The user prefers metric units")
    await mem0_provider.add(fact(A, text))
    assert text in await contents(mem0_provider, owner=A, graphs={G})
    assert text not in await contents(mem0_provider, owner=B, graphs={G})
    assert text not in await contents(mem0_provider, owner=C)


async def test_graph_id_alone_never_authorizes_a_read(mem0_provider):
    """RAUTH-003/V1: B is a reader of G, and A's fact is *scoped* to G, but it is
    private — graph scoping is not permission."""

    text = unique("The user prefers the project tracker")
    await mem0_provider.add(fact(A, text, graph=G))
    assert text not in await contents(mem0_provider, owner=B, graphs={G, H})
    assert all(f.content != text for f in await mem0_provider.list_facts(
        owner_user_id=B, readable_graph_ids=frozenset({G, H}), limit=50))


async def test_graph_shared_memory_reaches_readers_of_that_graph_only(mem0_provider):
    text = unique("The user wants the project shipped on Friday")
    fact_id = await mem0_provider.add(fact(A, text, graph=G, fact_type=FactType.STATED_GOAL))
    await mem0_provider.set_visibility(fact_id, Visibility.GRAPH)

    assert text in await contents(mem0_provider, owner=B, graphs={G})
    # cross-graph: a reader of H only never sees G's shared facts
    assert text not in await contents(mem0_provider, owner=C, graphs={H})
    # and a user with no readable graph sees nothing of it
    assert text not in await contents(mem0_provider, owner=C)
    listed = await mem0_provider.list_facts(owner_user_id=B, readable_graph_ids=frozenset({G}), limit=50)
    assert [f.content for f in listed if f.content == text] == [text]


async def test_mp_t2_a_mirror_exists_iff_the_fact_is_graph_visible(mem0_provider):
    fact_id = await mem0_provider.add(fact(A, unique("The user prefers tea in the project room")))
    assert mirrors(mem0_provider, fact_id) == []
    await mem0_provider.set_visibility(fact_id, Visibility.GRAPH)
    rows = mirrors(mem0_provider, fact_id)
    assert len(rows) == 1 and rows[0].payload["user_id"] == graph_scope(G)
    await mem0_provider.set_visibility(fact_id, Visibility.GRAPH)  # idempotent
    assert len(mirrors(mem0_provider, fact_id)) == 1
    await mem0_provider.set_visibility(fact_id, Visibility.PRIVATE)
    assert mirrors(mem0_provider, fact_id) == []
    assert (await mem0_provider.get(fact_id)).visibility is Visibility.PRIVATE


async def test_the_canonical_record_decides_and_a_stale_mirror_is_never_returned(mem0_provider):
    """Defence against a half-applied un-share: a mirror whose canonical fact is
    private (or gone) is ignored by search and listing."""

    text = unique("The user prefers tea at project reviews")
    fact_id = await mem0_provider.add(fact(A, text))
    await mem0_provider.set_visibility(fact_id, Visibility.GRAPH)
    # Simulate a crash between "flip canonical to private" and "delete mirror".
    mem0_provider.mem0.update(str(fact_id), metadata={"visibility": "private"})
    assert len(mirrors(mem0_provider, fact_id)) == 1
    assert text not in await contents(mem0_provider, owner=B, graphs={G})
    assert all(f.content != text for f in await mem0_provider.list_facts(
        owner_user_id=B, readable_graph_ids=frozenset({G}), limit=50))


async def test_a_record_outside_its_owners_scope_is_not_a_fact(mem0_provider):
    """A record whose metadata claims owner A but that sits in B's scope — the
    shape a merge across owners would produce — is never projected as A's fact."""

    forged = mem0_provider.mem0.add(
        "The user prefers forged tea", user_id=owner_scope(B), infer=False,
        metadata={"jarvis_kind": "fact", "owner_user_id": str(A), "source_user_id": str(A),
                  "graph_id": str(G), "visibility": "private", "fact_type": "preference",
                  "timestamp": "2026-01-01T00:00:00+00:00", "content_hash": "x"},
    )["results"][0]["id"]
    assert await mem0_provider.get(uuid.UUID(forged)) is None
    assert "The user prefers forged tea" not in await contents(mem0_provider, owner=A, query="forged tea")


async def test_update_content_changes_the_fact_and_its_mirror(mem0_provider):
    fact_id = await mem0_provider.add(fact(A, "The user prefers the project board in list view"))
    await mem0_provider.set_visibility(fact_id, Visibility.GRAPH)
    await mem0_provider.update_content(fact_id, "The user prefers the project board in kanban view")
    assert (await mem0_provider.get(fact_id)).content.endswith("kanban view")
    assert mirrors(mem0_provider, fact_id)[0].payload["data"].endswith("kanban view")
    assert "The user prefers the project board in kanban view" in await contents(
        mem0_provider, owner=B, graphs={G}, query="project board view")


# ── deletion and lifecycle (MP-T11, MEM-T9) ────────────────────────────────


async def test_delete_removes_the_fact_and_every_mirror(mem0_provider):
    fact_id = await mem0_provider.add(fact(A, unique("The user prefers tea")))
    await mem0_provider.set_visibility(fact_id, Visibility.GRAPH)
    assert await mem0_provider.delete(fact_id) is True
    assert await mem0_provider.get(fact_id) is None
    assert mirrors(mem0_provider, fact_id) == []
    assert await mem0_provider.delete(fact_id) is False


async def test_account_deletion_removes_all_of_one_users_facts_and_mirrors_only(mem0_provider):
    a_private = await mem0_provider.add(fact(A, unique("The user prefers tea")))
    a_shared = await mem0_provider.add(fact(A, unique("The user wants the project done"), graph=H))
    await mem0_provider.set_visibility(a_shared, Visibility.GRAPH)
    b_private = await mem0_provider.add(fact(B, unique("The user prefers coffee")))

    removed = await mem0_provider.delete_all_for_user(A)
    assert removed == 3  # two facts + one mirror
    assert await mem0_provider.get(a_private) is None and await mem0_provider.get(a_shared) is None
    assert mirrors(mem0_provider, a_shared) == []
    assert mem0_provider.mem0.vector_store.list(filters={"owner_user_id": str(A)}, top_k=100)[0] == []
    assert (await mem0_provider.get(b_private)).owner_user_id == B


async def test_mem_t9_graph_deletion_removes_only_graph_shared_facts(mem0_provider):
    shared = await mem0_provider.add(fact(A, unique("The user wants the project shipped")))
    await mem0_provider.set_visibility(shared, Visibility.GRAPH)
    a_private_in_g = await mem0_provider.add(fact(A, unique("The user prefers tea")))
    b_private_in_g = await mem0_provider.add(fact(B, unique("The user prefers coffee")))
    other_graph_shared = await mem0_provider.add(fact(B, unique("The user wants docs"), graph=H))
    await mem0_provider.set_visibility(other_graph_shared, Visibility.GRAPH)

    removed = await mem0_provider.delete_graph_shared(G)
    assert removed == 2  # the shared fact and its mirror
    assert await mem0_provider.get(shared) is None
    assert (await mem0_provider.get(a_private_in_g)).owner_user_id == A
    assert (await mem0_provider.get(b_private_in_g)).owner_user_id == B
    assert (await mem0_provider.get(other_graph_shared)).visibility is Visibility.GRAPH
    assert len(mirrors(mem0_provider, other_graph_shared)) == 1


async def test_deleted_text_is_not_kept_in_a_mem0_history_database(mem0_provider):
    """Mem0's history SQLite keeps the old text of every UPDATE/DELETE. The
    adapter disables it, so a deletion leaves no plaintext copy there."""

    text = unique("The user prefers history-free tea")
    fact_id = await mem0_provider.add(fact(A, text))
    await mem0_provider.update_content(fact_id, text + " now")
    await mem0_provider.delete(fact_id)
    assert mem0_provider.mem0.db.get_history(str(fact_id)) == []
    assert not list(Path(mem0_provider.path).rglob("history.db"))
    assert not (Path.home() / ".mem0").exists() or not list((Path.home() / ".mem0").glob("history*"))


# ── health and degradation (FAIL-008) ──────────────────────────────────────


async def test_health_reports_a_working_store(mem0_provider):
    assert await mem0_provider.health() is True


async def test_an_unavailable_store_fails_closed_without_leaking(mem0_provider, monkeypatch):
    text = unique("The user prefers private tea")
    await mem0_provider.add(fact(A, text))

    def boom(*args, **kwargs):
        raise RuntimeError(f"backend exploded while holding {text}")

    monkeypatch.setattr(mem0_provider.mem0.vector_store.collection, "count", boom)
    monkeypatch.setattr(mem0_provider.mem0.vector_store, "search", boom)
    monkeypatch.setattr(mem0_provider.mem0.vector_store, "get", boom)
    assert await mem0_provider.health() is False
    with pytest.raises(MemoryProviderUnavailable) as caught:
        await mem0_provider.search(query="tea", owner_user_id=A, readable_graph_ids=frozenset(), limit=5)
    assert text not in str(caught.value) and caught.value.__cause__ is None
    with pytest.raises(MemoryProviderUnavailable):
        await mem0_provider.get(uuid.uuid4())


# ── Mem0 hardening (docs/21 §2.1, MP-T4/T8) ────────────────────────────────


async def test_mem0_runs_with_telemetry_off_and_no_model_client(mem0_provider):
    import mem0.memory.telemetry as telemetry

    assert telemetry.MEM0_TELEMETRY is False
    assert telemetry.client_telemetry.posthog is None
    assert telemetry._oss_telemetry_instance is None
    memory = mem0_provider.mem0
    assert type(memory.llm).__name__ == "_RefusingLLM"
    assert type(memory.db).__name__ == "_NullHistory"
    for value in vars(memory).values():
        assert "openai" not in type(value).__module__.lower()
    assert mem0_provider.client.get_settings().anonymized_telemetry is False


async def test_mp_t4_a_planted_mem0_inference_call_cannot_reach_any_model(mem0_provider, egress):
    """Mem0's own extraction path (`infer=True`) would call its LLM. In this
    deployment that LLM is a refusing stub: the call fails and nothing leaves
    the process."""

    egress.active = True
    with pytest.raises(Exception):
        mem0_provider.mem0.add("The user prefers tea", user_id=owner_scope(A), infer=True)
    egress.active = False
    assert egress.network() == []


async def test_mp_t8_the_whole_lifecycle_makes_no_network_call_and_writes_only_its_store(
    mem0_provider, egress, tmp_path
):
    egress.active = True
    fact_id = await mem0_provider.add(fact(A, unique("The user prefers tea")))
    await mem0_provider.search(query="tea", owner_user_id=A, readable_graph_ids=frozenset({G}), limit=5)
    await mem0_provider.set_visibility(fact_id, Visibility.GRAPH)
    await mem0_provider.update_content(fact_id, "The user prefers green tea")
    await mem0_provider.list_facts(owner_user_id=B, readable_graph_ids=frozenset({G}), limit=5)
    await mem0_provider.delete(fact_id)
    await mem0_provider.delete_all_for_user(A)
    await mem0_provider.delete_graph_shared(G)
    await mem0_provider.health()
    egress.active = False
    assert egress.events == []
    assert egress.writes_outside == []


async def test_building_the_provider_makes_no_network_call(tmp_path, egress):
    from server.memory.mem0_provider import build_mem0_provider
    from tests.memory.conftest import mem0_section, require_memory_stack

    require_memory_stack()
    egress.active = True
    provider = build_mem0_provider(mem0_section(tmp_path / "fresh"))
    await provider.health()
    egress.active = False
    assert egress.events == []
    assert egress.writes_outside == []


async def test_memory_text_never_reaches_the_logs(mem0_provider, caplog):
    text = unique("The user prefers log-free tea")
    with caplog.at_level(logging.DEBUG):
        fact_id = await mem0_provider.add(fact(A, text))
        await mem0_provider.update_content(fact_id, text + " indeed")
        await mem0_provider.search(query="tea", owner_user_id=A, readable_graph_ids=frozenset(), limit=5)
        await mem0_provider.delete(fact_id)
    assert text not in caplog.text


# ── startup refusals ───────────────────────────────────────────────────────


async def test_the_adapter_refuses_an_unpinned_mem0_version(tmp_path, monkeypatch):
    import importlib.metadata

    from server.memory import mem0_provider as adapter
    from tests.memory.conftest import mem0_section, require_memory_stack

    require_memory_stack()
    real = importlib.metadata.version
    monkeypatch.setattr(importlib.metadata, "version",
                        lambda name: "9.9.9" if name == "mem0ai" else real(name))
    with pytest.raises(adapter.Mem0SetupError, match="validated against"):
        adapter.build_mem0_provider(mem0_section(tmp_path))


async def test_the_adapter_refuses_to_start_when_spacy_could_download_models(tmp_path, monkeypatch):
    import importlib.util

    from server.memory import mem0_provider as adapter
    from tests.memory.conftest import mem0_section, require_memory_stack

    require_memory_stack()
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: object() if name == "spacy" else real(name, *a))
    with pytest.raises(adapter.Mem0SetupError, match="spaCy"):
        adapter.build_mem0_provider(mem0_section(tmp_path))


async def test_an_unprovisioned_embedder_is_a_startup_failure_not_a_download(tmp_path, egress):
    from server.memory.mem0_provider import build_mem0_provider
    from server.models.embedding import EmbedderUnavailable
    from tests.memory.conftest import mem0_section, require_memory_stack

    require_memory_stack()
    egress.active = True
    with pytest.raises(EmbedderUnavailable, match="python -m server.memory provision"):
        build_mem0_provider(mem0_section(tmp_path, embedder_cache=str(tmp_path / "empty_cache")))
    egress.active = False
    assert egress.network() == []


async def test_deleted_and_corrected_text_leaves_no_trace_on_disk(mem0_provider):
    """MP-T11 made physical: after delete, un-share, correction, account deletion
    and graph deletion, the removed text is in no file of the store — not in
    Chroma's write-ahead log, not in freed SQLite pages, not in a history DB."""

    def on_disk(marker: str) -> list[str]:
        return [str(f) for f in Path(mem0_provider.path).rglob("*")
                if f.is_file() and marker.encode() in f.read_bytes()]

    kept, deleted, old, unshared, purged_user, purged_graph = (unique(k) for k in (
        "KEPTMARK", "DELETEDMARK", "OLDMARK", "UNSHAREDMIRRORMARK", "USERMARK", "GRAPHMARK"))
    keep_id = await mem0_provider.add(fact(A, f"The user prefers {kept}"))
    del_id = await mem0_provider.add(fact(A, f"The user prefers {deleted}"))
    await mem0_provider.set_visibility(del_id, Visibility.GRAPH)
    old_id = await mem0_provider.add(fact(A, f"The user prefers {old}"))
    un_id = await mem0_provider.add(fact(A, f"The user prefers {unshared}"))
    await mem0_provider.set_visibility(un_id, Visibility.GRAPH)
    await mem0_provider.add(fact(C, f"The user prefers {purged_user}"))
    graph_id = await mem0_provider.add(fact(B, f"The user prefers {purged_graph}", graph=H))
    await mem0_provider.set_visibility(graph_id, Visibility.GRAPH)
    assert all(on_disk(m) for m in (kept, deleted, old, unshared, purged_user, purged_graph))

    await mem0_provider.delete(del_id)
    await mem0_provider.update_content(old_id, "The user prefers the corrected value")
    await mem0_provider.set_visibility(un_id, Visibility.PRIVATE)
    await mem0_provider.delete_all_for_user(C)
    await mem0_provider.delete_graph_shared(H)

    assert on_disk(kept), "control: live text is on disk"
    assert on_disk(unshared), "control: the un-shared fact itself still exists (private)"
    for marker in (deleted, old, purged_user, purged_graph):
        assert on_disk(marker) == [], marker
    # the un-shared fact's graph-scope mirror is gone: only its one canonical copy remains
    rows = mem0_provider.mem0.vector_store.list(filters={"owner_user_id": str(A)}, top_k=100)[0]
    assert sum(unshared in (r.payload.get("data") or "") for r in rows) == 1
    assert (await mem0_provider.get(keep_id)).content.endswith(kept)
