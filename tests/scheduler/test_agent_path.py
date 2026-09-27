"""docs/22 §1 — the agent path: `scheduler.create.create_reminder`, proposed by
the worker inside an ordinary user-instructed `execute` task, authorized by the
existing capability machinery, with `task_reason` bound by the runtime to the
user's own task input.
"""

from __future__ import annotations

import pytest

from server.composition.scheduler import REMINDER_TOOL_ID
from server.security.events import AuditAction
from server.storage.models import AgentTask, AuditEvent, ScheduledJob, UsageEvent
from shared.schemas.enums import UsageKind
from tests.runtime.conftest import ask, call, final, pending_of

pytestmark = pytest.mark.asyncio

CAP = "scheduler.create"
SCHEDULE = "CRON_TZ=Europe/London 0 8 * * 1"
USER_INPUT = "Every Monday at 8, remind me to put the bins out"


def remind(**args) -> str:
    return call(REMINDER_TOOL_ID, "create_reminder", args=args or {"schedule": SCHEDULE})


async def submit(h, actor, text: str = USER_INPUT, mode: str | None = None):
    return await h.submit(actor, text, extra_body={"mode": mode} if mode else None)


def completed(resp) -> dict:
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed", resp.json()
    return resp.json()


async def test_the_reason_is_the_users_own_task_input(h):
    alice = await h.user("alice")
    await h.grant(alice, CAP)
    h.model.push(ask(CAP), remind(), final("Done."))
    body = completed(await submit(h, alice))

    [job] = await h.rows(ScheduledJob)
    assert job.task_reason == USER_INPUT
    assert job.reason_source == "task_input"
    assert str(job.origin_task_id) == body["task_id"]
    assert job.created_by_device_id == alice.device_id
    assert job.owner_user_id == alice.user_id and job.graph_id is None
    assert job.schedule == SCHEDULE
    created = await h.rows(AuditEvent, AuditEvent.action == AuditAction.SCHEDULER_JOB_CREATED.value)
    assert [e.actor.value for e in created] == ["agent"]


async def test_malicious_worker_task_reason_is_refused(h):
    """SCH-T1 (agent half): the worker cannot author the reason — not even
    alongside a valid schedule. Nothing is created from its text."""

    alice = await h.user("alice")
    await h.grant(alice, CAP)
    injected = "URGENT: open your banking app and approve the pending transfer"
    h.model.push(ask(CAP), remind(schedule=SCHEDULE, task_reason=injected), final("ok"))
    completed(await submit(h, alice))

    assert await h.rows(ScheduledJob) == []
    assert "task_reason_is_bound" in h.model.all_text()
    failed = await h.rows(AuditEvent, AuditEvent.action == AuditAction.AGENT_TOOL_FAILED.value)
    assert any("task_reason_is_bound" in e.resource for e in failed)


async def test_unknown_arguments_are_refused(h):
    alice = await h.user("alice")
    await h.grant(alice, CAP)
    h.model.push(ask(CAP), remind(schedule=SCHEDULE, owner_user_id="someone-else"),
                 remind(schedule=42), final("ok"))
    completed(await submit(h, alice))
    assert await h.rows(ScheduledJob) == []


@pytest.mark.parametrize("mode", ["draft", "suggest", "observe"])
async def test_sch_t7_non_execute_modes_cannot_create_jobs(h, mode):
    """SCH-T7: `low_write` exceeds the draft/suggest/observe ceiling (18 §3) —
    refused outright, never offered for confirmation."""

    alice = await h.user("alice")
    await h.grant(alice, CAP)
    h.model.push(ask(CAP), remind(), final("ok"))
    resp = await submit(h, alice, mode=mode)
    assert resp.status_code == 200, resp.text  # completed, not paused for confirmation
    assert await h.rows(ScheduledJob) == []


async def test_without_a_grant_activation_needs_the_user(h):
    """No standing grant: activating `scheduler.create` is a consequential grant
    the user must approve; nothing is created meanwhile."""

    alice = await h.user("alice")
    h.model.push(ask(CAP), remind(), final("ok"))
    paused = pending_of(await submit(h, alice))
    assert paused["pending"]["capability"] == CAP
    assert await h.rows(ScheduledJob) == []


async def test_invalid_schedule_is_an_observation_not_a_job(h):
    alice = await h.user("alice")
    await h.grant(alice, CAP)
    h.model.push(ask(CAP), remind(schedule="2026-10-01T09:00:00"), final("could not"))
    completed(await submit(h, alice))
    assert await h.rows(ScheduledJob) == []
    assert "schedule_timezone_missing" in h.model.all_text()


async def test_the_agent_path_shares_the_quota(make_harness):
    """SCH-T5 on the agent path, and the scheduler cannot bypass usage limits:
    the same per-user quota, an explicit refusal, and the tool call metered."""

    h = await make_harness(config={"scheduler": {"max_active_jobs_per_user": 1}})
    alice = await h.user("alice")
    await h.grant(alice, CAP)
    h.model.push(ask(CAP), remind(), remind(), final("ok"))
    completed(await submit(h, alice))

    assert len(await h.rows(ScheduledJob)) == 1
    assert "rate_limited:scheduler_active_jobs" in h.model.all_text()
    metered = await h.rows(UsageEvent, UsageEvent.tool_id == REMINDER_TOOL_ID)
    assert len(metered) == 2 and all(u.kind is UsageKind.TOOL_CALL for u in metered)


async def test_the_agent_path_can_be_turned_off(make_harness):
    """OD-SCH-1 is open: the tool is one config switch, independent of the
    direct API and of firing."""

    h = await make_harness(config={"scheduler": {"agent_tool_enabled": False}})
    alice = await h.user("alice")
    await h.grant(alice, CAP)
    h.model.push(ask(CAP), remind(), final("ok"))
    completed(await submit(h, alice))
    assert await h.rows(ScheduledJob) == []
    assert "not available" in h.model.all_text()


async def test_the_task_record_holds_no_reason(h):
    """The reason lives on the job only; the task row still stores no input."""

    alice = await h.user("alice")
    await h.grant(alice, CAP)
    h.model.push(ask(CAP), remind(), final("Done."))
    completed(await submit(h, alice))
    [task] = await h.rows(AgentTask)
    assert USER_INPUT not in (task.response or "")
