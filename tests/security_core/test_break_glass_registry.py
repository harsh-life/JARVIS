"""The break-glass record store (`server/composition/break_glass.py`, 20 §2.2/§2.4).

Pure, in-memory, and the only place a record is created. These tests pin its
rules one at a time: who can create one, what it may name, how long it lives,
what ends it, and that every invocation and end leaves exactly one audit row
to be written.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from server.composition.break_glass import (
    BreakGlassRefused,
    BreakGlassRegistry,
    EndReason,
    EventKind,
    RefusalCode,
    activation_resource,
)
from server.composition.security_port import _end_reason
from server.config.schema import BreakGlassConfig
from server.execution.break_glass import InvocationOutcome
from shared.schemas.agent import AgentFailureCode, AgentTaskStatus
from tests.support import make_superuser

TASK, OWNER = uuid.uuid4(), uuid.uuid4()


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def su(monkeypatch):
    return make_superuser(monkeypatch)


@pytest.fixture
def clock():
    return Clock()


def store(clock=None, **overrides) -> BreakGlassRegistry:
    cfg = dict(enabled=True, allowed_executables=["cat", "strace"], max_invocations=3, max_window_minutes=15)
    cfg.update(overrides)
    return BreakGlassRegistry(BreakGlassConfig(**cfg), **({"clock": clock} if clock else {}))


def prepare(reg, su, **overrides):
    args = dict(task_id=TASK, task_owner=OWNER, user_id=OWNER, executables=["cat"], max_invocations=1,
                window_seconds=None, task_seconds_left=3600, reason="repair")
    args.update(overrides)
    return reg.prepare(su, **args)


def activate(reg, su, **overrides):
    record = prepare(reg, su, **overrides)
    reg.install(su, record)
    return record


def refusal(fn, *args, **kwargs) -> RefusalCode:
    with pytest.raises(BreakGlassRefused) as excinfo:
        fn(*args, **kwargs)
    return excinfo.value.code


# ── creating a record ──────────────────────────────────────────────────────


def test_only_a_verified_superuser_can_create_or_revoke(su):
    reg = store()
    for impostor in (object(), None, "superuser", OWNER):
        assert refusal(prepare, reg, impostor) is RefusalCode.NOT_SUPERUSER
        assert refusal(reg.install, impostor, prepare(reg, su)) is RefusalCode.NOT_SUPERUSER
        assert refusal(reg.revoke, impostor, task_id=TASK) is RefusalCode.NOT_SUPERUSER
    assert reg.active() == []


def test_a_disabled_store_creates_nothing(su):
    reg = store(enabled=False)
    assert refusal(prepare, reg, su) is RefusalCode.DISABLED
    assert reg.allowed_executables == ()


def test_the_named_user_must_own_the_task(su):
    assert refusal(prepare, store(), su, user_id=uuid.uuid4()) is RefusalCode.USER_MISMATCH


@pytest.mark.parametrize("executables", [[], ["sh"], ["cat", "sh"], ["/usr/bin/cat"]])
def test_executables_must_come_from_the_break_glass_list(su, executables):
    assert refusal(prepare, store(), su, executables=executables) is RefusalCode.EXECUTABLE_NOT_ALLOWED


@pytest.mark.parametrize("limits", [
    {"max_invocations": 0}, {"max_invocations": 4},
    {"window_seconds": 0}, {"window_seconds": 15 * 60 + 1},
])
def test_limits_are_bounded_by_the_operator_config(su, limits):
    assert refusal(prepare, store(), su, **limits) is RefusalCode.INVALID_LIMITS


def test_the_window_defaults_to_the_cap_and_never_outlasts_the_task(su, clock):
    reg = store(clock)
    record = prepare(reg, su)
    assert record.expires_at - record.activated_at == timedelta(minutes=15)
    record = prepare(reg, su, task_seconds_left=42.9)
    assert record.expires_at - record.activated_at == timedelta(seconds=42)
    record = prepare(reg, su, window_seconds=10, task_seconds_left=42)
    assert record.expires_at - record.activated_at == timedelta(seconds=10)
    assert refusal(prepare, reg, su, task_seconds_left=0.5) is RefusalCode.TASK_NOT_LIVE


def test_prepared_is_not_live_until_installed(su):
    reg = store()
    record = prepare(reg, su)
    assert reg.active() == [] and reg.claim(task_id=TASK, user_id=OWNER, executable="cat") is None
    reg.install(su, record)
    assert reg.active() == [record] and reg.active_for(TASK)


def test_one_live_record_per_task(su):
    reg = store()
    first = activate(reg, su)
    assert refusal(prepare, reg, su) is RefusalCode.ALREADY_ACTIVE
    second = prepare(reg, su, task_id=uuid.uuid4())
    reg.install(su, second)
    assert refusal(reg.install, su, first) is RefusalCode.ALREADY_ACTIVE
    assert len(reg.active()) == 2


def test_install_refuses_a_window_that_already_passed(su, clock):
    reg = store(clock)
    record = prepare(reg, su, window_seconds=5)
    clock.now += timedelta(seconds=5)
    assert refusal(reg.install, su, record) is RefusalCode.INVALID_LIMITS


def test_record_ids_are_unique_and_the_activation_resource_is_identifiers_only(su):
    reg = store()
    ids = {prepare(reg, su).record_id for _ in range(200)}
    assert len(ids) == 200
    record = prepare(reg, su, executables=["cat", "strace"], max_invocations=2, window_seconds=90)
    assert activation_resource(record) == f"bg:{record.record_id}:{TASK}:repair:n2:t90:cat,strace"


# ── claiming ───────────────────────────────────────────────────────────────


def test_a_claim_needs_the_task_the_owner_and_a_named_executable(su):
    reg = store()
    activate(reg, su, max_invocations=3)
    assert reg.claim(task_id=uuid.uuid4(), user_id=OWNER, executable="cat") is None
    assert reg.claim(task_id=TASK, user_id=uuid.uuid4(), executable="cat") is None
    assert reg.claim(task_id=TASK, user_id=OWNER, executable="strace") is None  # on the list, not in the record
    claim = reg.claim(task_id=TASK, user_id=OWNER, executable="cat")
    assert (claim.task_id, claim.user_id, claim.executable, claim.remaining) == (TASK, OWNER, "cat", 2)


def test_invocations_are_counted_and_the_last_one_ends_the_record(su):
    reg = store()
    record = activate(reg, su, max_invocations=2)
    first = reg.claim(task_id=TASK, user_id=OWNER, executable="cat")
    reg.record_invocation(first, argv_sha256="a" * 16, outcome=InvocationOutcome.EXITED, exit_code=0,
                          duration_ms=5)
    last = reg.claim(task_id=TASK, user_id=OWNER, executable="cat")
    assert last.remaining == 0
    assert reg.claim(task_id=TASK, user_id=OWNER, executable="cat") is None  # spent while it runs
    assert reg.active() == []
    reg.record_invocation(last, argv_sha256="b" * 16, outcome=InvocationOutcome.EXITED, exit_code=1,
                          duration_ms=7)
    events = reg.drain(TASK)
    assert [(e.kind, e.resource) for e in events] == [
        (EventKind.INVOKED, f"bg:{record.record_id}:{TASK}:exited:0:5ms:{'a' * 16}:cat"),
        (EventKind.INVOKED, f"bg:{record.record_id}:{TASK}:exited:1:7ms:{'b' * 16}:cat"),
        (EventKind.ENDED, f"bg:{record.record_id}:{TASK}:exhausted"),
    ]
    assert all(e.user_id == OWNER for e in events)
    assert record.ended is EndReason.EXHAUSTED and reg.drain(TASK) == []  # each row returned once


def test_expiry_ends_the_record(su, clock):
    reg = store(clock)
    record = activate(reg, su, window_seconds=60)
    clock.now += timedelta(seconds=59)
    assert reg.active_for(TASK)
    clock.now += timedelta(seconds=1)
    assert not reg.active_for(TASK)
    assert reg.claim(task_id=TASK, user_id=OWNER, executable="cat") is None
    assert [e.resource for e in reg.drain(TASK)] == [f"bg:{record.record_id}:{TASK}:expired"]
    assert record.ended is EndReason.EXPIRED


# ── ending a record ────────────────────────────────────────────────────────


@pytest.mark.parametrize("reason", [EndReason.TASK_COMPLETED, EndReason.TASK_FAILED, EndReason.TASK_CANCELLED,
                                    EndReason.BREAKER_TRIP])
def test_the_task_ending_ends_the_record(su, reason):
    reg = store()
    record = activate(reg, su)
    reg.end_task(TASK, reason)
    reg.end_task(TASK, EndReason.TASK_FAILED)  # idempotent: ended once
    assert reg.claim(task_id=TASK, user_id=OWNER, executable="cat") is None
    assert [e.resource for e in reg.drain(TASK)] == [f"bg:{record.record_id}:{TASK}:{reason.value}"]


def test_a_task_ending_mid_run_ends_a_spent_record_too(su):
    reg = store()
    record = activate(reg, su)
    claim = reg.claim(task_id=TASK, user_id=OWNER, executable="cat")
    reg.end_task(TASK, EndReason.BREAKER_TRIP)
    reg.record_invocation(claim, argv_sha256="c" * 16, outcome=InvocationOutcome.CANCELLED, exit_code=None,
                          duration_ms=3)
    assert [e.resource for e in reg.drain(TASK)] == [
        f"bg:{record.record_id}:{TASK}:breaker_trip",
        f"bg:{record.record_id}:{TASK}:cancelled:-:3ms:{'c' * 16}:cat",
    ]


def test_an_expired_record_ends_as_expired_even_at_task_end(su, clock):
    reg = store(clock)
    activate(reg, su, window_seconds=10)
    clock.now += timedelta(seconds=11)
    reg.end_task(TASK, EndReason.TASK_COMPLETED)
    assert [e.resource.rsplit(":", 1)[1] for e in reg.drain(TASK)] == ["expired"]


def test_revoke(su):
    reg = store()
    record = activate(reg, su)
    assert reg.revoke(su, task_id=TASK) is record
    assert refusal(reg.revoke, su, task_id=TASK) is RefusalCode.NOT_FOUND
    assert [e.resource.rsplit(":", 1)[1] for e in reg.drain(TASK)] == ["revoked"]


def test_orphaned_rows_are_drained_for_tasks_nobody_drives(su, clock):
    reg = store(clock)
    other = uuid.uuid4()
    activate(reg, su, window_seconds=10)
    activate(reg, su, task_id=other, window_seconds=10)
    clock.now += timedelta(seconds=10)
    drained = reg.drain_except([other])
    assert [e.task_id for e in drained] == [TASK]
    assert [e.task_id for e in reg.drain(other)] == [other]


def test_task_statuses_map_to_end_reasons():
    assert _end_reason(AgentTaskStatus.COMPLETED, None) is EndReason.TASK_COMPLETED
    assert _end_reason(AgentTaskStatus.CANCELLED, None) is EndReason.TASK_CANCELLED
    assert _end_reason(AgentTaskStatus.FAILED, AgentFailureCode.EMERGENCY_STOP) is EndReason.BREAKER_TRIP
    for code in (None, AgentFailureCode.TIMEOUT, AgentFailureCode.STALLED):
        assert _end_reason(AgentTaskStatus.FAILED, code) is EndReason.TASK_FAILED


def test_enabling_logs_a_startup_warning(caplog):
    with caplog.at_level("WARNING", logger="hypermind.composition.break_glass"):
        store()
    assert "break-glass is ENABLED" in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING", logger="hypermind.composition.break_glass"):
        store(enabled=False)
    assert caplog.text == ""
