"""RT-T2, RATE-001, 05 §3 — a runaway agent must be impossible.

`[LOCKED]`: breach of any ceiling is an explicit failure, never a silent
stop and never a fabricated "done" — every test here asserts `FAILED` with
the specific reason, not merely "not completed".
"""

from __future__ import annotations

import json

import pytest

from server.agent.orchestrator import AgentOrchestrator
from server.agent.tasks import TaskStore
from server.memory.hydrator import NullMemoryHydrator
from shared.schemas.enums import CapabilityScopeType
from shared.schemas.runtime import FailureReason, RuntimeBounds, TaskStatus, ToolResult

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


def _tool_call():
    return json.dumps(
        {"kind": "tool_call", "tool_id": "notes", "capability": "file.write", "operation": "write_file"}
    )


def _final(text="done"):
    return json.dumps({"kind": "final_answer", "final_text": text})


def _model_tool_call(model_tool_id="summarizer"):
    return json.dumps({"kind": "model_tool_call", "model_tool_id": model_tool_id, "prompt": "summarize this"})


async def _grant(grants, db, user, capability, resource_scope=None):
    await grants.grant(
        db, principal_id=user.user_id, scope_type=CapabilityScopeType.USER,
        capability=capability, granted_by=user.user_id, resource_scope=resource_scope,
    )


def _build(principal, authorizer, db, audit, bounds, model_responses, executor=None, tool_id="notes"):
    executor = executor or RecordingToolExecutor()
    registry = build_registry(tool_id=tool_id, capability="file.write", executor=executor)
    return AgentOrchestrator(
        principal=principal,
        bounds=bounds,
        authorizer=authorizer,
        model=ScriptedModelInvoker(responses=list(model_responses)),
        tools=build_tool_dispatcher(registry=registry, allowed_tool_ids=frozenset({tool_id}), db=db, audit=audit, principal=principal),
        memory=NullMemoryHydrator(),
        events=build_event_recorder(audit=audit, principal=principal),
        tool_catalog=build_tool_catalog(registry=registry, allowed_tool_ids=frozenset({tool_id})),
        task_store=TaskStore(),
    ), executor


async def test_runaway_iterations_stops_explicitly(alice, authorizer, db, audit, grants):
    await _grant(grants, db, alice, "file.write")
    principal = principal_for(alice)
    bounds = RuntimeBounds(max_iterations=2, max_tool_calls=100, max_model_calls=100)
    # An infinite stream of tool calls the model never stops proposing.
    orch, executor = _build(principal, authorizer, db, audit, bounds, [_tool_call()] * 10)

    result = await orch.start("keep writing notes forever")

    assert result.status is TaskStatus.FAILED
    assert result.failure_reason is FailureReason.RUNAWAY_ITERATIONS
    assert result.iterations_used <= bounds.max_iterations


async def test_runaway_tool_calls_stops_explicitly(alice, authorizer, db, audit, grants):
    await _grant(grants, db, alice, "file.write")
    principal = principal_for(alice)
    bounds = RuntimeBounds(max_iterations=100, max_tool_calls=2, max_model_calls=100)
    orch, executor = _build(principal, authorizer, db, audit, bounds, [_tool_call()] * 10)

    result = await orch.start("write many notes")

    assert result.status is TaskStatus.FAILED
    assert result.failure_reason is FailureReason.RUNAWAY_TOOL_CALLS
    assert result.tool_calls_used <= bounds.max_tool_calls


async def test_runaway_model_calls_stops_explicitly(alice, authorizer, db, audit):
    principal = principal_for(alice)
    bounds = RuntimeBounds(max_iterations=100, max_tool_calls=100, max_model_calls=2)
    # Never a tool call, just a model that never gives a final answer either
    # — malformed output would be caught by parse-retry first, so use valid
    # tool_call proposals against a tool the principal has no grant for
    # (denied -> observation -> replan -> another model call, forever).
    orch, executor = _build(principal, authorizer, db, audit, bounds, [_tool_call()] * 10)

    result = await orch.start("keep trying")

    assert result.status is TaskStatus.FAILED
    assert result.failure_reason is FailureReason.RUNAWAY_MODEL_CALLS
    assert result.model_calls_used <= bounds.max_model_calls


async def test_replanning_after_a_denial_cannot_exceed_any_bound(alice, authorizer, db, audit):
    """RT-T10: the model may replan after a denial (05 §6), but replanning
    consumes the same iteration/model-call budget — it cannot be used to
    grind past a ceiling."""

    principal = principal_for(alice)
    bounds = RuntimeBounds(max_iterations=3, max_tool_calls=100, max_model_calls=100)
    orch, executor = _build(principal, authorizer, db, audit, bounds, [_tool_call()] * 20)

    result = await orch.start("keep trying forever")

    assert result.status is TaskStatus.FAILED
    assert result.iterations_used == bounds.max_iterations
    assert executor.requests == []  # every attempt was denied (no grant)


async def test_budget_exceeded_stops_explicitly(alice, authorizer, db, audit, grants):
    await _grant(grants, db, alice, "file.write")
    principal = principal_for(alice)
    bounds = RuntimeBounds(max_iterations=100, max_tool_calls=100, max_model_calls=100, max_cost=0.05)
    executor = RecordingToolExecutor(result=ToolResult(tool_id="notes", success=True, output="ok", estimated_cost=0.10))
    orch, executor = _build(principal, authorizer, db, audit, bounds, [_tool_call()] * 10, executor=executor)

    result = await orch.start("write a note")

    assert result.status is TaskStatus.FAILED
    assert result.failure_reason is FailureReason.BUDGET_EXCEEDED
    assert result.tool_calls_used == 1  # the one call that pushed it over is the last one that ran


async def test_model_tool_nesting_depth_is_capped(alice, authorizer, db, audit, grants):
    """RT-T7/MP-T7: a model-tool call beyond the configured nesting depth is
    denied by the runtime itself — never reaching the dispatcher — while the
    task is still free to finish normally afterwards."""

    await _grant(grants, db, alice, "model_tool.invoke", resource_scope={"model_tool_id": "summarizer"})
    principal = principal_for(alice)
    bounds = RuntimeBounds(max_iterations=5, max_tool_calls=5, max_model_calls=5, max_model_tool_nesting_depth=0)

    executor = RecordingToolExecutor(result=ToolResult(tool_id="summarizer", success=True, output="summary"))
    registry = build_registry(tool_id="summarizer", capability="model_tool.invoke", executor=executor)
    orch = AgentOrchestrator(
        principal=principal,
        bounds=bounds,
        authorizer=authorizer,
        model=ScriptedModelInvoker(responses=[_model_tool_call(), _final("gave up on nesting")]),
        tools=build_tool_dispatcher(registry=registry, allowed_tool_ids=frozenset({"summarizer"}), db=db, audit=audit, principal=principal),
        memory=NullMemoryHydrator(),
        events=build_event_recorder(audit=audit, principal=principal),
        tool_catalog=build_tool_catalog(registry=registry, allowed_tool_ids=frozenset({"summarizer"})),
        task_store=TaskStore(),
    )

    result = await orch.start("summarize this for me")

    assert result.status is TaskStatus.COMPLETED
    assert executor.requests == []  # the nesting cap denied it before dispatch
