"""Runtime × bounds, rate, budget, metering (05 §3, 13, RT-T2/T8/T10, US-T*).

Every ceiling is enforced by the runtime and ends in an explicit failure with its
own code — never a silent stop and never a fabricated "done" (AGENT-003).
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from server.storage.models import AgentTask, AuditEvent, UsageEvent
from shared.schemas.enums import UsageKind, Visibility
from tests.runtime.conftest import ask, call, failure_of, final
from tests.runtime.test_secrets_and_providers import PAID, WireLog

pytestmark = pytest.mark.asyncio


def _bounds(**values) -> dict:
    return {"agent": {"bounds": values}}


async def _assert_explicit_failure(h, resp, code: str):
    assert resp.status_code == 429, resp.text
    assert failure_of(resp) == code
    task = (await h.rows(AgentTask))[-1]
    assert task.status == "failed" and task.failure_code == code
    assert task.response is None  # never a fabricated answer


async def test_rt_t2_a_runaway_agent_hits_max_iterations(make_harness):
    """RT-T2 / RT-T10: an agent that keeps proposing denied steps — replanning
    after every denial — cannot grind past the iteration ceiling."""

    h = await make_harness(config=_bounds(max_iterations=4))
    alice = await h.user("alice")
    h.model.push(*[call("files.read", "read_file", ref=str(uuid.uuid4())) for _ in range(20)])

    resp = await h.submit(alice)

    await _assert_explicit_failure(h, resp, "max_iterations_exceeded")
    assert resp.json()["error"]["details"]["task_id"]
    assert len(h.model.seen) == 4


async def test_max_tool_calls_is_enforced(make_harness):
    h = await make_harness(config=_bounds(max_tool_calls=2))
    alice = await h.user("alice")
    await h.grant(alice, "file.read")
    h.model.push(ask("file.read"), *[call("files.read", "list_directory") for _ in range(5)])

    resp = await h.submit(alice)

    await _assert_explicit_failure(h, resp, "max_tool_calls_exceeded")
    assert len(h.reads.calls) == 2


async def test_max_model_calls_is_enforced(make_harness):
    h = await make_harness(config=_bounds(max_model_calls=2, max_iterations=10))
    alice = await h.user("alice")
    h.model.push(*[call("nope", "x") for _ in range(5)])

    resp = await h.submit(alice)

    await _assert_explicit_failure(h, resp, "max_model_calls_exceeded")
    assert len(h.model.seen) == 2


async def test_the_wall_clock_bound_is_enforced(make_harness):
    h = await make_harness(config=_bounds(wall_clock_timeout_seconds=0.3))
    alice = await h.user("alice")

    async def slow(_messages):
        await asyncio.sleep(0.2)
        return call("nope", "x")

    h.model.push(*[slow for _ in range(5)])

    resp = await h.submit(alice)

    await _assert_explicit_failure(h, resp, "wall_clock_timeout")


async def test_per_user_rate_limits_contain_one_users_runaway(make_harness):
    """13 §2/§6 / US-T6: Alice exhausting her per-minute quota is refused
    explicitly — and Bob is still served."""

    h = await make_harness(config={"security": {"rate_limits": {"per_user_requests_per_minute": 3}}})
    alice, bob = await h.user("alice"), await h.user("bob")
    h.model.push(*[call("nope", "x") for _ in range(10)])

    resp = await h.submit(alice)
    await _assert_explicit_failure(h, resp, "rate_limited")
    assert len(h.model.seen) == 3

    h.model.script.clear()
    h.model.push(final("bob is fine"))
    ok = await h.submit(bob)
    assert ok.status_code == 200 and ok.json()["response"] == "bob is fine"


async def test_per_session_concurrency_is_capped(h):
    """13 §2: one running task per session (the proposed default); a second
    concurrent submission is refused explicitly, not queued forever."""

    alice = await h.user("alice")
    release = asyncio.Event()

    async def blocked(_messages):
        await release.wait()
        return final("first")

    h.model.push(blocked)

    first = asyncio.create_task(h.submit(alice, "one"))
    await asyncio.sleep(0.2)
    second = await h.submit(alice, "two")
    release.set()
    first_resp = await first

    assert second.status_code == 429
    assert second.json()["error"]["details"]["limit"] == "per_session_concurrency"
    assert first_resp.status_code == 200


async def test_a_paid_call_over_the_task_budget_is_refused_before_it_is_made(make_harness):
    """US-T3 / MP-T8 / SEC-V: refused explicitly, never silently made."""

    wire = WireLog()
    h = await make_harness(
        config={"agent": {"provider": "openai_compatible", "model": "gpt-test",
                          "endpoint": "https://llm.test/v1", "pricing": PAID}},
        transport=wire.transport,
    )
    alice = await h.user("alice")

    resp = await h.submit(alice)

    await _assert_explicit_failure(h, resp, "budget_exceeded")
    assert wire.requests == []


async def test_the_user_daily_budget_is_enforced_against_the_ledger(make_harness):
    """USAGE-002 / US-T8: the budget is a sum over `usage_events`, not a guess."""

    wire = WireLog(*[call("nope", "x") for _ in range(10)])
    h = await make_harness(
        config={"agent": {"provider": "openai_compatible", "model": "gpt-test",
                          "endpoint": "https://llm.test/v1", "pricing": PAID,
                          "bounds": {"per_task_budget": 100.0}},
                "security": {"budgets": {"per_user_daily_cost_limit": 1.3,
                                         "global_daily_cost_limit": 100.0}}},
        transport=wire.transport,
    )
    alice = await h.user("alice")

    resp = await h.submit(alice)

    # Each call costs 0.1 but is *projected* at ~1.1 (max_tokens at the output
    # price), so the precheck refuses once ledger spend + projection > 1.3.
    await _assert_explicit_failure(h, resp, "budget_exceeded")
    spent = sum(u.estimated_cost for u in await h.rows(UsageEvent))
    assert spent <= 1.3
    assert 1 <= len(wire.requests) < 10


async def test_us_t1_every_model_and_tool_call_is_metered_exactly_once(h):
    """USAGE-001 / RT-T8 / TL-T9: one UsageEvent per model call and per tool
    execution, and an AuditEvent for every tool execution."""

    alice = await h.user("alice")
    await h.grant(alice, "file.read")
    f = await h.file(alice, None, Visibility.PRIVATE, "a.txt")
    h.model.push(ask("file.read"), call("files.read", "read_file", ref=f),
                 call("files.read", "list_directory"), final())

    resp = await h.submit(alice)
    counters = resp.json()["counters"]

    usage = await h.rows(UsageEvent)
    assert len([u for u in usage if u.kind is UsageKind.MODEL_CALL]) == counters["model_calls"] == 4
    tool_usage = [u for u in usage if u.kind is UsageKind.TOOL_CALL]
    assert len(tool_usage) == counters["tool_calls"] == 2
    assert {u.tool_id for u in tool_usage} == {"files.read"}
    executed = await h.rows(AuditEvent, AuditEvent.action == "agent.tool.executed")
    assert len(executed) == 2


async def test_idempotent_submission_never_runs_twice_and_never_crosses_users(h):
    """02 §1.4 / API-T9: a retry returns the original result. The key is
    namespaced per user, so another user's identical key never replays it."""

    alice, bob = await h.user("alice"), await h.user("bob")
    h.model.push(final("alice's answer"))
    first = await h.submit(alice, "same input", key="retry-key")
    again = await h.submit(alice, "same input", key="retry-key")

    assert first.json() == again.json()
    assert len(h.model.seen) == 1

    h.model.push(final("bob's answer"))
    bobs = await h.submit(bob, "same input", key="retry-key")
    assert bobs.json()["response"] == "bob's answer"
    assert bobs.json()["task_id"] != first.json()["task_id"]

    conflict = await h.submit(alice, "different input", key="retry-key")
    assert conflict.status_code == 409


async def test_submission_requires_an_idempotency_key(h):
    alice = await h.user("alice")
    resp = await h.client.post("/api/v1/agent/tasks", json={"input": "x"}, headers=alice.auth)
    assert resp.status_code == 422


async def test_unauthenticated_requests_never_reach_the_runtime(h):
    """API-T1."""

    resp = await h.client.post("/api/v1/agent/tasks", json={"input": "x"},
                               headers={"Idempotency-Key": "k"})
    assert resp.status_code == 401
    assert h.model.seen == []
