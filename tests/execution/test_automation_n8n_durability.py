"""n8n executor hardening (2026-11-27): streamed response bounds and the durable
idempotency record (`automation_invocations`, `StorageInvocationLedger`).

Everything here uses TEST-ONLY workflows against an httpx transport standing in
for n8n; nothing external is called.

| Property | Tests |
|---|---|
| an oversized answer is refused while streaming, never buffered | `test_an_oversized_streamed_answer_stops_being_read`, `test_a_declared_oversize_is_refused_before_reading`, `test_an_encoded_body_is_refused` |
| the reservation is durable before sending | `test_the_reservation_is_committed_before_anything_is_sent` |
| restart: nothing that may have run is re-sent | `test_a_completed_call_is_not_re_sent_after_a_restart`, `test_a_crash_after_sending_is_uncertain_after_a_restart` |
| uncertain outcomes are never retried | `test_a_lost_connection_after_sending_is_uncertain`, `test_cancellation_after_sending_is_recorded_uncertain` |
| only provably unsent attempts re-run | `test_a_call_that_never_left_may_run_again`, `test_a_stale_reservation_is_re_taken` |
| concurrent duplicates across processes | `test_two_instances_sharing_the_store_send_once` |
| retention | `test_old_records_are_pruned` |
| no credential or response data in the record's audit-facing fields | `test_the_record_holds_no_credential` |
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from server.composition.automation_ledger import StorageInvocationLedger
from server.execution.automation import InvocationState, N8nWorkflowExecutor, idempotency_key
from server.storage import SQLAlchemyStorageBackend
from server.storage.models import AutomationInvocationRow
from shared.schemas.execution import ExecutionError, ExecutionErrorCode
from tests.dbsupport import database_url_for
from tests.execution.test_automation_n8n import ECHO, FAIL, TOKEN, Recorder, _token, request

ARGS = {"workflow_id": "jarvis.echo", "inputs": {"message": "test only"}}


@pytest.fixture
async def store(tmp_path):
    storage = SQLAlchemyStorageBackend(database_url_for(tmp_path / f"{uuid.uuid4().hex}.db"))
    await storage.init_models()
    yield storage
    await storage.dispose()


def ledger(store, **kw) -> StorageInvocationLedger:
    kw.setdefault("stale_after", timedelta(minutes=5))
    return StorageInvocationLedger(store, **kw)


def make(store, transport, **kw) -> N8nWorkflowExecutor:
    return N8nWorkflowExecutor(base_url="http://127.0.0.1:5678", workflows=[ECHO, FAIL], token=_token,
                               transport=transport, ledger=kw.pop("ledger", None) or ledger(store), **kw)


async def rows(store) -> list[AutomationInvocationRow]:
    async with store.session() as s:
        return list((await s.execute(select(AutomationInvocationRow))).scalars())


# ── streamed response bounds ──────────────────────────────────────────────


class CountingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: int, size: int = 4096) -> None:
        self.total, self.size, self.pulled = chunks, size, 0

    async def __aiter__(self):
        for _ in range(self.total):
            self.pulled += 1
            yield b"x" * self.size


async def test_an_oversized_streamed_answer_stops_being_read(store):
    stream = CountingStream(chunks=10_000)          # ~40 MiB offered, no Content-Length
    ex = make(store, httpx.MockTransport(lambda r: httpx.Response(200, stream=stream)), max_response_bytes=65536)
    with pytest.raises(ExecutionError) as exc:
        await ex.run(request(ARGS))
    assert exc.value.code is ExecutionErrorCode.RESPONSE_TOO_LARGE
    assert stream.pulled <= 65536 // 4096 + 1, "the executor kept reading past its limit"
    # n8n answered: the call happened, so it is recorded as a known failure, never re-sent.
    [row] = await rows(store)
    assert (row.state, row.error_code) == ("failed", "response_too_large")


async def test_a_declared_oversize_is_refused_before_reading(store):
    stream = CountingStream(chunks=100)
    ex = make(store, httpx.MockTransport(lambda r: httpx.Response(
        200, stream=stream, headers={"Content-Length": str(100 * 4096)})), max_response_bytes=1000)
    with pytest.raises(ExecutionError) as exc:
        await ex.run(request(ARGS))
    assert exc.value.code is ExecutionErrorCode.RESPONSE_TOO_LARGE and stream.pulled == 0


async def test_an_encoded_body_is_refused(store):
    seen = []

    def handler(r):
        seen.append(r.headers.get("accept-encoding"))
        return httpx.Response(200, content=b"\x1f\x8b...", headers={"Content-Encoding": "gzip"})

    with pytest.raises(ExecutionError) as exc:
        await make(store, httpx.MockTransport(handler)).run(request(ARGS))
    assert exc.value.code is ExecutionErrorCode.AUTOMATION_UNAVAILABLE and seen == ["identity"]


async def test_a_bounded_answer_within_the_limit_still_works(store):
    ex = make(store, httpx.MockTransport(Recorder()), max_response_bytes=65536)
    result = await ex.run(request(ARGS))
    assert json.loads(result.content)["result"] == {"echo": {"message": "test only"}}


# ── the durable record ────────────────────────────────────────────────────


async def test_the_reservation_is_committed_before_anything_is_sent(store):
    states = []

    async def handler(r):
        states.extend(row.state for row in await rows(store))   # what another process would see now
        return httpx.Response(200, json={"ok": True})

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, r):
            return await handler(r)

    await make(store, Transport()).run(request(ARGS))
    assert states == ["in_progress"]
    [row] = await rows(store)
    assert row.state == "completed" and json.loads(row.result_content)["result"] == {"ok": True}


async def test_a_completed_call_is_not_re_sent_after_a_restart(store):
    rec = Recorder()
    first = await make(store, httpx.MockTransport(rec)).run(request(ARGS))
    # A new process: new executor, new ledger object, same store.
    again = await make(store, httpx.MockTransport(rec)).run(request(ARGS))
    assert len(rec.requests) == 1
    assert "not re-executed" in again.content and first.content in again.content


async def test_a_crash_after_sending_is_uncertain_after_a_restart(store):
    rec = Recorder()
    key = idempotency_key(request(ARGS), "jarvis.echo", ARGS["inputs"])
    crashed = ledger(store)
    assert await crashed.reserve(key, user_id=uuid.UUID(int=1), task_id=uuid.UUID(int=2),
                                 workflow_id="jarvis.echo") is None
    await crashed.record(key, InvocationState.IN_PROGRESS)       # …and the process died here
    with pytest.raises(ExecutionError) as exc:
        await make(store, httpx.MockTransport(rec)).run(request(ARGS))
    assert exc.value.code is ExecutionErrorCode.OUTCOME_UNCERTAIN and rec.requests == []


async def test_a_lost_connection_after_sending_is_uncertain(store):
    rec = Recorder(lambda r: (_ for _ in ()).throw(httpx.RemoteProtocolError("connection reset", request=r)))
    ex = make(store, httpx.MockTransport(rec))
    with pytest.raises(ExecutionError):
        await ex.run(request(ARGS))
    with pytest.raises(ExecutionError) as exc:
        await ex.run(request(ARGS))
    assert exc.value.code is ExecutionErrorCode.OUTCOME_UNCERTAIN and len(rec.requests) == 1
    [row] = await rows(store)
    assert row.state == "outcome_uncertain"


async def test_cancellation_after_sending_is_recorded_uncertain(store):
    arrived = asyncio.Event()

    class Hang(httpx.AsyncBaseTransport):
        async def handle_async_request(self, r):
            arrived.set()
            await asyncio.sleep(30)
            return httpx.Response(200, json={})

    running = asyncio.ensure_future(make(store, Hang()).run(request(ARGS)))
    await arrived.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    [row] = await rows(store)
    assert (row.state, row.error_code) == ("outcome_uncertain", "cancelled")
    rec = Recorder()
    with pytest.raises(ExecutionError) as exc:
        await make(store, httpx.MockTransport(rec)).run(request(ARGS))
    assert exc.value.code is ExecutionErrorCode.OUTCOME_UNCERTAIN and rec.requests == []


async def test_a_call_that_never_left_may_run_again(store):
    up = False

    def handler(r):
        if not up:
            raise httpx.ConnectError("refused", request=r)
        return httpx.Response(200, json={"ok": True})

    rec = Recorder(handler)
    with pytest.raises(ExecutionError) as exc:
        await make(store, httpx.MockTransport(rec)).run(request(ARGS))
    assert exc.value.code is ExecutionErrorCode.AUTOMATION_UNAVAILABLE
    [row] = await rows(store)
    assert row.state == "failed_before_send"
    up = True
    result = await make(store, httpx.MockTransport(rec)).run(request(ARGS))   # after a restart, too
    assert json.loads(result.content)["result"] == {"ok": True}
    assert (await rows(store))[0].state == "completed"


async def test_a_stale_reservation_is_re_taken(store):
    key = idempotency_key(request(ARGS), "jarvis.echo", ARGS["inputs"])
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    dead = StorageInvocationLedger(store, stale_after=timedelta(minutes=5), clock=lambda: past)
    assert await dead.reserve(key, user_id=uuid.UUID(int=1), task_id=uuid.UUID(int=2),
                              workflow_id="jarvis.echo") is None   # …died before marking in_progress
    rec = Recorder()
    await make(store, httpx.MockTransport(rec)).run(request(ARGS))
    assert len(rec.requests) == 1


async def test_a_fresh_reservation_held_elsewhere_is_not_taken(store):
    key = idempotency_key(request(ARGS), "jarvis.echo", ARGS["inputs"])
    other = ledger(store)
    assert await other.reserve(key, user_id=uuid.UUID(int=1), task_id=uuid.UUID(int=2),
                               workflow_id="jarvis.echo") is None
    rec = Recorder()
    with pytest.raises(ExecutionError) as exc:
        await make(store, httpx.MockTransport(rec)).run(request(ARGS))
    assert exc.value.code is ExecutionErrorCode.OUTCOME_UNCERTAIN and rec.requests == []


async def test_two_instances_sharing_the_store_send_once(store):
    """Two processes (two executors, two ledgers, one store), the same request
    at the same time: one sends, the other is refused — never both."""

    calls = 0
    release = asyncio.Event()

    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, r):
            nonlocal calls
            calls += 1
            await release.wait()
            return httpx.Response(200, json={"ok": True})

    a, b = make(store, Slow()), make(store, Slow())
    first = asyncio.ensure_future(a.run(request(ARGS)))
    second = asyncio.ensure_future(b.run(request(ARGS)))
    await asyncio.sleep(0.3)
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert calls == 1
    refused = [r for r in results if isinstance(r, ExecutionError)]
    assert len(refused) == 1 and refused[0].code is ExecutionErrorCode.OUTCOME_UNCERTAIN


async def test_different_users_or_tasks_never_share_a_record(store):
    rec = Recorder()
    ex = make(store, httpx.MockTransport(rec))
    await ex.run(request(ARGS))
    await ex.run(request(ARGS, user=uuid.UUID(int=7)))
    await ex.run(request(ARGS, task=uuid.UUID(int=8)))
    assert len(rec.requests) == 3 and len(await rows(store)) == 3


async def test_old_records_are_pruned(store):
    rec = Recorder()
    long_ago = datetime.now(timezone.utc) - timedelta(days=40)
    old = StorageInvocationLedger(store, stale_after=timedelta(minutes=5), clock=lambda: long_ago)
    await make(store, httpx.MockTransport(rec), ledger=old).run(request(ARGS))
    await make(store, httpx.MockTransport(rec)).run(request(ARGS, task=uuid.UUID(int=9)))
    keys = [r.task_id for r in await rows(store)]
    assert keys == [uuid.UUID(int=9)]   # the 40-day-old record is gone


async def test_the_record_holds_no_credential(store):
    await make(store, httpx.MockTransport(Recorder())).run(request(ARGS))
    [row] = await rows(store)
    assert TOKEN not in repr(vars(row))
    assert row.error_code is None and row.workflow_id == "jarvis.echo"


async def test_an_unavailable_record_sends_nothing(store):
    class Broken:
        async def reserve(self, *a, **k):
            raise RuntimeError("database down")

        async def record(self, *a, **k):
            raise AssertionError("never reached")

    rec = Recorder()
    with pytest.raises(ExecutionError) as exc:
        await make(store, httpx.MockTransport(rec), ledger=Broken()).run(request(ARGS))
    assert exc.value.code is ExecutionErrorCode.AUTOMATION_UNAVAILABLE and rec.requests == []
