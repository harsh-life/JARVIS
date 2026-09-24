"""The circuit breaker — `docs/18_SUPERVISORY_RUNTIME_RECOVERY_CONTROL.md` §5 (build unit U1).

Task-scoped trips from the three deterministic in-task triggers, the one-way
rule, the enforcement sequence, and token invalidation at every task end.

| Hook | Test |
|---|---|
| SUP-T8 (deterministic triggers; no Judge exists) | `test_repeated_authorization_denials_trip_the_breaker`, `test_repeated_boundary_violations_trip_the_breaker`, `test_repeated_confirmation_rejections_trip_the_breaker` |
| SUP-T9 (a trip cannot be reversed) | `test_a_tripped_task_cannot_be_confirmed_or_resumed`, `test_the_breaker_exposes_no_way_back` |
| SUP-T10 (nothing pending is replayed; no auto-resume) | `test_a_tripped_task_cannot_be_confirmed_or_resumed` |
| SUP-T12 (trip audited, no content) | `test_repeated_authorization_denials_trip_the_breaker` |
| 18 §5.3 step 3 (tokens invalidated) | `test_every_way_a_task_ends_spends_its_confirmation_tokens` |

Operator stops, the global latch (18 §5.4) and the Judge trigger (19 §6) are
later units and are deliberately not exercised here.
"""

from __future__ import annotations

import uuid

import pytest

from server.agent.breaker import (
    BOUNDARY_VIOLATION_CODES,
    BreakerLimits,
    BreakerScope,
    CircuitBreaker,
)
from server.agent.state import TaskState, TaskStateRegistry
from server.config.schema import AgentBreakerConfig
from server.gateway.routers.agent import _FAILURE_CODES
from server.security.events import AuditAction
from server.storage.models import AgentTask, AuditEvent, ConfirmationToken, UsageEvent
from shared.schemas.agent import AgentFailureCode
from shared.schemas.authorization import Principal
from shared.schemas.enums import AuditActor, AuditResult, UsageKind, Visibility
from shared.schemas.execution import ExecutionErrorCode
from tests.runtime.conftest import ask, call, failure_of, final, pending_of

pytestmark = pytest.mark.asyncio

PKG = {"package_name": "com.example"}


def _state() -> TaskState:
    principal = Principal(user_id=uuid.uuid4(), device_id=uuid.uuid4(), session_id=uuid.uuid4())
    return TaskState(task_id=uuid.uuid4(), principal=principal, graph_id=None)


async def _audit(h, action: AuditAction) -> list[AuditEvent]:
    return [r for r in await h.rows(AuditEvent) if r.action == action.value]


async def _tokens(h) -> list[ConfirmationToken]:
    return await h.rows(ConfirmationToken)


def _stopped(resp) -> dict:
    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    assert error["code"] == "conflict"
    assert error["retryable"] is False  # a stopped task is never auto-retried (18 §6)
    assert failure_of(resp) == AgentFailureCode.EMERGENCY_STOP.value
    return error


# ── SUP-T8: each deterministic trigger stops the task ──────────────────────


async def test_repeated_authorization_denials_trip_the_breaker(make_harness):
    """Grinding against the engine: every read of another user's private file is
    denied (D4). At `denial_limit` the task is stopped, and the model is never
    asked for another step."""

    h = await make_harness(config={"agent": {"breaker": {"denial_limit": 3}}})
    alice, bob = await h.user("alice"), await h.user("bob")
    bobs = await h.file(bob, None, Visibility.PRIVATE, "diary.txt")
    await h.grant(alice, "file.read")
    h.model.push(
        ask("file.read"),
        *[call("files.read", "read_file", ref=bobs) for _ in range(3)],
        final("never reached"),
    )

    error = _stopped(await h.submit(alice))

    assert "refused by authorization" in error["message"]
    assert h.reads.calls == []
    assert len(h.model.script) == 1  # the step after the trip was never requested
    [row] = await h.rows(AgentTask)
    assert (row.status, row.failure_code) == ("failed", "emergency_stop")

    [tripped] = await _audit(h, AuditAction.BREAKER_TRIPPED)
    assert tripped.actor == AuditActor.SYSTEM.value
    assert tripped.result == AuditResult.BLOCKED.value
    assert tripped.resource == f"breaker:task:{row.task_id}:denial_limit"
    assert tripped.user_id == alice.user_id
    assert "diary" not in tripped.resource  # identifiers only, no task content
    assert len(await _audit(h, AuditAction.AGENT_TASK_FAILED)) == 1


async def test_repeated_boundary_violations_trip_the_breaker(make_harness, tmp_path):
    """Acceptance case F's hostile probes, at the default `violation_limit` (3):
    the third boundary refusal stops the task and the remaining probes never
    reach the execution layer."""

    h = await make_harness(
        config={"execution": {
            "filesystem": {"base_root": str(tmp_path / "sandboxes")},
            "network": {"default_internet": True},
        }},
        use_real_execution_tools=True,
    )
    alice = await h.user("alice")
    notes = {"sandbox_root": "notes"}
    await h.grant(alice, "file.read", resource_scope=notes)
    await h.grant(alice, "net.request")
    h.model.push(
        ask("file.read", scope=notes),
        ask("net.request"),
        call("files.read", "read_file", args={"relative_path": "../../../../../etc/passwd"}),
        call("net.request", "get", args={"url": "http://169.254.169.254/latest/meta-data/"}),
        call("net.request", "get", args={"url": "http://127.0.0.1:8000/api/v1/health"}),
        call("net.request", "get", args={"url": "file:///etc/passwd"}),
        call("files.read", "read_file", args={"relative_path": "/etc/passwd"}),
        final("never reached"),
    )

    error = _stopped(await h.submit(alice))

    assert "sandbox, network, or executable boundary" in error["message"]
    failed = await _audit(h, AuditAction.AGENT_TOOL_FAILED)
    assert len(failed) == 3  # probes four and five never ran
    assert len([u for u in await h.rows(UsageEvent) if u.kind is UsageKind.TOOL_CALL]) == 3
    assert len(h.model.script) == 3
    [tripped] = await _audit(h, AuditAction.BREAKER_TRIPPED)
    assert tripped.resource.endswith(":violation_limit")


async def test_repeated_confirmation_rejections_trip_the_breaker(h):
    """A worker that keeps proposing the action the user keeps declining is
    stopped at `rejection_limit`, instead of being handed the task back again."""

    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    retry = call("ui.app", "input_text", args={"text": "send it"}, platform="android")
    h.model.push(ask("app.interact", scope=PKG), retry, retry, retry, final("never reached"))

    paused = pending_of(await h.submit(alice))
    for _ in range(2):
        resp = await h.confirm(alice, paused["task_id"], paused["confirmation_token"], approve=False)
        paused = pending_of(resp)  # the task resumed and paused on the next proposal
    error = _stopped(await h.confirm(alice, paused["task_id"], paused["confirmation_token"],
                                     approve=False))

    assert "declined too many" in error["message"]
    assert h.ui.calls == []
    assert len(h.model.script) == 1
    assert len(await _audit(h, AuditAction.CONFIRMATION_REJECTED)) == 3
    assert all(t.used_at is not None for t in await _tokens(h))


async def test_only_boundary_refusals_count_as_violations():
    """Ordinary operational failures are not boundary violations: counting them
    would stop honest tasks on a flaky network."""

    breaker = CircuitBreaker(BreakerLimits(violation_limit=1), TaskStateRegistry())
    state = _state()
    for code in ExecutionErrorCode:
        if code.value not in BOUNDARY_VIOLATION_CODES:
            breaker.record_tool_outcome(state, ok=False, error=code.value)
    breaker.record_tool_outcome(state, ok=True, error=None)
    breaker.record_tool_outcome(state, ok=False, error="cancelled")
    assert (state.violations, state.tripped) == (0, None)

    assert BOUNDARY_VIOLATION_CODES == {
        "sandbox_violation", "forbidden_path", "egress_denied", "unauthorized_executable",
    }
    breaker.record_tool_outcome(state, ok=False, error=ExecutionErrorCode.EGRESS_DENIED.value)
    assert state.tripped is not None and state.tripped.source == "violation_limit"
    assert state.cancel_event.is_set()


# ── SUP-T9 / SUP-T10: one-way ──────────────────────────────────────────────


async def test_a_tripped_task_cannot_be_confirmed_or_resumed(h):
    """A trip on a paused task: the valid, unexpired token no longer approves
    anything, the pending consequential action is never performed, and the task
    stays terminal."""

    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    h.model.push(
        ask("app.interact", scope=PKG),
        call("ui.app", "input_text", args={"text": "pay now"}, platform="android"),
        final("never reached"),
    )
    paused = pending_of(await h.submit(alice))
    task_id = uuid.UUID(paused["task_id"])

    h.runtime.breaker.trip(BreakerScope.TASK, task_id, reason="test_stop", source="test_signal")
    first = h.runtime.states.get(task_id).tripped
    # Idempotent, first trip wins: a later signal changes nothing.
    h.runtime.breaker.trip(BreakerScope.TASK, task_id, reason="other", source="other")
    assert h.runtime.states.get(task_id).tripped is first

    _stopped(await h.confirm(alice, paused["task_id"], paused["confirmation_token"]))

    assert h.ui.calls == []  # the pending action was not replayed
    assert len(h.model.script) == 1  # and the task was not resumed
    assert h.runtime.states.get(task_id) is None
    assert all(t.used_at is not None for t in await _tokens(h))
    [tripped] = await _audit(h, AuditAction.BREAKER_TRIPPED)
    assert tripped.resource == f"breaker:task:{task_id}:test_signal:test_stop"

    # Terminal: the same token, again, is refused; the task reads as stopped.
    again = await h.confirm(alice, paused["task_id"], paused["confirmation_token"])
    assert again.status_code == 409
    assert h.ui.calls == []
    fetched = (await h.get(alice, paused["task_id"])).json()
    assert (fetched["status"], fetched["failure"]["code"]) == ("failed", "emergency_stop")


async def test_a_tripped_task_is_stopped_whatever_the_confirm_call_carries(h):
    """The stop does not depend on the caller presenting a valid token or
    declining: a wrong token on a tripped task ends it too, rather than leaving
    it paused for another attempt."""

    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    h.model.push(ask("app.interact", scope=PKG),
                 call("ui.app", "input_text", args={"text": "x"}, platform="android"))
    paused = pending_of(await h.submit(alice))
    task_id = uuid.UUID(paused["task_id"])
    h.runtime.breaker.trip(BreakerScope.TASK, task_id, reason="test_stop", source="test_signal")

    _stopped(await h.confirm(alice, paused["task_id"], "not-the-token"))

    assert h.runtime.states.get(task_id) is None
    assert await _audit(h, AuditAction.CONFIRMATION_REJECTED) == []  # not counted as a user decline
    assert all(t.used_at is not None for t in await _tokens(h))
    assert h.ui.calls == []


async def test_the_breaker_exposes_no_way_back():
    """18 §5.2: nothing on the breaker resumes, clears, grants, or authorizes."""

    public = {n for n in dir(CircuitBreaker) if not n.startswith("_")}
    assert public == {"trip", "record_denial", "record_tool_outcome", "record_rejection", "limits"}

    registry = TaskStateRegistry()
    state = _state()
    registry.put(state)
    breaker = CircuitBreaker(BreakerLimits(), registry)
    breaker.trip(BreakerScope.TASK, state.task_id, reason="first", source="first")
    breaker.trip(BreakerScope.TASK, state.task_id, reason="second", source="second")
    assert state.tripped.reason == "first"

    # A task that is not live is a no-op, not an error.
    breaker.trip(BreakerScope.TASK, uuid.uuid4(), reason="gone", source="gone")


async def test_trip_reasons_are_identifiers_never_prose():
    """The audit trail has no free-form payload (SECRET-004); a trip reason that
    could carry task text is refused."""

    breaker = CircuitBreaker(BreakerLimits(), TaskStateRegistry())
    for bad in ("", "Has Spaces", "x" * 40, "token=sk-abc", "UPPER"):
        with pytest.raises(ValueError):
            breaker.trip(BreakerScope.TASK, uuid.uuid4(), reason=bad, source="ok")
        with pytest.raises(ValueError):
            breaker.trip(BreakerScope.TASK, uuid.uuid4(), reason="ok", source=bad)


# ── 18 §5.3 step 3: no token outlives its task ─────────────────────────────


async def test_every_way_a_task_ends_spends_its_confirmation_tokens(h):
    """Before U1 a cancelled or lost paused task left its token row unused until
    it expired. Nothing could consume it — but "unusable because nothing calls
    it" is a property of today's callers; a spent row is a property of the
    data."""

    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)

    async def paused_task() -> dict:
        h.model.push(ask("app.interact", scope=PKG),
                     call("ui.app", "input_text", args={"text": "x"}, platform="android"))
        return pending_of(await h.submit(alice))

    async def unused_for(task_id: str) -> list[ConfirmationToken]:
        return [t for t in await _tokens(h) if t.task_id == task_id and t.used_at is None]

    # cancelled while paused
    cancelled = await paused_task()
    assert await unused_for(cancelled["task_id"])
    resp = await h.client.post(f"/api/v1/agent/tasks/{cancelled['task_id']}/cancel", headers=alice.auth)
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
    assert await unused_for(cancelled["task_id"]) == []

    # paused state lost (a restart), then confirmed
    lost = await paused_task()
    h.runtime.states.pop(uuid.UUID(lost["task_id"]))
    resp = await h.confirm(alice, lost["task_id"], lost["confirmation_token"])
    assert failure_of(resp) == "confirmation_state_lost"
    assert await unused_for(lost["task_id"]) == []

    # confirmation expired
    expired = await paused_task()
    from datetime import datetime, timedelta, timezone

    h.runtime.states.get(uuid.UUID(expired["task_id"])).pending.expires_at = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    resp = await h.confirm(alice, expired["task_id"], expired["confirmation_token"])
    assert failure_of(resp) == "confirmation_expired"
    assert await unused_for(expired["task_id"]) == []
    assert h.ui.calls == []


async def test_invalidation_is_scoped_to_one_task_and_one_owner(h):
    """Ending one task spends only that task's tokens — never another task's,
    and never another user's."""

    alice, bob = await h.user("alice"), await h.user("bob")
    for actor in (alice, bob):
        await h.grant(actor, "app.interact", resource_scope=PKG)

    async def paused(actor) -> dict:
        h.model.push(ask("app.interact", scope=PKG),
                     call("ui.app", "input_text", args={"text": "x"}, platform="android"))
        return pending_of(await h.submit(actor))

    a1, a2, b1 = await paused(alice), await paused(alice), await paused(bob)
    resp = await h.client.post(f"/api/v1/agent/tasks/{a1['task_id']}/cancel", headers=alice.auth)
    assert resp.status_code == 200

    unused = {t.task_id for t in await _tokens(h) if t.used_at is None}
    assert unused == {a2["task_id"], b1["task_id"]}
    # And the survivors still work.
    resp = await h.confirm(bob, b1["task_id"], b1["confirmation_token"])
    assert resp.status_code == 200, resp.text
    assert [c.user_id for c in h.ui.calls] == [bob.user_id]


# ── configuration and API surface ──────────────────────────────────────────


async def test_breaker_defaults_and_bounds():
    config = AgentBreakerConfig()
    assert (config.denial_limit, config.violation_limit, config.rejection_limit) == (5, 3, 3)
    assert BreakerLimits() == BreakerLimits(5, 3, 3)
    for field in ("denial_limit", "violation_limit", "rejection_limit"):
        with pytest.raises(ValueError):
            AgentBreakerConfig(**{field: 0})
        with pytest.raises(ValueError):
            CircuitBreaker(BreakerLimits(**{field: 0}), TaskStateRegistry())


async def test_configured_limits_reach_the_runtime(make_harness):
    h = await make_harness(config={"agent": {"breaker": {"denial_limit": 7, "rejection_limit": 2}}})
    assert h.runtime.breaker.limits == BreakerLimits(denial_limit=7, violation_limit=3, rejection_limit=2)


async def test_every_failure_code_has_an_http_mapping_and_a_message():
    from server.agent.runtime import _FAILURE_MESSAGES

    assert set(_FAILURE_CODES) == set(AgentFailureCode)
    assert set(_FAILURE_MESSAGES) == set(AgentFailureCode)
