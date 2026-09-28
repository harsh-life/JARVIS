"""PRD §39 #32 — measured service under ~10-device pilot load (RG6, OD-TEST-2).

#32: "The system holds correctly under a ~10-device concurrent pilot load, with
per-principal fairness — no user's usage starves another's." 17 §7 OD-TEST-2
leaves *how* it is load-simulated to the implementer and requires
"multi-tenant boundary-under-load". `test_pilot_concurrency.py` shows isolation
and the per-principal caps hold under concurrency. This module **measures** the
service side — who waits, how long, and why — through the production
composition root, the real Mem0 + Chroma store and the pilot's file-backed
SQLite store, and prints the numbers (`-s`).

Like BR-T2 this is a measurement, asserted in both directions. The invariants
must hold: nothing leaks, nothing is silently dropped, every refusal is explicit
and retryable, every user is eventually served. The *shortfall* is asserted
too: while one user's task runs longer than the store's busy timeout, other
users' write-bearing requests are refused `503 storage`. If that assertion ever
fails, the single-writer limitation has genuinely changed; re-run this and
update `docs/RELEASE_VALIDATION.md` rather than deleting the assertion.

Model latency is simulated with a sleep. Real model calls commonly take
several seconds, and `agent.bounds.wall_clock_timeout_seconds` (OD-02) allows a
task to run for up to 120 s; the long-task scenario uses a call just past the
5 s busy timeout, the shortest duration at which the effect appears.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field

import pytest

from server.gateway.app import API_V1_PREFIX
from tests.memory.conftest import unique
from tests.runtime.conftest import final

pytestmark = pytest.mark.asyncio

MEM = f"{API_V1_PREFIX}/memory"
PILOT = 10
RETRYABLE = {429, 503}
SHORT_MODEL_SECONDS = 0.5
LONG_MODEL_SECONDS = 6.0  # just past SQLite's 5 s busy timeout (server/storage)


@dataclass
class Served:
    user: str
    kind: str
    status: int
    seconds: float
    refusals: dict[int, int] = field(default_factory=dict)


async def _serve(user: str, kind: str, send, *, deadline: float = 90.0) -> Served:
    """Send, retrying explicit retryable refusals with the client's backoff,
    and record how long the user waited and what they were refused with."""

    start = time.monotonic()
    refusals: dict[int, int] = {}
    attempt = 0
    while True:
        resp = await send()
        if resp.status_code not in RETRYABLE:
            return Served(user, kind, resp.status_code, time.monotonic() - start, refusals)
        body = resp.json()["error"]
        assert body["retryable"] is True, resp.text
        if resp.status_code == 503:
            assert body["details"] == {"dependency": "storage"}, resp.text
        refusals[resp.status_code] = refusals.get(resp.status_code, 0) + 1
        if time.monotonic() - start > deadline:
            return Served(user, kind, resp.status_code, time.monotonic() - start, refusals)
        attempt += 1
        await asyncio.sleep(min(0.05 * attempt, 0.5))


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def _table(title: str, rows: list[tuple[str, str]]) -> None:
    print(f"\n{title}")
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"  {key.ljust(width)}  {value}")


@pytest.fixture
async def pilot(stack):
    h = await stack()
    users = [await h.user(f"svc-{i}") for i in range(PILOT)]
    graph = await h.shared_graph(users[0], *users[1:])
    return h, users, graph


async def test_s1_ten_users_at_once_are_all_served_but_serialized(pilot):
    """S1: every user submits one task and one memory write at the same moment."""

    h, users, _ = pilot

    async def respond(messages):
        await asyncio.sleep(SHORT_MODEL_SECONDS)
        return final("ok")

    h.model.push(*[respond for _ in range(6 * PILOT)])
    facts = {u.subject: unique(f"The user prefers {u.subject} aisle seats") for u in users}

    started = time.monotonic()
    results = await asyncio.gather(*(
        *(_serve(u.subject, "task", lambda u=u: h.submit(u, f"plan for {u.subject}")) for u in users),
        *(_serve(u.subject, "memory_write", lambda u=u: h.client.post(
            MEM, json={"fact_type": "preference", "content": facts[u.subject]}, headers=u.auth))
          for u in users),
    ))
    wall = time.monotonic() - started

    tasks = [r for r in results if r.kind == "task"]
    writes = [r for r in results if r.kind == "memory_write"]
    assert all(r.status == 200 for r in tasks), [(r.user, r.status) for r in tasks]
    assert all(r.status == 201 for r in writes), [(r.user, r.status) for r in writes]

    # Nothing was lost or crossed: each user sees exactly their own new fact.
    for u in users:
        listing = (await h.client.get(MEM, params={"limit": 100}, headers=u.auth)).json()["items"]
        contents = {item["content"] for item in listing}
        assert facts[u.subject] in contents
        assert not (contents & {t for s, t in facts.items() if s != u.subject})

    task_s = [r.seconds for r in tasks]
    write_s = [r.seconds for r in writes]
    refused_503 = sum(r.refusals.get(503, 0) for r in results)
    refused_429 = sum(r.refusals.get(429, 0) for r in results)
    users_503 = sum(1 for u in users if any(r.refusals.get(503) for r in results if r.user == u.subject))
    _table(
        f"PRD #32 S1 — {PILOT} users, one task (model {SHORT_MODEL_SECONDS}s) + one memory write each, at once",
        [
            ("wall clock", f"{wall:.2f}s (a single task alone: ~{SHORT_MODEL_SECONDS:.1f}s)"),
            ("task time-to-served", f"p50 {statistics.median(task_s):.2f}s  p95 {_pct(task_s, .95):.2f}s  "
                                    f"max {max(task_s):.2f}s"),
            ("memory write time-to-served", f"p50 {statistics.median(write_s):.2f}s  p95 {_pct(write_s, .95):.2f}s  "
                                            f"max {max(write_s):.2f}s"),
            ("retryable refusals", f"503 storage: {refused_503} (users affected: {users_503}/{PILOT})  "
                                   f"429 caps: {refused_429}"),
            ("served / lost / leaked", f"{len(results)} / 0 / 0"),
        ],
    )
    # Every user was eventually served; the store serialized them (the slowest
    # user waited several single-task durations).
    assert max(task_s) > 2 * SHORT_MODEL_SECONDS


async def test_s2_one_long_task_blocks_every_other_users_writes(pilot):
    """S2: one user's model call runs past the busy timeout; the other nine each
    try one read and one write while it runs (one attempt each, no retry)."""

    h, users, _ = pilot
    long_user, others = users[0], users[1:]
    release = asyncio.Event()

    async def respond(messages):
        if "long task" in messages[-1].content:
            try:
                await asyncio.wait_for(release.wait(), timeout=LONG_MODEL_SECONDS)
            except asyncio.TimeoutError:
                pass
        return final("ok")

    h.model.push(*[respond for _ in range(4 * PILOT)])
    long_task = asyncio.create_task(h.submit(long_user, "long task"))
    await asyncio.sleep(0.3)  # the long task is running and has written

    async def once(u, kind, send):
        start = time.monotonic()
        resp = await send()
        return u.subject, kind, resp.status_code, time.monotonic() - start, resp

    attempts = await asyncio.gather(*(
        *(once(u, "memory_read", lambda u=u: h.client.get(MEM, params={"limit": 10}, headers=u.auth))
          for u in others),
        *(once(u, "memory_write", lambda u=u: h.client.post(
            MEM, json={"fact_type": "preference", "content": unique(f"{u.subject} likes tea")},
            headers=u.auth)) for u in others),
    ))
    release.set()
    done = await long_task
    assert done.status_code == 200, done.text

    reads = [a for a in attempts if a[1] == "memory_read"]
    writes = [a for a in attempts if a[1] == "memory_write"]
    for _, _, status, _, resp in attempts:
        assert status in {200, 201, 503}, resp.text
        if status == 503:
            assert resp.json()["error"]["retryable"] is True
            assert resp.json()["error"]["details"] == {"dependency": "storage"}
    reads_refused = [a for a in reads if a[2] == 503]
    writes_refused = [a for a in writes if a[2] == 503]

    # Once the long task ends, everyone is served (nothing was persisted by a
    # refused request, so the retry is the first write).
    after = await asyncio.gather(*(_serve(u.subject, "memory_write", lambda u=u: h.client.post(
        MEM, json={"fact_type": "preference", "content": unique(f"{u.subject} likes coffee")},
        headers=u.auth)) for u in others))
    assert all(r.status == 201 for r in after), [(r.user, r.status) for r in after]

    def waited(rows):
        return f"{statistics.median([r[3] for r in rows]):.2f}s" if rows else "-"

    _table(
        f"PRD #32 S2 — one user's task holds the store ({LONG_MODEL_SECONDS}s model call); "
        f"{len(others)} other users each try once",
        [
            ("other users' memory reads refused", f"{len(reads_refused)}/{len(reads)} 503 storage "
                                                  f"(median wait before answer {waited(reads)})"),
            ("other users' memory writes refused", f"{len(writes_refused)}/{len(writes)} 503 storage "
                                                   f"(median wait before refusal {waited(writes_refused)})"),
            ("after the long task ended", f"{sum(r.status == 201 for r in after)}/{len(after)} writes served"),
            ("#32 per-principal fairness", "NOT MET on the single-writer store: one user's task "
                                           "delays or refuses every other user's writes"),
        ],
    )

    # The shortfall, asserted so it cannot silently become a pass (see module docstring).
    assert len(writes_refused) == len(writes), [(a[0], a[2]) for a in writes]
