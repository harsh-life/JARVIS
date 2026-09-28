"""PRD §39 #32 — fair service under ~10-device pilot load (acceptance, OD-TEST-2).

#32: "The system holds correctly under a ~10-device concurrent pilot load, with
per-principal fairness — no user's usage starves another's." 17 §7 OD-TEST-2
leaves *how* it is load-simulated to the implementer and requires
"multi-tenant boundary-under-load". `test_pilot_concurrency.py` shows isolation
and the per-principal caps hold under concurrency. This module is the service
acceptance: it drives the production composition root, the real Mem0 + Chroma
store and the configured relational store, prints the measurement (`-s`), and
asserts the requirement.

The workload is the one the real-data gate measured (docs/RELEASE_VALIDATION.md
§I), unchanged:

* **S1** — ten users in one shared graph each submit one task (0.5 s model call)
  and one memory write, all at once, retrying explicit retryable refusals as
  the Android client does.
* **S2** — one user's task makes a 6 s model call (past SQLite's 5 s busy
  timeout, and short of `agent.bounds.wall_clock_timeout_seconds` = 120 s);
  while it runs, each of the nine other users reads and writes once.

"No user's usage starves another's", as asserted here:

* no request is refused `503 storage` — another user's open work never makes
  the store unavailable;
* every request is served, nothing is lost, nothing crosses users;
* S1: no user waits more than `MAX_WAIT_S1` (6x a single task; the per-principal
  concurrency caps — 8 tasks at once server-wide — make users 9-10 wait one
  wave, which is fairness, not starvation);
* S2: every other user's write succeeds on its first attempt, *while* the long
  task is still running, with a median latency of at most `MAX_MEDIAN_S2`.

The single-writer SQLite store fails this (H-1: one task holds the write lock
for its whole request). The acceptance therefore runs on PostgreSQL, the owner's
H-1 remedy; a run on anything else fails loudly unless the SQLite baseline is
requested explicitly with `HYPERMIND_PRD32_BASELINE=sqlite` — which then fails
the assertions, as it should, and prints the baseline numbers.
"""

from __future__ import annotations

import asyncio
import os
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
MAX_WAIT_S1 = 6 * SHORT_MODEL_SECONDS
MAX_MEDIAN_S2 = 1.0
BASELINE_ENV = "HYPERMIND_PRD32_BASELINE"


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
    backend = h.storage.engine.dialect.name
    if backend != "postgresql" and os.environ.get(BASELINE_ENV) != backend:
        pytest.fail(f"PRD #32 acceptance runs on PostgreSQL (H-1); this run's store is {backend!r}. "
                    f"Set HYPERMIND_TEST_DATABASE_URL, or {BASELINE_ENV}={backend} to measure the baseline.")
    users = [await h.user(f"svc-{i}") for i in range(PILOT)]
    graph = await h.shared_graph(users[0], *users[1:])
    return h, users, graph, backend


async def test_s1_ten_users_at_once_are_all_served_without_starvation(pilot):
    """S1: every user submits one task and one memory write at the same moment."""

    h, users, _, backend = pilot

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
    task_s = [r.seconds for r in tasks]
    write_s = [r.seconds for r in writes]
    refused_503 = sum(r.refusals.get(503, 0) for r in results)
    refused_429 = sum(r.refusals.get(429, 0) for r in results)
    users_503 = sum(1 for u in users if any(r.refusals.get(503) for r in results if r.user == u.subject))
    per_user = {u.subject: max(r.seconds for r in results if r.user == u.subject) for u in users}
    _table(
        f"PRD #32 S1 [{backend}] — {PILOT} users, one task (model {SHORT_MODEL_SECONDS}s) "
        f"+ one memory write each, at once",
        [
            ("wall clock", f"{wall:.2f}s (a single task alone: ~{SHORT_MODEL_SECONDS:.1f}s)"),
            ("task time-to-served", f"p50 {statistics.median(task_s):.2f}s  p95 {_pct(task_s, .95):.2f}s  "
                                    f"max {max(task_s):.2f}s"),
            ("memory write time-to-served", f"p50 {statistics.median(write_s):.2f}s  "
                                            f"p95 {_pct(write_s, .95):.2f}s  max {max(write_s):.2f}s"),
            ("per-user worst wait", "  ".join(f"{s.split('-')[-1]}:{v:.2f}s" for s, v in per_user.items())),
            ("retryable refusals", f"503 storage: {refused_503} (users affected: {users_503}/{PILOT})  "
                                   f"429 caps: {refused_429}"),
            ("served", f"{sum(r.status in (200, 201) for r in results)}/{len(results)}"),
        ],
    )

    assert all(r.status == 200 for r in tasks), [(r.user, r.status) for r in tasks]
    assert all(r.status == 201 for r in writes), [(r.user, r.status) for r in writes]
    # Nothing was lost or crossed: each user sees exactly their own new fact.
    for u in users:
        listing = (await h.client.get(MEM, params={"limit": 100}, headers=u.auth)).json()["items"]
        contents = {item["content"] for item in listing}
        assert facts[u.subject] in contents
        assert not (contents & {t for s, t in facts.items() if s != u.subject})

    assert refused_503 == 0, f"{refused_503} storage refusals across {users_503} users: the store starved them"
    assert max(per_user.values()) <= MAX_WAIT_S1, per_user


async def test_s2_one_long_task_does_not_block_other_users(pilot):
    """S2: one user's model call runs past the busy timeout; the other nine
    each read and write once while it runs (one attempt each, no retry)."""

    h, users, _, backend = pilot
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
    long_started = time.monotonic()
    long_task = asyncio.create_task(h.submit(long_user, "long task"))
    await asyncio.sleep(0.3)  # the long task is running and has written

    async def once(u, kind, send):
        start = time.monotonic()
        resp = await send()
        finished = time.monotonic()
        return u.subject, kind, resp.status_code, finished - start, resp, long_task.done()

    attempts = await asyncio.gather(*(
        *(once(u, "memory_read", lambda u=u: h.client.get(MEM, params={"limit": 10}, headers=u.auth))
          for u in others),
        *(once(u, "memory_write", lambda u=u: h.client.post(
            MEM, json={"fact_type": "preference", "content": unique(f"{u.subject} likes tea")},
            headers=u.auth)) for u in others),
    ))
    release.set()
    done = await long_task
    long_seconds = time.monotonic() - long_started
    assert done.status_code == 200, done.text

    reads = [a for a in attempts if a[1] == "memory_read"]
    writes = [a for a in attempts if a[1] == "memory_write"]
    for _, _, status, _, resp, _ in attempts:
        if status == 503:
            assert resp.json()["error"]["retryable"] is True, resp.text
    reads_ok = [a for a in reads if a[2] == 200]
    writes_ok = [a for a in writes if a[2] == 201]
    writes_503 = [a for a in writes if a[2] == 503]
    during = [a for a in writes_ok if not a[5]]
    write_s = [a[3] for a in writes]

    _table(
        f"PRD #32 S2 [{backend}] — one user's task runs a {LONG_MODEL_SECONDS}s model call; "
        f"{len(others)} other users each try once",
        [
            ("long task", f"{long_seconds:.2f}s end to end"),
            ("other users' memory reads", f"{len(reads_ok)}/{len(reads)} served "
                                          f"(median {statistics.median(a[3] for a in reads):.2f}s)"),
            ("other users' memory writes", f"{len(writes_ok)}/{len(writes)} served, {len(writes_503)} × 503 storage"),
            ("  served while the long task ran", f"{len(during)}/{len(writes)}"),
            ("  write latency", f"p50 {statistics.median(write_s):.2f}s  max {max(write_s):.2f}s"),
        ],
    )

    assert len(reads_ok) == len(reads), [(a[0], a[2]) for a in reads]
    assert writes_503 == [], f"{len(writes_503)}/{len(writes)} writes refused 503 while one task ran"
    assert len(writes_ok) == len(writes), [(a[0], a[2]) for a in writes]
    assert len(during) == len(writes), "a write had to wait for another user's task to finish"
    assert statistics.median(write_s) <= MAX_MEDIAN_S2, write_s
