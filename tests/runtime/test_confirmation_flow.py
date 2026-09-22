"""RT-T3, PERM-004, 05 §4 — confirmation is a pause, not a suggestion.

`[LOCKED]` (05 §4): no timeout auto-approves; the action never happens
without a valid, single-use, exactly-matching token.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from server.agent.orchestrator import AgentOrchestrator
from server.agent.tasks import TaskConflict, TaskStore
from server.memory.hydrator import NullMemoryHydrator
from shared.schemas.enums import CapabilityScopeType
from shared.schemas.runtime import TaskStatus

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


def _delete_call():
    # `delete_file` is CONSEQUENTIAL in the real registry (server/capabilities/
    # registry.py) — always require_confirmation, never automatic.
    return json.dumps(
        {
            "kind": "tool_call",
            "tool_id": "notes",
            "capability": "file.write",
            "operation": "delete_file",
            "arguments": {"path": "a.txt"},
        }
    )


def _final(text="done"):
    return json.dumps({"kind": "final_answer", "final_text": text})


async def _grant_file_write(grants, db, alice):
    await grants.grant(
        db, principal_id=alice.user_id, scope_type=CapabilityScopeType.USER,
        capability="file.write", granted_by=alice.user_id,
    )


def _make(principal, authorizer, db, audit, bounds, model_responses, executor=None):
    executor = executor or RecordingToolExecutor()
    registry = build_registry(tool_id="notes", capability="file.write", executor=executor)
    task_store = TaskStore()
    orch = AgentOrchestrator(
        principal=principal,
        bounds=bounds,
        authorizer=authorizer,
        model=ScriptedModelInvoker(responses=list(model_responses)),
        tools=build_tool_dispatcher(registry=registry, allowed_tool_ids=frozenset({"notes"}), db=db, audit=audit, principal=principal),
        memory=NullMemoryHydrator(),
        events=build_event_recorder(audit=audit, principal=principal),
        tool_catalog=build_tool_catalog(registry=registry, allowed_tool_ids=frozenset({"notes"})),
        task_store=task_store,
    )
    return orch, executor


async def test_consequential_action_pauses_for_confirmation(alice, authorizer, db, audit, small_bounds, grants):
    await _grant_file_write(grants, db, alice)
    principal = principal_for(alice)
    orch, executor = _make(principal, authorizer, db, audit, small_bounds, [_delete_call()])

    result = await orch.start("delete the note")

    assert result.status is TaskStatus.AWAITING_CONFIRMATION
    assert result.confirmation_token is not None
    assert executor.requests == []  # not performed yet


async def test_confirmation_accepted_executes_the_pending_action(alice, authorizer, db, audit, small_bounds, grants):
    await _grant_file_write(grants, db, alice)
    principal = principal_for(alice)
    orch, executor = _make(
        principal, authorizer, db, audit, small_bounds,
        [_delete_call(), _final("deleted")],
    )

    paused = await orch.start("delete the note")
    assert paused.status is TaskStatus.AWAITING_CONFIRMATION

    result = await orch.resume(paused.task_id, confirmation_token=paused.confirmation_token, approve=True)

    assert result.status is TaskStatus.COMPLETED
    assert result.output == "deleted"
    assert len(executor.requests) == 1


async def test_confirmation_rejected_does_not_execute(alice, authorizer, db, audit, small_bounds, grants):
    await _grant_file_write(grants, db, alice)
    principal = principal_for(alice)
    orch, executor = _make(
        principal, authorizer, db, audit, small_bounds,
        [_delete_call(), _final("okay, not deleted")],
    )

    paused = await orch.start("delete the note")
    result = await orch.resume(paused.task_id, confirmation_token=paused.confirmation_token, approve=False)

    assert result.status is TaskStatus.COMPLETED
    assert result.output == "okay, not deleted"
    assert executor.requests == []  # never performed


async def test_a_task_awaiting_confirmation_that_is_never_confirmed_never_executes(
    alice, authorizer, db, audit, small_bounds, grants
):
    """05 §4: "If the human never confirms, the action never happens; the
    task can be cancelled or expire *without* performing the action." This
    test simply never calls `/confirm` at all."""

    await _grant_file_write(grants, db, alice)
    principal = principal_for(alice)
    orch, executor = _make(principal, authorizer, db, audit, small_bounds, [_delete_call()])

    result = await orch.start("delete the note")
    assert result.status is TaskStatus.AWAITING_CONFIRMATION
    assert executor.requests == []

    status_result = await orch.get_status(result.task_id)
    assert status_result.status is TaskStatus.AWAITING_CONFIRMATION
    assert executor.requests == []


async def test_expired_confirmation_token_is_treated_as_not_performed(
    alice, authorizer, db, audit, small_bounds, grants
):
    """`[LOCKED]` 05 §4: expiry is a denial, never an approval — even when
    the human *did* click approve, an elapsed deadline means "not
    performed", exactly like `server/capabilities/confirmation.py`'s own
    `consume()` guarantees at the lower level."""

    from server.secrets.crypto import hash_token
    from server.storage.models import ConfirmationToken

    await _grant_file_write(grants, db, alice)
    principal = principal_for(alice)
    orch, executor = _make(
        principal, authorizer, db, audit, small_bounds,
        [_delete_call(), _final("gave up, expired")],
    )

    paused = await orch.start("delete the note")
    token = paused.confirmation_token
    assert token is not None

    row = await db.get(ConfirmationToken, hash_token(token))
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db.flush()

    result = await orch.resume(paused.task_id, confirmation_token=token, approve=True)

    assert result.status is TaskStatus.COMPLETED
    assert result.output == "gave up, expired"
    assert executor.requests == []  # the expired approval never ran anything


async def test_resume_on_a_task_not_awaiting_confirmation_is_a_conflict(alice, authorizer, db, audit, small_bounds):
    principal = principal_for(alice)
    orch, _ = _make(principal, authorizer, db, audit, small_bounds, [_final("done already")])

    result = await orch.start("say hi")
    assert result.status is TaskStatus.COMPLETED

    with pytest.raises(TaskConflict):
        await orch.resume(result.task_id, confirmation_token="whatever", approve=True)


async def test_a_confirmation_token_is_single_use_even_through_the_runtime(
    alice, authorizer, db, audit, small_bounds, grants
):
    """Replaying an already-spent token through `/confirm` a second time
    must not re-execute the action (mirrors `test_confirmation.py`'s
    single-use guarantee, now proven at the runtime boundary)."""

    await _grant_file_write(grants, db, alice)
    principal = principal_for(alice)
    orch, executor = _make(
        principal, authorizer, db, audit, small_bounds,
        [_delete_call(), _final("deleted"), _final("should not reach here")],
    )

    paused = await orch.start("delete the note")
    first = await orch.resume(paused.task_id, confirmation_token=paused.confirmation_token, approve=True)
    assert first.status is TaskStatus.COMPLETED
    assert len(executor.requests) == 1

    # The task already finished; a second resume against a finished task is
    # a conflict, exactly like `test_resume_on_a_task_not_awaiting_confirmation_is_a_conflict`.
    with pytest.raises(TaskConflict):
        await orch.resume(paused.task_id, confirmation_token=paused.confirmation_token, approve=True)
    assert len(executor.requests) == 1  # still just once
