"""The rules evaluator wired into the real application.

Built by the production composition root around the production Security Core,
ledgers and breaker. Only the worker's model is scripted; evaluation has no
model at all.

| Property | Tests |
|---|---|
| disabled → fully functional, nothing wired | `test_disabled_evaluation_leaves_the_system_unchanged` |
| a record, never read back | `test_a_post_hoc_evaluation_is_recorded_and_changes_nothing` |
| no model call, no usage, no cost | `test_evaluation_makes_no_model_call_and_meters_nothing` |
| secrets redacted, audited by name | `test_a_secret_in_an_observation_is_redacted_and_audited` |
| one user's task per record | `test_each_record_is_one_users_task` |
| no effect on a running task | `test_evaluation_never_observes_or_stops_a_running_task` |
"""

from __future__ import annotations

import pytest

from server.security.events import AuditAction
from server.storage.models import AgentTask, ImprovementCandidateRow, TaskEvaluation, UsageEvent
from shared.schemas.enums import Visibility
from tests.evaluation.conftest import audit, drain, evaluations
from tests.runtime.conftest import ask, call, failure_of, final

pytestmark = pytest.mark.asyncio

SECRET = "sk-proj-AbCdEfGhIjKlMnOpQrStUv123456"  # TEST-ONLY fixture


async def completed_task(h, actor, *steps, text="list my files please"):
    await h.grant(actor, "file.read")
    h.model.push(ask("file.read"), *steps)
    resp = await h.submit(actor, text)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_disabled_evaluation_leaves_the_system_unchanged(make_harness):
    h = await make_harness()
    alice = await h.user("alice")
    result = await completed_task(h, alice, call("files.read", "list_directory"), final("two files"))
    assert result["status"] == "completed" and result["response"] == "two files"
    assert h.app.state.evaluation is None
    assert h.runtime._observer is None  # nothing observes tasks
    assert await evaluations(h) == []
    # Every breaker trigger still works with evaluation off (SUP-T8).
    bob = await h.user("bob")
    bobs = await h.file(bob, None, Visibility.PRIVATE, "diary.txt")
    h.model.push(ask("file.read"), *[call("files.read", "read_file", ref=bobs) for _ in range(5)])
    assert failure_of(await h.submit(alice)) == "emergency_stop"


async def test_a_post_hoc_evaluation_is_recorded_and_changes_nothing(evaluated):
    h = await evaluated()
    alice = await h.user("alice")
    result = await completed_task(h, alice, call("files.read", "list_directory"), final("done"))
    await drain(h)

    [row] = await evaluations(h)
    assert (row.outcome, row.kind, row.evaluator_id) == ("recorded", "post_hoc", "rules_evaluator")
    assert row.quality is None and row.stop_requested is False and row.stop_honoured is False
    assert row.owner_user_id == alice.user_id and row.visibility.value == "private"
    assert str(row.task_id) == result["task_id"]
    assert set(row.findings) == {"redundant_steps", "failures"}
    [recorded] = await audit(h, AuditAction.EVALUATION_RECORDED)
    assert recorded.resource.startswith(f"evaluation:{row.evaluation_id}:task:{row.task_id}")
    # A record, not a control: the finished task is exactly what it was, and
    # no candidate (or anything else another user's task could read) exists.
    async with h.storage.session() as s:
        task = await s.get(AgentTask, row.task_id)
    assert (task.status, task.response, task.model_calls) == ("completed", "done", 3)
    assert await h.rows(ImprovementCandidateRow) == []


async def test_evaluation_makes_no_model_call_and_meters_nothing(evaluated):
    h = await evaluated()
    alice = await h.user("alice")
    await completed_task(h, alice, call("files.read", "list_directory"), final("done"))
    calls_before = len(h.model.seen)
    usage_before = len(await h.rows(UsageEvent))
    await drain(h)
    assert len(await evaluations(h)) == 1
    assert len(h.model.seen) == calls_before          # the worker's model was not called again
    assert all(len(m.seen) == 0 for name, m in h.models.items() if m is not h.model)
    assert len(await h.rows(UsageEvent)) == usage_before  # nothing metered for evaluation
    assert all(not (u.tool_id or "").startswith("evaluator:") for u in await h.rows(UsageEvent))


async def test_a_secret_in_an_observation_is_redacted_and_audited(evaluated):
    h = await evaluated()
    alice = await h.user("alice")
    await completed_task(h, alice, call("files.read", "list_directory"), final(f"the key is {SECRET}"),
                         text=f"remember {SECRET}")
    await drain(h)
    [row] = await evaluations(h)
    assert row.redactions
    redacted = await audit(h, AuditAction.EVALUATION_TRACE_REDACTED)
    assert redacted and all(SECRET not in r.resource for r in redacted)
    assert SECRET not in repr(row.findings)


async def test_each_record_is_one_users_task(evaluated):
    h = await evaluated()
    alice, bob = await h.user("alice"), await h.user("bob")
    a = await completed_task(h, alice, call("files.read", "list_directory"), final("alice's"))
    b = await completed_task(h, bob, call("files.read", "list_directory"), final("bob's"))
    await drain(h)
    rows = {str(r.task_id): r for r in await evaluations(h)}
    assert rows[a["task_id"]].owner_user_id == alice.user_id
    assert rows[b["task_id"]].owner_user_id == bob.user_id


async def test_evaluation_never_observes_or_stops_a_running_task(evaluated):
    h = await evaluated()
    alice = await h.user("alice")
    # A task with repeated denials: the rules flag it, informationally, after it ends.
    bob = await h.user("bob")
    bobs = await h.file(bob, None, Visibility.PRIVATE, "diary.txt")
    await h.grant(alice, "file.read")
    h.model.push(ask("file.read"), call("files.read", "read_file", ref=bobs),
                 call("files.read", "read_file", ref=bobs), final("gave up"))
    resp = await h.submit(alice)
    assert resp.status_code == 200 and resp.json()["status"] == "completed"
    await drain(h)
    [row] = await evaluations(h)
    assert (row.anomaly, row.anomaly_reason) == ("suspicious", "repeated_denials")
    assert row.stop_requested is False
    # The only observer method that does anything runs after the task ended.
    observer = h.runtime._observer
    assert observer.step_completed(row.task_id, 1, lambda: None) is None
    assert not hasattr(observer, "trip") and not hasattr(h.app.state.evaluation, "breaker")
    assert await h.rows(TaskEvaluation, TaskEvaluation.stop_honoured.is_(True)) == []
