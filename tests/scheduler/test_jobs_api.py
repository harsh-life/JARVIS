"""02 §9 / docs/22 §1 — the direct creation path, through the production
composition root: identity from the token, the engine for every decision,
`task_reason` required, visibility default `private`, cancel confirmed like any
delete, and the per-user quota as an explicit `429`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from server.gateway.app import API_V1_PREFIX
from server.security.events import AuditAction
from server.storage.models import AuditEvent, ScheduledJob
from shared.schemas.enums import JobStatus, Visibility

pytestmark = pytest.mark.asyncio

JOBS = f"{API_V1_PREFIX}/jobs"


def future(minutes: int = 90) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).replace(microsecond=0).isoformat()


async def create(h, actor, reason: str = "call the dentist", schedule: str | None = None, **extra):
    return await h.client.post(
        JOBS, json={"task_reason": reason, "schedule": schedule or future(), **extra}, headers=actor.auth
    )


def error(resp) -> dict:
    return resp.json()["error"]


# ── creation ─────────────────────────────────────────────────────────────


async def test_create_is_owned_by_the_caller_private_and_audited(h):
    alice = await h.user("alice")
    resp = await create(h, alice, "Pay the electricity bill", "CRON_TZ=Asia/Kolkata 0 9 1 * *")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["owner_user_id"] == str(alice.user_id) == body["source_user_id"]
    assert body["visibility"] == "private"
    assert body["status"] == "active"
    assert body["reason_source"] == "user"
    assert body["task_reason"] == "Pay the electricity bill"
    assert body["next_fire_at"] is not None and body["last_firing"] is None

    [row] = await h.rows(ScheduledJob)
    assert row.created_by_device_id == alice.device_id and row.origin_task_id is None
    [event] = await h.rows(AuditEvent, AuditEvent.action == AuditAction.SCHEDULER_JOB_CREATED.value)
    assert event.resource == f"scheduledjob:{row.job_id}"
    # The audit row names the job, never the reminder's words.
    assert "electricity" not in event.resource


async def test_identity_and_visibility_cannot_be_asserted_in_the_body(h):
    """PHONE-003 / API-T2: a body claiming an owner, a visibility or a device is
    refused outright, never half-read."""

    alice, bob = await h.user("alice"), await h.user("bob")
    for extra in ({"owner_user_id": str(bob.user_id)}, {"visibility": "graph"},
                  {"source_user_id": str(bob.user_id)}, {"created_by_device_id": str(bob.device_id)}):
        resp = await create(h, alice, **extra)
        assert resp.status_code == 422, (extra, resp.text)
    assert await h.rows(ScheduledJob) == []


async def test_sch_t1_an_empty_task_reason_is_rejected(h):
    """SCH-T1 (API half), DM-T4: no reminder without the user's reason."""

    alice = await h.user("alice")
    for reason in ("", "   ", "\n\t "):
        resp = await create(h, alice, reason)
        assert resp.status_code == 422, (reason, resp.text)
    resp = await h.client.post(JOBS, json={"schedule": future()}, headers=alice.auth)
    assert resp.status_code == 422
    assert await h.rows(ScheduledJob) == []
    refused = await h.rows(AuditEvent, AuditEvent.action == AuditAction.SCHEDULER_JOB_REFUSED.value)
    assert refused and all(e.resource == "scheduledjob:refused:task_reason_required" for e in refused)


async def test_invalid_schedules_are_explicit_422s(h):
    alice = await h.user("alice")
    cases = {
        "2026-10-01T09:00:00": "schedule_timezone_missing",
        "2020-01-01T09:00:00Z": "schedule_in_past",
        "* * * * *": "schedule_too_frequent",
        "every day": "schedule_invalid",
    }
    for schedule, reason in cases.items():
        resp = await create(h, alice, schedule=schedule)
        assert resp.status_code == 422, resp.text
        assert error(resp)["details"]["reason"] == reason
    too_long = await create(h, alice, "x" * 1001)
    assert error(too_long)["details"]["reason"] == "task_reason_too_long"
    assert await h.rows(ScheduledJob) == []


async def test_unauthenticated_calls_are_401(h):
    assert (await h.client.get(JOBS)).status_code == 401
    assert (await h.client.post(JOBS, json={"task_reason": "x", "schedule": future()})).status_code == 401
    assert (await h.client.delete(f"{JOBS}/{uuid.uuid4()}")).status_code == 401


async def test_graph_id_is_a_claim_checked_by_membership(h):
    alice, bob, outsider = await h.user("alice"), await h.user("bob"), await h.user("outsider")
    graph = await h.shared_graph(alice, bob)

    resp = await create(h, alice, graph_id=str(graph))
    assert resp.status_code == 201, resp.text
    assert resp.json()["graph_id"] == str(graph) and resp.json()["visibility"] == "private"

    # A graph the caller is not in: 404, indistinguishable from no such graph.
    resp = await create(h, outsider, graph_id=str(graph))
    assert resp.status_code == 404, resp.text
    resp = await create(h, outsider, graph_id=str(uuid.uuid4()))
    assert resp.status_code == 404
    assert len(await h.rows(ScheduledJob)) == 1


async def test_idempotent_creation_does_not_double_create(h):
    alice = await h.user("alice")
    body = {"task_reason": "water the plants", "schedule": future()}
    headers = {**alice.auth, "Idempotency-Key": "k-1"}
    first = await h.client.post(JOBS, json=body, headers=headers)
    second = await h.client.post(JOBS, json=body, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json()["job_id"] == second.json()["job_id"]
    assert len(await h.rows(ScheduledJob)) == 1
    conflict = await h.client.post(JOBS, json={**body, "task_reason": "other"}, headers=headers)
    assert conflict.status_code == 409


# ── visibility ───────────────────────────────────────────────────────────


async def test_listing_is_visibility_filtered(h):
    """RAUTH V1: a job's `graph_id` alone never makes it readable by members."""

    alice, bob, outsider = await h.user("alice"), await h.user("bob"), await h.user("outsider")
    graph = await h.shared_graph(alice, bob)
    private_in_graph = (await create(h, alice, "private in graph", graph_id=str(graph))).json()["job_id"]
    shared = (await create(h, alice, "shared with graph", graph_id=str(graph))).json()["job_id"]
    mine = (await create(h, bob, "bob's own")).json()["job_id"]
    # Sharing is an owner act with its own flow (RAUTH V2); set it directly here.
    async with h.storage.session() as s:
        await s.execute(update(ScheduledJob).where(ScheduledJob.job_id == uuid.UUID(shared))
                        .values(visibility=Visibility.GRAPH))
        await s.commit()

    def ids(resp):
        assert resp.status_code == 200, resp.text
        return {item["job_id"] for item in resp.json()["items"]}

    assert ids(await h.client.get(JOBS, headers=alice.auth)) == {private_in_graph, shared}
    assert ids(await h.client.get(JOBS, headers=bob.auth)) == {shared, mine}
    assert ids(await h.client.get(JOBS, headers=outsider.auth)) == set()

    assert (await h.client.get(f"{JOBS}/{private_in_graph}", headers=bob.auth)).status_code == 404
    assert (await h.client.get(f"{JOBS}/{shared}", headers=bob.auth)).status_code == 200
    assert (await h.client.get(f"{JOBS}/{shared}", headers=outsider.auth)).status_code == 404

    # A member who leaves stops seeing the shared job at once (live membership).
    r = await h.client.delete(f"{API_V1_PREFIX}/graphs/{graph}/members/{bob.user_id}", headers=alice.auth)
    if r.status_code == 403:  # the graph API asks the owner to confirm removals
        token = r.json()["error"]["details"]["confirmation_token"]
        r = await h.client.delete(f"{API_V1_PREFIX}/graphs/{graph}/members/{bob.user_id}",
                                  headers={**alice.auth, "X-Confirmation-Token": token})
    assert r.status_code in (200, 204), r.text
    assert ids(await h.client.get(JOBS, headers=bob.auth)) == {mine}


async def test_listing_paginates_with_a_cursor(h):
    alice = await h.user("alice")
    created = [(await create(h, alice, f"reminder {i}", future(60 + i))).json()["job_id"] for i in range(5)]
    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = (await h.client.get(JOBS, params=params, headers=alice.auth)).json()
        seen += [item["job_id"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(created) and len(seen) == 5
    bad = await h.client.get(JOBS, params={"cursor": "not-a-cursor"}, headers=alice.auth)
    assert bad.status_code == 422


# ── cancellation ─────────────────────────────────────────────────────────


async def cancel(h, actor, job_id: str):
    first = await h.client.delete(f"{JOBS}/{job_id}", headers=actor.auth)
    if first.status_code != 403 or error(first)["code"] != "confirmation_required":
        return first
    token = error(first)["details"]["confirmation_token"]
    return await h.client.delete(f"{JOBS}/{job_id}", headers={**actor.auth, "X-Confirmation-Token": token})


async def test_cancel_needs_confirmation_then_stops_the_job(h):
    alice = await h.user("alice")
    job_id = (await create(h, alice)).json()["job_id"]

    first = await h.client.delete(f"{JOBS}/{job_id}", headers=alice.auth)
    assert first.status_code == 403 and error(first)["code"] == "confirmation_required"
    assert error(first)["details"]["action"] == "cancel_reminder"
    [row] = await h.rows(ScheduledJob)
    assert row.status is JobStatus.ACTIVE  # nothing changed yet

    token = error(first)["details"]["confirmation_token"]
    ok = await h.client.delete(f"{JOBS}/{job_id}", headers={**alice.auth, "X-Confirmation-Token": token})
    assert ok.status_code == 204, ok.text
    [row] = await h.rows(ScheduledJob)
    assert row.status is JobStatus.CANCELLED and row.next_fire_at is None
    assert await h.rows(AuditEvent, AuditEvent.action == AuditAction.SCHEDULER_JOB_CANCELLED.value)

    # A spent token does not cancel anything else; a second cancel is a no-op.
    other = (await create(h, alice, "another")).json()["job_id"]
    reused = await h.client.delete(f"{JOBS}/{other}", headers={**alice.auth, "X-Confirmation-Token": token})
    assert reused.status_code == 403
    assert (await cancel(h, alice, job_id)).status_code == 204
    assert [r.status for r in await h.rows(ScheduledJob, ScheduledJob.job_id == uuid.UUID(other))] == [
        JobStatus.ACTIVE
    ]


async def test_only_the_owner_can_cancel(h):
    alice, bob, outsider = await h.user("alice"), await h.user("bob"), await h.user("outsider")
    graph = await h.shared_graph(alice, bob)
    job_id = (await create(h, alice, graph_id=str(graph))).json()["job_id"]
    async with h.storage.session() as s:
        await s.execute(update(ScheduledJob).values(visibility=Visibility.GRAPH))
        await s.commit()

    # Bob can see it (graph-visible) but not govern it (D3); the outsider cannot
    # even learn it exists.
    assert (await cancel(h, bob, job_id)).status_code == 403
    assert (await cancel(h, outsider, job_id)).status_code == 404
    assert (await cancel(h, outsider, str(uuid.uuid4()))).status_code == 404
    [row] = await h.rows(ScheduledJob)
    assert row.status is JobStatus.ACTIVE


# ── quota (SCH-T5) ───────────────────────────────────────────────────────


async def test_sch_t5_hourly_creation_quota_is_an_explicit_429(make_harness):
    h = await make_harness(config={"security": {"rate_limits": {"scheduler_creations_per_hour": 3}}})
    alice, bob = await h.user("alice"), await h.user("bob")
    for i in range(3):
        assert (await create(h, alice, f"r{i}")).status_code == 201
    over = await create(h, alice, "one too many")
    assert over.status_code == 429, over.text
    assert error(over)["code"] == "rate_limited" and error(over)["retryable"] is True
    assert error(over)["details"]["limit"] == "scheduler_creations_per_hour"
    assert 0 < error(over)["details"]["retry_after"] <= 3601

    # Cancelling does not give creations back: the quota counts rows created.
    job_id = (await h.client.get(JOBS, headers=alice.auth)).json()["items"][0]["job_id"]
    assert (await cancel(h, alice, job_id)).status_code == 204
    assert (await create(h, alice, "still over")).status_code == 429
    # Per user: bob is unaffected.
    assert (await create(h, bob, "bob's")).status_code == 201
    assert await h.rows(AuditEvent, AuditEvent.action == AuditAction.USAGE_LIMIT_EXCEEDED.value,
                        AuditEvent.resource == "limit:scheduler_creations_per_hour")


async def test_sch_t5_active_job_quota_is_an_explicit_429(make_harness):
    h = await make_harness(config={"scheduler": {"max_active_jobs_per_user": 2}})
    alice = await h.user("alice")
    first = (await create(h, alice, "a")).json()["job_id"]
    assert (await create(h, alice, "b")).status_code == 201
    over = await create(h, alice, "c")
    assert over.status_code == 429 and error(over)["details"]["limit"] == "scheduler_active_jobs"
    # Cancelling an active job frees a slot.
    assert (await cancel(h, alice, first)).status_code == 204
    assert (await create(h, alice, "c")).status_code == 201


async def test_the_quota_check_runs_before_validation(make_harness):
    """02 §2: rate precheck (step 4) before body validation (step 5)."""

    h = await make_harness(config={"scheduler": {"max_active_jobs_per_user": 1}})
    alice = await h.user("alice")
    assert (await create(h, alice)).status_code == 201
    assert (await create(h, alice, schedule="garbage")).status_code == 429


# ── disabled ─────────────────────────────────────────────────────────────


async def test_a_disabled_scheduler_is_503_scheduler(make_harness):
    h = await make_harness(config={"scheduler": {"enabled": False}})
    alice = await h.user("alice")
    for resp in (await create(h, alice), await h.client.get(JOBS, headers=alice.auth),
                 await h.client.delete(f"{JOBS}/{uuid.uuid4()}", headers=alice.auth)):
        assert resp.status_code == 503
        assert error(resp)["details"]["dependency"] == "scheduler"
    tools = (await h.client.get(f"{API_V1_PREFIX}/config/tools", headers=alice.auth)).json()
    assert "scheduler.reminders" not in str(tools)
