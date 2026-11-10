"""OD-CHAR-4: `GET /presentation/activity`, through the production composition
root.

The report is computed from the caller's own task rows on each request: it is
owner-scoped (another user's work never shows), uncached, writes nothing, and
carries scores and components only — never task text or results. Nothing on
the server outside the presentation package reads it, and the task paths that
authorize, confirm and execute behave exactly the same whatever it says.
"""

from __future__ import annotations

import pathlib
import uuid
from datetime import datetime, timedelta, timezone

from server.gateway.app import API_V1_PREFIX
from server.storage.models import AgentTask, AuditEvent, Session
from shared.schemas.presentation import ActivityPresentation
from tests.runtime.conftest import final

URL = f"{API_V1_PREFIX}/presentation/activity"
ROOT = pathlib.Path(__file__).resolve().parents[2]
SECRET = "the launch codes are 0000"


async def _report(h, actor) -> ActivityPresentation:
    resp = await h.client.get(URL, headers=actor.auth)
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    return ActivityPresentation.model_validate_json(resp.text)


async def _insert(h, actor, *, status: str, minutes_ago: float = 0, failure_code: str | None = None,
                  model_calls: int = 2, tool_calls: int = 1) -> None:
    now = datetime.now(timezone.utc)
    at = now - timedelta(minutes=minutes_ago)
    async with h.storage.session() as s:
        session_id = (await s.execute(
            Session.__table__.select().where(Session.device_id == actor.device_id)
        )).first().session_id
        live = status in {"running", "awaiting_confirmation", "waiting_for_platform"}
        s.add(AgentTask(task_id=uuid.uuid4(), user_id=actor.user_id, device_id=actor.device_id,
                        session_id=session_id, graph_id=None, status=status, mode="execute",
                        failure_code=failure_code, iterations=1, worker_switches=0,
                        model_calls=model_calls, tool_calls=tool_calls,
                        created_at=at - timedelta(seconds=30), updated_at=at,
                        finished_at=None if live else at))
        await s.commit()


async def test_a_new_user_with_no_work_is_sleepy_and_nothing_else(make_harness):
    h = await make_harness()
    alice = await h.user("alice")
    report = await _report(h, alice)
    assert report.scores.sleepy == 10
    assert report.components.inactivity == 100
    assert (report.scores.happy, report.scores.sad, report.scores.angry) == (1, 1, 1)


async def test_a_completed_task_shows_steady_work_and_only_for_its_owner(make_harness):
    h = await make_harness()
    alice, bob = await h.user("alice"), await h.user("bob")
    h.model.push(final("done"))
    done = await h.submit(alice, SECRET)
    assert done.json()["status"] == "completed", done.text

    report = await _report(h, alice)
    assert report.scores.satisfied >= 7
    assert report.scores.sleepy == 1 and report.components.inactivity == 0
    assert report.components.engagement > 0

    other = await _report(h, bob)                              # never another user's work
    assert other.components.engagement == 0 and other.scores.satisfied == 1


async def test_failures_and_cancellations_are_told_apart(make_harness):
    h = await make_harness()
    alice, bob = await h.user("alice"), await h.user("bob")
    await _insert(h, alice, status="failed", failure_code="internal_error")
    failed = await _report(h, alice)
    assert failed.scores.surprised == 10 and failed.scores.sad >= 8

    await _insert(h, bob, status="cancelled")
    cancelled = await _report(h, bob)
    assert cancelled.components.failure == 0 and cancelled.scores.sad == 1


async def test_sustained_work_is_pressure(make_harness):
    h = await make_harness()
    alice = await h.user("alice")
    for i in range(4):
        await _insert(h, alice, status="completed", minutes_ago=i * 2, model_calls=16, tool_calls=24)
    report = await _report(h, alice)
    assert report.scores.angry >= 8 and report.scores.angry > report.scores.happy


async def test_old_work_outside_the_window_does_not_count(make_harness):
    h = await make_harness()
    alice = await h.user("alice")
    await _insert(h, alice, status="failed", minutes_ago=45, failure_code="internal_error")
    report = await _report(h, alice)
    assert report.components.failure == 0 and report.components.surprise == 0
    assert report.scores.sleepy == 10                          # nor dates the inactivity: nothing older is read


async def test_a_live_task_prevents_sleepy_however_old_its_last_update(make_harness):
    h = await make_harness()
    alice = await h.user("alice")
    await _insert(h, alice, status="awaiting_confirmation", minutes_ago=90)
    report = await _report(h, alice)
    assert report.scores.sleepy == 1 and report.components.inactivity == 0
    assert report.components.pressure > 0


async def test_the_report_writes_nothing_and_carries_no_task_content(make_harness):
    h = await make_harness()
    alice = await h.user("alice")
    h.model.push(final(SECRET))
    await h.submit(alice, SECRET)
    tasks, audits = len(await h.rows(AgentTask)), len(await h.rows(AuditEvent))
    resp = await h.client.get(URL, headers=alice.auth)
    assert SECRET not in resp.text and "launch" not in resp.text
    assert set(resp.json()) == {"schema_version", "computed_at", "window_s", "scores", "components"}
    assert (len(await h.rows(AgentTask)), len(await h.rows(AuditEvent))) == (tasks, audits)


async def test_the_report_needs_a_token(make_harness):
    h = await make_harness()
    assert (await h.client.get(URL)).status_code == 401
    assert (await h.client.get(URL, headers={"Authorization": "Bearer nope"})).status_code == 401


def test_nothing_outside_the_presentation_package_reads_the_report():
    """The report is rendered by clients; on the server only the scorer, its
    route and the composition root that mounts the router may name it."""

    allowed = {
        ROOT / "server/presentation/activity.py",
        ROOT / "server/presentation/routes.py",
    }
    readers = {
        path for path in (ROOT / "server").rglob("*.py")
        if path not in allowed
        and any(word in path.read_text() for word in
                ("ActivityPresentation", "presentation.activity", "score_activity", "ActivityPolicy"))
    }
    assert readers == set()
