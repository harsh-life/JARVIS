"""PRD §39 #22 and #32 under concurrent multi-tenant load (OD-TEST-2).

#22: "Per-user/per-graph Mem0 isolation is verified by a concurrency/multi-tenant
test, not assumed." #32: "The system holds correctly under a ~10-device
concurrent pilot load, with per-principal fairness." The other MEM-T1 suites
drive users one at a time; this one drives the pilot's size — ten users, each
on their own device, all members of one shared graph — **at the same time**,
through the production composition root and the real Mem0 + Chroma store.

What must hold while everything interleaves:

* every user's private facts reach that user only — through the memory API,
  a targeted search, and the agent's hydrated context;
* every shared fact reaches every member;
* the concurrency caps refuse load **explicitly** and per principal: a
  refusal is a retryable `429`/`503`, never a silent drop, never another
  user's answer, and one user flooding the server cannot take every slot.

A retryable refusal is retried, as the Android client does; anything else is a
failure. A `503 dependency_unavailable` for a transient store conflict is an
honest, explicit state (02 §13), not a defect — but since H-1 one user's
running task never holds the store against another's (the flood test below).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from server.gateway.app import API_V1_PREFIX
from tests.memory.conftest import unique
from tests.runtime.conftest import final

pytestmark = pytest.mark.asyncio

MEM = f"{API_V1_PREFIX}/memory"
PILOT = 10
RETRYABLE = {429, 503}


async def _until_served(send, *, attempts: int = 40):
    """Retry a request refused retryably (explicit back-pressure), as a client
    would. Anything else is returned as-is for the caller to assert on."""

    for attempt in range(attempts):
        resp = await send()
        if resp.status_code not in RETRYABLE:
            return resp
        assert resp.json()["error"]["retryable"] is True, resp.text
        await asyncio.sleep(0.02 * (attempt + 1))
    raise AssertionError(f"still refused after {attempts} attempts: {resp.status_code} {resp.text}")


async def _remember(h, actor, text, fact_type="preference") -> str:
    resp = await _until_served(lambda: h.client.post(
        MEM, json={"fact_type": fact_type, "content": text}, headers=actor.auth))
    assert resp.status_code == 201, resp.text
    return resp.json()["fact_id"]


async def _share(h, actor, fact_id) -> None:
    url = f"{MEM}/{fact_id}"
    first = await _until_served(lambda: h.client.patch(url, json={"visibility": "graph"}, headers=actor.auth))
    assert first.status_code == 403, first.text
    token = first.json()["error"]["details"]["confirmation_token"]
    second = await _until_served(lambda: h.client.patch(
        url, json={"visibility": "graph"}, headers={**actor.auth, "X-Confirmation-Token": token}))
    assert second.status_code == 200, second.text


async def _listed(h, actor, query: str | None = None) -> str:
    resp = await _until_served(lambda: h.client.get(
        MEM, params={"query": query, "limit": 100} if query else {"limit": 100}, headers=actor.auth))
    assert resp.status_code == 200, resp.text
    return "\n".join(item["content"] for item in resp.json()["items"])


@pytest.fixture
async def pilot(stack):
    h = await stack()
    users = [await h.user(f"pilot-{i}") for i in range(PILOT)]
    graph = await h.shared_graph(users[0], *users[1:])
    return h, users, graph


async def test_ten_concurrent_users_never_see_each_others_private_facts(pilot):
    h, users, _ = pilot
    private = {u.subject: [unique(f"The user prefers the {u.subject} private plan {k}") for k in range(2)]
               for u in users}
    shared = {u.subject: unique(f"The user wants the {u.subject} shared trip booked") for u in users}

    # Every user writes at once; then every user shares one fact at once.
    await asyncio.gather(*(_remember(h, u, text) for u in users for text in private[u.subject]))
    shared_ids = await asyncio.gather(*(_remember(h, u, shared[u.subject], "stated_goal") for u in users))
    await asyncio.gather(*(_share(h, u, fact_id) for u, fact_id in zip(users, shared_ids)))

    # Every user reads at once — a full listing, and a search aimed squarely
    # at the next user's private facts.
    listings = await asyncio.gather(*(_listed(h, u) for u in users))
    probes = await asyncio.gather(*(
        _listed(h, u, query=private[users[(i + 1) % PILOT].subject][0]) for i, u in enumerate(users)))

    all_shared = set(shared.values())
    for i, (u, listing, probe) in enumerate(zip(users, listings, probes)):
        others_private = [t for s, texts in private.items() if s != u.subject for t in texts]
        assert all(t in listing for t in private[u.subject]), f"{u.subject} lost its own facts"
        assert all(t in listing for t in all_shared), f"{u.subject} missed a shared fact"
        leaked = [t for t in others_private if t in listing or t in probe]
        assert leaked == [], f"{u.subject} saw another user's private facts: {leaked}"


async def test_concurrent_tasks_hydrate_only_their_own_principals_memory(pilot):
    h, users, _ = pilot
    private = {u.subject: unique(f"The user prefers {u.subject} seats near the window") for u in users}
    await asyncio.gather(*(_remember(h, u, private[u.subject]) for u in users))

    # Every task's request names every user's words, so relevance alone would
    # pull every fact in; only visibility may keep the others' out.
    everyone = " ".join(private.values())
    h.model.push(*[final("ok") for _ in range(4 * PILOT)])

    async def run(u):
        text = f"[{u.subject}] plan my seats: {everyone}"
        resp = await _until_served(lambda: h.submit(u, text))
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "completed", resp.text
        return text

    inputs = await asyncio.gather(*(run(u) for u in users))

    for u, text in zip(users, inputs):
        prompts = [c for c in h.model.seen if any(m.content.endswith(text) or text in m.content for m in c)]
        assert prompts, f"no model call for {u.subject}"
        context = "\n".join(m.content.split("USER REQUEST:")[0] for c in prompts for m in c)
        assert private[u.subject] in context, f"{u.subject}'s own fact was not hydrated"
        leaked = [t for s, t in private.items() if s != u.subject and t in context]
        assert leaked == [], f"{u.subject}'s task hydrated another user's private fact: {leaked}"


async def test_one_flooding_user_is_capped_per_principal_and_never_blocks_another(pilot):
    """Per-principal fairness (13 §6, PRD #32), measured in both directions.

    The flooder's excess is refused **per principal** — explicitly and
    retryably — so one user can never occupy more than its own slots; and while
    the flooder's admitted tasks are still running, another user's task is
    served at once. Before H-1 that second half failed on SQLite: a running
    task's request held the store's one write lock, and the other user got
    `503 storage` for as long as it ran (docs/RELEASE_VALIDATION.md, H-1).
    """

    h, users, _ = pilot
    flooder, others = users[0], users[1:]
    gate = asyncio.Event()

    async def respond(messages):
        # The script is one queue shared by every task, so each answer decides
        # by the request it is answering: only the flooder's tasks are held.
        if "flood" in messages[-1].content:
            await gate.wait()
        return final("ok")

    h.model.push(*[respond for _ in range(8 * PILOT)])
    flood = [asyncio.create_task(h.submit(flooder, f"flood {n}")) for n in range(6)]
    await asyncio.sleep(0.3)

    # Per-principal caps: the flooder's excess came back refused, retryably.
    refused_now = [t.result() for t in flood if t.done()]
    assert len(refused_now) >= 5 and all(r.status_code == 429 for r in refused_now), \
        [r.text for r in refused_now]
    limits = {r.json()["error"]["details"]["limit"] for r in refused_now}
    assert limits <= {"per_session_concurrency", "per_user_concurrency"}, limits
    assert all(r.json()["error"]["retryable"] is True for r in refused_now)

    # Another user's task is served while the flooder's are still running.
    during = await h.submit(others[0], "meanwhile, a normal request")  # not held by `respond`
    assert during.status_code == 200 and during.json()["status"] == "completed", during.text
    assert sum(1 for t in flood if not t.done()) == h.config.security.rate_limits.per_session_concurrent_tasks

    gate.set()
    flooded = await asyncio.gather(*flood)
    accepted = [r for r in flooded if r.status_code == 200]
    assert len(accepted) == h.config.security.rate_limits.per_session_concurrent_tasks
    served = await asyncio.gather(*(_until_served(lambda u=u: h.submit(u, f"normal {u.subject}")) for u in others))
    assert all(r.status_code == 200 and r.json()["status"] == "completed" for r in served), \
        [r.text for r in served if r.status_code != 200]


async def test_a_task_result_is_never_readable_by_another_concurrent_user(pilot):
    h, users, _ = pilot
    h.model.push(*[final(f"answer-{uuid.uuid4().hex[:6]}") for _ in range(4 * PILOT)])
    responses = await asyncio.gather(*(_until_served(lambda u=u: h.submit(u, f"hello from {u.subject}"))
                                       for u in users))
    task_ids = [r.json()["task_id"] for r in responses]
    # Every user tries every other user's task id at once: all 404 (anti-enumeration).
    reads = await asyncio.gather(*(h.get(u, task_ids[(i + k) % PILOT])
                                   for i, u in enumerate(users) for k in range(1, 4)))
    assert all(r.status_code == 404 for r in reads), [r.status_code for r in reads if r.status_code != 404]
    own = await asyncio.gather(*(h.get(u, task_ids[i]) for i, u in enumerate(users)))
    assert all(r.status_code == 200 for r in own)
