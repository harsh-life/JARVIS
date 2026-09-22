"""RT-T1/RT-T2/RT-T6 and this branch's own instructions: the loop's core
behaviors, exercised against the *real* authorization engine.
"""

from __future__ import annotations

import json

import pytest

from server.agent.orchestrator import AgentOrchestrator
from server.agent.tasks import TaskStore
from server.memory.hydrator import NullMemoryHydrator
from shared.schemas.enums import CapabilityScopeType
from shared.schemas.runtime import FailureReason, ModelUnavailable, TaskStatus

from tests.runtime.conftest import (
    RecordingToolExecutor,
    ScriptedModelInvoker,
    build_event_recorder,
    build_registry,
    build_tool_catalog,
    build_tool_dispatcher,
    principal_for,
)

pytestmark = pytest.mark.asyncio


def _tool_call(tool_id="notes", capability="file.write", operation="write_file", **kw):
    return json.dumps(
        {"kind": "tool_call", "tool_id": tool_id, "capability": capability, "operation": operation, **kw}
    )


def _final(text="done"):
    return json.dumps({"kind": "final_answer", "final_text": text})


def make_orchestrator(
    *, principal, authorizer, db, audit, bounds, model_responses, executor=None, allowed_tool_ids=None, tool_id="notes"
):
    executor = executor or RecordingToolExecutor()
    registry = build_registry(tool_id=tool_id, capability="file.write", executor=executor)
    allowed = allowed_tool_ids if allowed_tool_ids is not None else frozenset({tool_id})
    return AgentOrchestrator(
        principal=principal,
        bounds=bounds,
        authorizer=authorizer,
        model=ScriptedModelInvoker(responses=list(model_responses)),
        tools=build_tool_dispatcher(registry=registry, allowed_tool_ids=allowed, db=db, audit=audit, principal=principal),
        memory=NullMemoryHydrator(),
        events=build_event_recorder(audit=audit, principal=principal),
        tool_catalog=build_tool_catalog(registry=registry, allowed_tool_ids=allowed),
        task_store=TaskStore(),
    ), executor


async def test_valid_tool_call_proposal_executes_and_completes(alice, authorizer, db, audit, small_bounds, grants):
    principal = principal_for(alice)
    await grants.grant(
        db, principal_id=alice.user_id, scope_type=CapabilityScopeType.USER,
        capability="file.write", granted_by=alice.user_id,
    )
    orch, executor = make_orchestrator(
        principal=principal, authorizer=authorizer, db=db, audit=audit, bounds=small_bounds,
        model_responses=[_tool_call(), _final("wrote the note")],
    )
    result = await orch.start("please write a note")

    assert result.status is TaskStatus.COMPLETED
    assert result.output == "wrote the note"
    assert result.tool_calls_used == 1
    assert len(executor.requests) == 1
    assert executor.requests[0].tool_id == "notes"


async def test_final_answer_proposal_completes_immediately(alice, authorizer, db, audit, small_bounds):
    principal = principal_for(alice)
    orch, _ = make_orchestrator(
        principal=principal, authorizer=authorizer, db=db, audit=audit, bounds=small_bounds,
        model_responses=[_final("hi there")],
    )
    result = await orch.start("say hi")
    assert result.status is TaskStatus.COMPLETED
    assert result.output == "hi there"
    assert result.model_calls_used == 1
    assert result.tool_calls_used == 0


async def test_malformed_proposal_bounded_retry_then_fail(alice, authorizer, db, audit, small_bounds):
    principal = principal_for(alice)
    # small_bounds.max_parse_retries == 1, so: 2 garbage responses exhausts it.
    orch, _ = make_orchestrator(
        principal=principal, authorizer=authorizer, db=db, audit=audit, bounds=small_bounds,
        model_responses=["not json at all", "still not json"],
    )
    result = await orch.start("do something")
    assert result.status is TaskStatus.FAILED
    assert result.failure_reason is FailureReason.MALFORMED_PROPOSAL


async def test_unknown_capability_is_denied_not_executed(alice, authorizer, db, audit, small_bounds):
    principal = principal_for(alice)
    orch, executor = make_orchestrator(
        principal=principal, authorizer=authorizer, db=db, audit=audit, bounds=small_bounds,
        model_responses=[_tool_call(capability="nonexistent.capability"), _final("gave up")],
    )
    result = await orch.start("try something odd")
    assert result.status is TaskStatus.COMPLETED
    assert result.output == "gave up"
    assert executor.requests == []  # never dispatched


async def test_unauthorized_capability_is_denied_not_executed(alice, authorizer, db, audit, small_bounds):
    """`file.write` is a real, registered capability — alice simply never
    received a grant for it. D5 must deny exactly as it would for a stranger
    calling any other protected endpoint."""

    principal = principal_for(alice)
    orch, executor = make_orchestrator(
        principal=principal, authorizer=authorizer, db=db, audit=audit, bounds=small_bounds,
        model_responses=[_tool_call(), _final("could not do it")],
    )
    result = await orch.start("write a note")
    assert result.status is TaskStatus.COMPLETED
    assert executor.requests == []


async def test_absolute_floor_capability_is_never_confirmable(alice, authorizer, db, audit, small_bounds):
    """PERM-006 — prohibition *by absence*: `superuser` is not, and cannot
    be, in `CAPABILITY_REGISTRY`, so it is denied at D5 exactly like an
    unknown capability — never offered as a confirmation prompt (RT-T4)."""

    from server.capabilities.registry import CAPABILITY_REGISTRY

    assert "superuser" not in CAPABILITY_REGISTRY

    principal = principal_for(alice)
    orch, executor = make_orchestrator(
        principal=principal, authorizer=authorizer, db=db, audit=audit, bounds=small_bounds,
        model_responses=[_tool_call(capability="superuser", operation="assume"), _final("refused")],
    )
    result = await orch.start("become superuser")
    assert result.status is TaskStatus.COMPLETED
    assert result.confirmation_token is None
    assert executor.requests == []


async def test_tool_failure_is_observed_and_the_model_may_replan(alice, authorizer, db, audit, small_bounds, grants):
    principal = principal_for(alice)
    await grants.grant(
        db, principal_id=alice.user_id, scope_type=CapabilityScopeType.USER,
        capability="file.write", granted_by=alice.user_id,
    )
    from shared.schemas.runtime import ToolResult

    executor = RecordingToolExecutor(result=ToolResult(tool_id="notes", success=False, error="disk full"))
    orch, executor = make_orchestrator(
        principal=principal, authorizer=authorizer, db=db, audit=audit, bounds=small_bounds,
        model_responses=[_tool_call(), _final("told the user it failed")],
        executor=executor,
    )
    result = await orch.start("write a note")
    assert result.status is TaskStatus.COMPLETED
    assert result.tool_calls_used == 1


async def test_provider_failure_is_an_explicit_task_failure_never_fabricated(alice, authorizer, db, audit, small_bounds):
    """FAIL-CORE-002: a model outage is an explicit failure, never a
    fabricated answer."""

    principal = principal_for(alice)
    orch, _ = make_orchestrator(
        principal=principal, authorizer=authorizer, db=db, audit=audit, bounds=small_bounds,
        model_responses=[ModelUnavailable("connection refused")],
    )
    result = await orch.start("do anything")
    assert result.status is TaskStatus.FAILED
    assert result.failure_reason is FailureReason.MODEL_UNAVAILABLE
    assert result.output is None


async def test_task_cancellation_stops_the_loop(alice, authorizer, db, audit, small_bounds, grants):
    """Cancellation is checked at the top of every iteration — a tool whose
    execution itself triggers the cancellation (as if `/cancel` had arrived
    on a concurrent request) stops the *next* iteration rather than being
    silently ignored."""

    principal = principal_for(alice)
    await grants.grant(
        db, principal_id=alice.user_id, scope_type=CapabilityScopeType.USER,
        capability="file.write", granted_by=alice.user_id,
    )

    from shared.schemas.runtime import ToolInvocationRequest, ToolResult

    task_store = TaskStore()

    class CancellingExecutor:
        def __init__(self, task_store: TaskStore) -> None:
            self._task_store = task_store
            self.requests: list[ToolInvocationRequest] = []

        async def execute(self, request: ToolInvocationRequest) -> ToolResult:
            self.requests.append(request)
            # Simulate a concurrent /cancel request arriving mid-execution.
            await self._task_store.request_cancellation(request.task_id, requested_by=principal)
            return ToolResult(tool_id=request.tool_id, success=True, output="ok")

    executor = CancellingExecutor(task_store)
    registry = build_registry(tool_id="notes", capability="file.write", executor=executor)
    orch = AgentOrchestrator(
        principal=principal,
        bounds=small_bounds,
        authorizer=authorizer,
        model=ScriptedModelInvoker(responses=[_tool_call(), _final("should never get here")]),
        tools=build_tool_dispatcher(registry=registry, allowed_tool_ids=frozenset({"notes"}), db=db, audit=audit, principal=principal),
        memory=NullMemoryHydrator(),
        events=build_event_recorder(audit=audit, principal=principal),
        tool_catalog=build_tool_catalog(registry=registry, allowed_tool_ids=frozenset({"notes"})),
        task_store=task_store,
    )
    result = await orch.start("write a note")
    assert result.status is TaskStatus.CANCELLED
    assert len(executor.requests) == 1  # the second model call never happened
