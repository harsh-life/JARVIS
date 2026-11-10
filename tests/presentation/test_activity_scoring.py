"""Central AI's activity scorer (OD-CHAR-4, `server/presentation/activity.py`).

Pure-function scenarios for every behaviour the owner named — inactivity,
increasing interaction, sustained workload and recovery from it, failed
tasks, unexpected events — and for the lines the scorer must not cross:
absence is never sad or angry, a user's own cancellation is never a failure,
the request count never makes the character angry, and delegated runs are
JARVIS's work but not the user's interaction.
"""

from __future__ import annotations

import ast
import dataclasses
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from server.presentation.activity import (
    IDLE_FULL,
    IDLE_START,
    TaskFacts,
    score_activity,
)
from shared.schemas.presentation import ActivityPolicy, CharacterPresentationSettings, Expression

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
THRESHOLDS = CharacterPresentationSettings().thresholds


def task(
    minutes_ago: float = 0,
    status: str = "completed",
    *,
    from_user: bool = True,
    failure_code: str | None = None,
    model_calls: int = 3,
    tool_calls: int = 2,
    worker_switches: int = 0,
    ran_for: float = 1,
) -> TaskFacts:
    end = NOW - timedelta(minutes=minutes_ago)
    live = status in {"running", "awaiting_confirmation", "waiting_for_platform"}
    return TaskFacts(
        from_user=from_user, status=status, failure_code=failure_code,
        created_at=end - timedelta(minutes=ran_for), updated_at=end,
        finished_at=None if live else end,
        model_calls=model_calls, tool_calls=tool_calls, worker_switches=worker_switches,
    )


def marker(ended_ago: timedelta, at: datetime = NOW) -> TaskFacts:
    """A record that only dates activity: a delegated run cancelled with no
    work counts for nothing but its timestamp."""

    end = at - ended_ago
    return TaskFacts(from_user=False, status="cancelled", failure_code=None, created_at=end, updated_at=end,
                     finished_at=end, model_calls=0, tool_calls=0, worker_switches=0)


def scores(tasks: list[TaskFacts], *, idle_for: timedelta | None = None, at: datetime = NOW):
    """`idle_for`: the last activity was this long ago (a marker record at that age)."""

    extra = [marker(idle_for, at)] if idle_for is not None else []
    return score_activity(tasks + extra, at).scores


def chosen(tasks: list[TaskFacts]) -> Expression:
    """What a client shows on this report alone (the user not recently present)."""

    policy = ActivityPolicy(CharacterPresentationSettings(), 0)
    report = score_activity(tasks, NOW)
    policy.on_report(report, 10_000_000)
    return policy.shown


def eligible(s, state: str) -> bool:
    return s.of(Expression(state)) >= THRESHOLDS.of(Expression(state))


# --- inactivity ------------------------------------------------------------------


def test_nothing_at_all_is_fully_sleepy_and_nothing_else():
    s = scores([], idle_for=None)
    assert s.sleepy == 10
    assert [s.happy, s.satisfied, s.surprised, s.angry, s.sad] == [1, 1, 1, 1, 1]
    assert s.default == 5


def test_sleepiness_rises_with_idle_time_and_only_after_it_starts():
    levels = [scores([], idle_for=timedelta(minutes=m)).sleepy for m in range(0, 13)]
    assert levels == sorted(levels)                          # monotonic
    assert scores([], idle_for=IDLE_START).sleepy == 1
    assert scores([], idle_for=IDLE_FULL).sleepy == 10
    assert not eligible(scores([], idle_for=timedelta(minutes=5)), "sleepy")
    assert eligible(scores([], idle_for=timedelta(minutes=9)), "sleepy")


def test_a_live_task_is_never_sleepy():
    s = score_activity([task(40, "awaiting_confirmation", ran_for=200)], NOW).scores
    assert s.sleepy == 1


def test_absence_is_never_sad_or_angry():
    for minutes in (0, 5, 30, 600, 6000):
        s = scores([], idle_for=timedelta(minutes=minutes))
        assert (s.sad, s.angry, s.happy) == (1, 1, 1)


# --- interaction and productive work -----------------------------------------------------


def test_increasing_interaction_moves_from_satisfied_to_happy():
    one = scores([task(0)])
    three = scores([task(0), task(2), task(4)])
    five = scores([task(0), task(2), task(4), task(6), task(8)])
    assert one.satisfied == 8 and not eligible(one, "happy")
    assert three.happy >= 8 and five.happy == 10
    assert one.happy < three.happy <= five.happy
    assert chosen([task(0)]) is Expression.SATISFIED
    assert chosen([task(0), task(2), task(4), task(6), task(8)]) is Expression.HAPPY


def test_frequent_requests_without_completed_work_are_not_happy():
    chatter = [task(i, "cancelled", model_calls=0, tool_calls=0) for i in range(8)]
    assert not eligible(scores(chatter), "happy")


def test_work_that_used_tools_counts_more_than_answers():
    assert scores([task(0), task(1), task(2)]).happy > scores(
        [task(0, tool_calls=0), task(1, tool_calls=0), task(2, tool_calls=0)]
    ).happy


def test_productive_evidence_fades_back_to_baseline():
    burst = [task(0), task(2), task(4), task(6), task(8)]
    later = [dataclasses.replace(t) for t in burst]
    s = scores(later, at=NOW + timedelta(minutes=45), idle_for=timedelta(minutes=45))
    assert not eligible(s, "happy") and not eligible(s, "satisfied")


def test_delegated_runs_are_work_but_not_interaction():
    delegated = [task(i, from_user=False) for i in range(5)]
    report = score_activity(delegated, NOW)
    assert report.components.engagement == 0
    assert report.components.productivity > 0
    assert not eligible(report.scores, "happy")              # happy needs the user's engagement


# --- workload, overload and recovery ---------------------------------------------------------


def test_sustained_heavy_work_is_angry_and_outranks_happy():
    heavy = [task(i * 2, model_calls=16, tool_calls=24) for i in range(4)]
    s = scores(heavy)
    assert eligible(s, "angry")
    assert s.angry > s.happy and s.angry > s.satisfied
    assert chosen(heavy) is Expression.ANGRY


def test_concurrent_live_work_alone_is_overload():
    s = scores([task(0, "running", from_user=False, model_calls=0, tool_calls=0) for _ in range(3)])
    assert s.angry == 10


def test_ceiling_and_rate_limit_stops_are_strain():
    s = scores([task(0, "failed", failure_code="rate_limited"), task(1, "failed", failure_code="rate_limited")])
    assert eligible(s, "angry")


def test_overload_recovers_as_the_work_fades():
    heavy = [task(i * 2, model_calls=16, tool_calls=24) for i in range(4)]
    angry = [scores(heavy, at=NOW + timedelta(minutes=m)).angry for m in (0, 5, 10, 20, 30)]
    assert angry == sorted(angry, reverse=True)
    assert angry[0] >= 8 and angry[-1] <= 3


def test_the_request_count_never_makes_the_character_angry():
    """Many requests that made JARVIS do no work: engagement, never strain."""

    requests = [task(i * 0.5, "completed", model_calls=0, tool_calls=0) for i in range(40)]
    report = score_activity(requests, NOW)
    assert report.components.engagement == 100
    assert report.scores.angry == 1


# --- failure ---------------------------------------------------------------------------------


def test_a_fresh_failure_is_sad_and_fades():
    fresh = scores([task(0, "failed", failure_code="max_iterations_exceeded")])
    assert eligible(fresh, "sad")
    later = scores([task(6, "failed", failure_code="max_iterations_exceeded")])
    assert later.sad < fresh.sad and not eligible(later, "sad")


def test_failure_suppresses_the_positive_states():
    s = scores([task(0), task(0.5, "failed", failure_code="max_tool_calls_exceeded")])
    assert not eligible(s, "happy") and not eligible(s, "satisfied")


def test_a_completion_after_a_failure_is_recovery():
    s = scores([task(2, "failed", failure_code="stalled"), task(0)])
    assert s.sad <= 3                                         # below the exit band (8 - 2)


def test_a_users_own_cancellation_is_never_a_failure():
    s = scores([task(0, "cancelled"), task(1, "cancelled"), task(2, "cancelled")])
    assert s.sad == 1 and s.surprised == 1
    assert score_activity([task(0, "cancelled")], NOW).components.failure == 0


# --- unexpected events ------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["internal_error", "stalled", "emergency_stop", "model_unavailable"])
def test_an_unexpected_stop_is_surprising_then_sad(code):
    s = scores([task(0, "failed", failure_code=code)])
    assert s.surprised == 10 and eligible(s, "sad")
    assert chosen([task(0, "failed", failure_code=code)]) is Expression.SURPRISED


def test_surprise_is_brief():
    assert scores([task(1, "failed", failure_code="internal_error")]).surprised < THRESHOLDS.surprised


def test_a_dependency_going_away_is_surprising():
    assert scores([task(0, "waiting_for_platform")]).surprised == 10


def test_a_worker_switch_counts_once_when_its_task_ends():
    assert scores([task(0, "running", worker_switches=1)]).surprised == 1
    assert scores([task(0, "completed", worker_switches=1)]).surprised == 10


def test_an_expected_failure_is_not_a_surprise():
    assert scores([task(0, "failed", failure_code="max_iterations_exceeded")]).surprised == 1


# --- determinism and bounds -------------------------------------------------------------------

STATUSES = ["running", "awaiting_confirmation", "waiting_for_platform", "completed", "failed", "cancelled"]
CODES = [None, "internal_error", "rate_limited", "max_tool_calls_exceeded", "stalled", "budget_exceeded"]


@pytest.mark.parametrize("seed", range(40))
def test_any_window_scores_in_range_and_identically_twice(seed):
    rng = random.Random(seed)
    tasks = [
        task(rng.uniform(-1, 40), rng.choice(STATUSES), from_user=rng.random() < 0.7,
             failure_code=rng.choice(CODES), model_calls=rng.randint(0, 16),
             tool_calls=rng.randint(0, 24), worker_switches=rng.randint(0, 2))
        for _ in range(rng.randint(0, 60))
    ]
    first = score_activity(tasks, NOW)
    assert first == score_activity(list(reversed(tasks)), NOW)          # same rows, any order
    assert first == score_activity(tasks, NOW)
    for value in first.scores.model_dump().values():
        assert 1 <= value <= 10
    for value in first.components.model_dump().values():
        assert 0 <= value <= 100
    assert first.scores.default == 5


def test_a_future_timestamp_counts_as_now():
    skewed = task(-5, "failed", failure_code="internal_error")
    assert scores([skewed]).surprised == 10


# --- the 30-minute boundary (OD-CHAR-4, owner correction of 2026-11-10) ---------------------------


def test_nothing_in_the_window_and_nothing_live_is_fully_sleepy_and_nothing_else():
    for ago in (timedelta(minutes=31), timedelta(hours=3), timedelta(days=2)):
        s = scores([], idle_for=ago)
        assert s.sleepy == 10, ago
        assert [s.happy, s.satisfied, s.surprised, s.angry, s.sad] == [1, 1, 1, 1, 1], ago


def test_the_window_edge_is_exactly_thirty_minutes():
    edge = score_activity([task(30, "failed", failure_code="stalled", ran_for=0)], NOW)
    past = score_activity([task(30 + 1 / 60, "failed", failure_code="stalled", ran_for=0)], NOW)
    assert edge.components.failure > 0 and edge.components.inactivity == 100
    assert past.components.failure == 0 and past.components.inactivity == 100


def test_work_older_than_thirty_minutes_changes_nothing():
    recent = [task(1), task(4, "failed", failure_code="max_tool_calls_exceeded")]
    old = [
        task(31, "failed", failure_code="internal_error"),
        task(45, "failed", failure_code="rate_limited"),
        task(60, model_calls=16, tool_calls=24),
        task(90, "completed", worker_switches=2),
        task(600, "failed", failure_code="emergency_stop"),
    ]
    assert score_activity(recent + old, NOW) == score_activity(recent, NOW)
    assert score_activity(old, NOW) == score_activity([], NOW)


@pytest.mark.parametrize("code", ["internal_error", "stalled", "max_iterations_exceeded", "rate_limited"])
def test_an_old_failure_is_neither_sad_nor_surprising(code):
    s = score_activity([task(31, "failed", failure_code=code)], NOW).scores
    assert (s.sad, s.surprised, s.angry) == (1, 1, 1)
    assert s.sleepy == 10


def test_live_work_counts_however_long_ago_it_last_changed():
    live = [task(120, "running", from_user=False, model_calls=0, tool_calls=0, ran_for=10) for _ in range(3)]
    s = score_activity(live, NOW).scores
    assert s.angry == 10 and s.sleepy == 1
    waiting = score_activity([task(240, "awaiting_confirmation", ran_for=5)], NOW).scores
    assert waiting.sleepy == 1


def test_the_pure_scorer_cannot_be_widened_by_passing_older_rows():
    from server.presentation.activity import qualifies

    assert qualifies(task(29), NOW) and not qualifies(task(31, ran_for=0), NOW)
    assert qualifies(task(500, "running", ran_for=0), NOW)


async def test_measurement_issues_exactly_one_query_and_no_historical_lookup():
    """`measure` reads the window once; with nothing found it does not look
    further back for an older timestamp."""

    from server.presentation.activity import WINDOW, measure

    statements = []

    class _Result:
        def all(self):
            return []

    class _Session:
        async def execute(self, statement):
            statements.append(str(statement.compile(compile_kwargs={"literal_binds": True})))
            return _Result()

    report = await measure(_Session(), uuid.uuid4(), NOW)
    assert len(statements) == 1
    sql = statements[0].lower()
    assert "max(" not in sql and "agent_tasks" in sql
    since = (NOW - WINDOW).strftime("%Y-%m-%d %H:%M:%S")
    assert since in statements[0]                                 # the only time bound is the window
    assert report.scores.sleepy == 10 and report.components.inactivity == 100


def test_the_scorer_has_no_lookback_beyond_the_window():
    source = (ROOT / "server/presentation/activity.py").read_text()
    assert "LOOKBACK" not in source and "func." not in source and "hours=" not in source
    assert source.count("session.execute(") == 1


# --- what the scorer can see ------------------------------------------------------------------


def test_the_scorer_sees_operational_fields_only():
    """No text, result, identity or device field reaches the scorer."""

    fields = {f.name for f in dataclasses.fields(TaskFacts)}
    assert fields == {"from_user", "status", "failure_code", "created_at", "updated_at", "finished_at",
                      "model_calls", "tool_calls", "worker_switches"}


def test_the_scorer_selects_only_operational_columns_and_writes_nothing():
    source = (ROOT / "server/presentation/activity.py").read_text()
    tree = ast.parse(source)
    columns = {
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "AgentTask"
    }
    assert columns <= {"session_id", "status", "failure_code", "created_at", "updated_at", "finished_at",
                       "model_calls", "tool_calls", "worker_switches", "user_id"}
    assert "response" not in columns
    for verb in ("insert(", "update(", "delete(", ".add(", ".commit(", "logger", "logging"):
        assert verb not in source, verb


def test_the_scorer_imports_nothing_that_decides_or_acts():
    """Mirrors the import-linter contract "The activity scorer reads task rows
    and nothing else (OD-CHAR-4)"."""

    tree = ast.parse((ROOT / "server/presentation/activity.py").read_text())
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    servers = {m for m in modules if m and m.startswith("server.")}
    assert servers == {"server.storage.models"}
