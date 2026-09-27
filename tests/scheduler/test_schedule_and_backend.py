"""docs/22 §3 — schedule semantics and the replaceable `SchedulerBackend`.

The backend's whole state is the job row in the application database, so the
restart test below opens a second storage backend on the same file and finds
every job exactly where it was.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from server.config.schema import AppConfig, SchedulerConfig
from server.scheduler.backend import DatabaseSchedulerBackend, SchedulerBackend, build_backend
from server.scheduler.schedule import ScheduleError, first_fire, parse_schedule
from server.storage import SQLAlchemyStorageBackend
from server.storage.models import ScheduledJob
from shared.schemas.enums import JobStatus, Visibility
from tests.scheduler.conftest import NOW, make_user

MIN = timedelta(minutes=5)
HORIZON = timedelta(days=730)


# ── schedule strings ────────────────────────────────────────────────────


def test_one_shot_needs_an_offset_and_is_normalized_to_utc() -> None:
    schedule = parse_schedule("2026-10-01T09:00:00+05:30")
    assert not schedule.recurring
    assert schedule.at == datetime(2026, 10, 1, 3, 30, tzinfo=timezone.utc)
    assert parse_schedule("2026-10-01T03:30:00Z").at == schedule.at

    with pytest.raises(ScheduleError) as naive:
        parse_schedule("2026-10-01T09:00:00")
    assert naive.value.reason == "schedule_timezone_missing"


def test_cron_runs_on_utc_or_the_named_zone() -> None:
    utc = parse_schedule("0 9 * * *")
    assert utc.recurring
    assert utc.next_after(NOW) == datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)

    kolkata = parse_schedule("CRON_TZ=Asia/Kolkata 0 9 * * *")
    assert kolkata.next_after(NOW) == datetime(2026, 9, 28, 3, 30, tzinfo=timezone.utc)
    # Strictly after: an occurrence at exactly `instant` is not "next".
    at = kolkata.next_after(NOW)
    assert kolkata.next_after(at) == at + timedelta(days=1)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("", "schedule_invalid"),
        ("tomorrow at nine", "schedule_invalid"),
        ("61 * * * *", "schedule_invalid"),
        ("* * * *", "schedule_invalid"),
        ("CRON_TZ=Mars/Olympus 0 9 * * *", "schedule_timezone_unknown"),
        ("0 9 * * * " + "x" * 200, "schedule_invalid"),
    ],
)
def test_malformed_schedules_are_refused(raw: str, reason: str) -> None:
    with pytest.raises(ScheduleError) as exc:
        parse_schedule(raw)
    assert exc.value.reason == reason


def test_bounds_on_new_jobs() -> None:
    with pytest.raises(ScheduleError) as past:
        first_fire(parse_schedule("2026-09-27T11:00:00Z"), now=NOW, min_recurrence=MIN, max_horizon=HORIZON)
    assert past.value.reason == "schedule_in_past"

    with pytest.raises(ScheduleError) as far:
        first_fire(parse_schedule("2031-01-01T00:00:00Z"), now=NOW, min_recurrence=MIN, max_horizon=HORIZON)
    assert far.value.reason == "schedule_too_far"

    # Every minute, and an irregular cron with one 1-minute gap, both recur
    # faster than allowed.
    for raw in ("* * * * *", "0,1 9 * * *"):
        with pytest.raises(ScheduleError) as fast:
            first_fire(parse_schedule(raw), now=NOW, min_recurrence=MIN, max_horizon=HORIZON)
        assert fast.value.reason == "schedule_too_frequent"

    assert first_fire(parse_schedule("*/5 * * * *"), now=NOW, min_recurrence=MIN, max_horizon=HORIZON) == (
        NOW + timedelta(minutes=5)
    )


def test_occurrences_between_is_bounded() -> None:
    schedule = parse_schedule("0 * * * *")
    found = schedule.occurrences_between(NOW, NOW + timedelta(days=30), limit=5)
    assert found == [NOW + timedelta(hours=h) for h in range(5)]


# ── configuration ───────────────────────────────────────────────────────


def test_scheduler_config_defaults_match_docs_22() -> None:
    config = SchedulerConfig()
    assert config.enabled is True
    assert config.backend == "apscheduler_db"
    assert config.misfire_grace_minutes == 60
    assert config.max_active_jobs_per_user == 50
    rate = AppConfig.model_fields["security"].annotation.model_fields["rate_limits"].default_factory()
    assert rate.scheduler_creations_per_hour == 20
    with pytest.raises(ValueError):
        SchedulerConfig(backend="cron_file")


def test_backend_is_replaceable_by_name_only() -> None:
    backend: SchedulerBackend = build_backend("apscheduler_db")
    assert isinstance(backend, DatabaseSchedulerBackend)
    with pytest.raises(ValueError):
        build_backend("in_memory")


# ── the database backend ────────────────────────────────────────────────


def _job(owner: uuid.UUID, reason: str = "call the dentist", schedule: str = "0 9 * * *") -> ScheduledJob:
    return ScheduledJob(
        owner_user_id=owner, source_user_id=owner, visibility=Visibility.PRIVATE,
        task_reason=reason, schedule=schedule, created_at=NOW,
    )


async def test_add_list_due_mark_remove(storage: SQLAlchemyStorageBackend) -> None:
    backend = DatabaseSchedulerBackend()
    owner = await make_user(storage)
    async with storage.session() as session:
        soon, later = _job(owner, "soon"), _job(owner, "later")
        await backend.add(session, soon, first_fire=NOW + timedelta(minutes=1))
        await backend.add(session, later, first_fire=NOW + timedelta(hours=5))
        await session.commit()

    async with storage.session() as session:
        assert [j.task_reason for j in await backend.list_due(session, now=NOW, limit=10)] == []
        due = await backend.list_due(session, now=NOW + timedelta(minutes=2), limit=10)
        assert [j.task_reason for j in due] == ["soon"]
        assert await backend.next_due_at(session) == NOW + timedelta(minutes=1)

        await backend.mark(session, due[0], next_fire_at=None, status=JobStatus.FIRED)
        await session.commit()

    async with storage.session() as session:
        assert await backend.list_due(session, now=NOW + timedelta(days=1), limit=10) != []
        listed = await backend.list(session, owner_user_id=owner, include_inactive=False, limit=10)
        assert [j.task_reason for j in listed] == ["later"]
        everything = await backend.list(session, owner_user_id=owner, include_inactive=True, limit=10)
        assert {j.task_reason for j in everything} == {"soon", "later"}

        assert await backend.remove(session, listed[0].job_id) is True
        assert await backend.remove(session, listed[0].job_id) is False
        await session.commit()

    async with storage.session() as session:
        assert await backend.list_due(session, now=NOW + timedelta(days=365), limit=10) == []
        assert await backend.next_due_at(session) is None


async def test_jobs_survive_a_restart(storage: SQLAlchemyStorageBackend, db_path) -> None:
    """SCH-T6 (persistence half): nothing about a job lives in process memory."""

    owner = await make_user(storage)
    async with storage.session() as session:
        await DatabaseSchedulerBackend().add(session, _job(owner), first_fire=NOW + timedelta(minutes=10))
        await session.commit()
    await storage.dispose()

    restarted = SQLAlchemyStorageBackend(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with restarted.session() as session:
            due = await DatabaseSchedulerBackend().list_due(session, now=NOW + timedelta(minutes=10), limit=10)
            assert [j.task_reason for j in due] == ["call the dentist"]
            assert due[0].status is JobStatus.ACTIVE
    finally:
        await restarted.dispose()


async def test_stored_instants_compare_in_utc(storage: SQLAlchemyStorageBackend) -> None:
    """An aware non-UTC instant is stored as the same UTC instant, so due-ness
    never depends on which offset a caller happened to use."""

    owner = await make_user(storage)
    ist = timezone(timedelta(hours=5, minutes=30))
    async with storage.session() as session:
        await DatabaseSchedulerBackend().add(
            session, _job(owner), first_fire=datetime(2026, 9, 27, 18, 0, tzinfo=ist)  # 12:30Z
        )
        await session.commit()
    async with storage.session() as session:
        backend = DatabaseSchedulerBackend()
        assert await backend.list_due(session, now=NOW + timedelta(minutes=29), limit=5) == []
        assert len(await backend.list_due(session, now=NOW + timedelta(minutes=30), limit=5)) == 1
