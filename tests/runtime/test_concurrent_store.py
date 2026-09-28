"""Concurrency correctness once the store no longer serializes requests (H-1).

On SQLite a task's request held the single write lock for its whole run, so
everything that touched the store happened one request at a time. That hid
three hazards that a multi-writer store (PostgreSQL, the H-1 remedy) and short
transactions expose, and each is pinned here:

* **Transaction scope.** No database transaction stays open across a model
  call or a tool run — the long waits of a task.
* **Idempotency.** A retry with the same `Idempotency-Key` that arrives while
  the original still runs (the phone lost the response and resent from another
  device) must never run the task twice (02 §1.4, API-T9).
* **Budget admission.** Two users' paid calls admitted at the same moment must
  not both pass a budget that has room for one (13 §0: "a paid call that would
  breach budget is refused").

Plus: ten users' tasks at once leave consistent lifecycle rows, and a transient
store conflict (a PostgreSQL deadlock; SQLite's busy lock) is a retryable
`503`, never a `500`. The module runs on whichever store the suite is pointed
at (`tests/dbsupport.py`); CI runs it on PostgreSQL.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from server.gateway.errors import install_error_handlers
from server.models.provider import ModelResult
from server.storage.errors import is_transient_store_error
from server.storage.models import AgentTask, UsageEvent
from shared.schemas.enums import UsageKind
from tests.runtime.conftest import ask, call, final
from tests.runtime.test_secrets_and_providers import PAID

pytestmark = pytest.mark.asyncio

TASKS = "/api/v1/agent/tasks"


async def _open_transactions(h) -> int:
    """How many *other* connections to this database sit inside a transaction."""

    async with h.storage.engine.connect() as conn:
        return int((await conn.execute(text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'"
        ))).scalar_one())


async def _foreign_write_seconds(h) -> float:
    """Time for another connection to take the store's write lock and commit."""

    started = time.monotonic()
    async with h.storage.engine.begin() as conn:
        await conn.execute(text("UPDATE agent_tasks SET updated_at = updated_at WHERE 1 = 0"))
        if h.storage.engine.dialect.name == "sqlite":
            # A no-op UPDATE takes no lock on SQLite; this one does.
            await conn.execute(text("CREATE TABLE IF NOT EXISTS _probe (x INTEGER)"))
            await conn.execute(text("DROP TABLE _probe"))
    return time.monotonic() - started


async def _no_transaction_is_held(h) -> bool:
    if h.storage.engine.dialect.name == "postgresql":
        return await _open_transactions(h) == 0
    return await _foreign_write_seconds(h) < 1.0  # SQLite would wait its 5 s busy timeout


async def test_no_transaction_is_open_during_a_model_call_or_a_tool_run(make_harness):
    h = await make_harness()
    alice = await h.user("alice")
    await h.grant(alice, "file.read")
    observed: dict[str, bool] = {}

    async def model_turn(messages):
        observed["model call"] = await _no_transaction_is_held(h)
        return ask("file.read")

    async def tool_turn(messages):
        observed["second model call"] = await _no_transaction_is_held(h)
        return call("files.read", "list_directory")

    original = h.reads.execute

    async def probing_execute(invocation):
        observed["tool run"] = await _no_transaction_is_held(h)
        return await original(invocation)

    h.reads.execute = probing_execute  # type: ignore[method-assign]
    h.model.push(model_turn, tool_turn, final("done"))
    resp = await h.submit(alice, "list my files")

    assert resp.status_code == 200 and resp.json()["status"] == "completed", resp.text
    assert observed == {"model call": True, "second model call": True, "tool run": True}, observed


async def test_a_same_key_retry_while_the_original_runs_never_runs_twice(make_harness):
    """API-T9 under concurrency: the phone lost the response and resent the
    submission from the user's other device, with the same key."""

    h = await make_harness()
    phone = await h.user("alice")
    tablet = await h.user("alice")  # the same user (subject-keyed), a second device
    assert tablet.user_id == phone.user_id and tablet.device_id != phone.device_id
    release = asyncio.Event()

    async def slow(messages):
        await asyncio.wait_for(release.wait(), timeout=10)
        return final("done once")

    h.model.push(slow)
    key = uuid.uuid4().hex
    original = asyncio.create_task(h.submit(phone, "book the table", key=key))
    await asyncio.sleep(0.3)

    retry = await h.submit(tablet, "book the table", key=key)
    release.set()
    first = await original

    assert first.status_code == 200 and first.json()["response"] == "done once", first.text
    assert retry.status_code == 409, retry.text
    assert retry.json()["error"]["details"] == {"idempotency": "in_progress"}
    # Once the original has answered, the same key replays it — still one task.
    replay = await h.submit(tablet, "book the table", key=key)
    assert replay.status_code == 200 and replay.json()["task_id"] == first.json()["task_id"]
    assert len(await h.rows(AgentTask)) == 1
    assert len(h.model.seen) == 1


class _PaidModel:
    """A paid worker whose reported usage equals the runtime's projection, so
    the projection is not a loose upper bound and a budget race would show."""

    def __init__(self) -> None:
        self.spec = None
        self.calls = 0
        self.both_admitted = asyncio.Event()

    async def invoke(self, messages, *, timeout: float) -> ModelResult:
        self.calls += 1
        if self.calls >= 2:
            self.both_admitted.set()
        # Hold the first call open until a second one is admitted (or a second
        # passes): the two users' admissions then genuinely overlap.
        with __import__("contextlib").suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.both_admitted.wait(), timeout=1.0)
        prompt_chars = sum(len(m.content) for m in messages)
        return ModelResult(content=final("paid answer"), prompt_tokens=prompt_chars // 4 + 1,
                           completion_tokens=1024)

    async def health(self) -> bool:
        return True


async def test_two_users_cannot_both_spend_a_budget_with_room_for_one(make_harness):
    """13 §0 / US-T3 under concurrency: admission counts calls already admitted,
    not only calls already committed to the ledger."""

    paid = _PaidModel()
    h = await make_harness(
        config={"agent": {"provider": "openai_compatible", "model": "paid-worker",
                          "endpoint": "https://llm.test/v1", "pricing": PAID,
                          "bounds": {"per_task_budget": 100.0}},
                "security": {"budgets": {"per_user_daily_cost_limit": 100.0,
                                         "global_daily_cost_limit": 1.5}}},
        models={"paid-worker": paid},
    )
    alice, bob = await h.user("alice"), await h.user("bob")

    results = await asyncio.gather(h.submit(alice, "one question"), h.submit(bob, "one question"))

    spent = sum(u.estimated_cost for u in await h.rows(UsageEvent) if u.kind is UsageKind.MODEL_CALL)
    outcomes = sorted(r.status_code for r in results)
    assert paid.calls == 1, f"{paid.calls} paid calls were made against a budget with room for one"
    assert spent <= 1.5, spent
    assert outcomes == [200, 429], [r.text for r in results]
    refused = next(r for r in results if r.status_code == 429)
    assert refused.json()["error"]["details"]["failure_code"] == "budget_exceeded"


async def test_ten_users_at_once_leave_consistent_task_rows(make_harness):
    h = await make_harness()
    users = [await h.user(f"user-{i}") for i in range(10)]

    async def respond(messages):
        await asyncio.sleep(0.2)
        return final("ok")

    h.model.push(*[respond for _ in range(40)])

    async def served(u):
        for attempt in range(40):
            resp = await h.submit(u, f"hello from {u.subject}")
            if resp.status_code != 429:  # the global cap of 8 is explicit back-pressure
                return resp
            await asyncio.sleep(0.05 * (attempt + 1))
        raise AssertionError("never served")

    responses = await asyncio.gather(*(served(u) for u in users))

    assert all(r.status_code == 200 for r in responses), [r.text for r in responses if r.status_code != 200]
    rows = {row.task_id: row for row in await h.rows(AgentTask)}
    assert len(rows) == 10
    for resp in responses:
        row = rows[uuid.UUID(resp.json()["task_id"])]
        assert (row.status, row.model_calls, row.iterations, row.response) == ("completed", 1, 1, "ok")
        assert row.finished_at is not None
    by_user = {row.user_id for row in rows.values()}
    assert by_user == {u.user_id for u in users}
    assert len([u for u in await h.rows(UsageEvent) if u.kind is UsageKind.MODEL_CALL]) == 10


async def _transient_error(h) -> BaseException:
    """A genuine transient conflict from the store in use: a PostgreSQL deadlock,
    or SQLite's busy single writer."""

    engine = h.storage.engine
    if engine.dialect.name == "postgresql":
        async with engine.begin() as setup:
            await setup.execute(text("CREATE TABLE deadlock_probe (id int primary key, v int)"))
            await setup.execute(text("INSERT INTO deadlock_probe VALUES (1, 0), (2, 0)"))
        a, b = await engine.connect(), await engine.connect()
        try:
            await a.begin()
            await b.begin()
            await a.execute(text("UPDATE deadlock_probe SET v = 1 WHERE id = 1"))
            await b.execute(text("UPDATE deadlock_probe SET v = 1 WHERE id = 2"))
            results = await asyncio.gather(
                a.execute(text("UPDATE deadlock_probe SET v = 2 WHERE id = 2")),
                b.execute(text("UPDATE deadlock_probe SET v = 2 WHERE id = 1")),
                return_exceptions=True,
            )
            errors = [r for r in results if isinstance(r, BaseException)]
            assert len(errors) == 1, results
            return errors[0]
        finally:
            await a.close()
            await b.close()
    from sqlalchemy.ext.asyncio import create_async_engine

    impatient = create_async_engine(str(engine.url), connect_args={"timeout": 0.1})
    holder = await engine.connect()
    try:
        await holder.begin()
        await holder.execute(text("CREATE TABLE lock_probe (x INTEGER)"))  # takes the write lock
        try:
            async with impatient.begin() as conn:
                await conn.execute(text("CREATE TABLE lock_probe_2 (x INTEGER)"))
        except Exception as exc:  # noqa: BLE001
            return exc
        raise AssertionError("the second writer was not refused")
    finally:
        await holder.rollback()
        await holder.close()
        await impatient.dispose()


async def test_a_transient_store_conflict_is_a_retryable_503(make_harness):
    h = await make_harness()
    exc = await _transient_error(h)
    assert is_transient_store_error(exc), repr(exc)

    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom")
    async def boom():
        raise exc

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.get("/boom")
    assert resp.status_code == 503, resp.text
    body = resp.json()["error"]
    assert body["retryable"] is True and body["details"] == {"dependency": "storage"}
    assert str(h.storage.engine.url.database) not in resp.text  # the store is never named
