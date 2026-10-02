"""docs/29 §15.5 — Phase 5 slice 5C: the unattended trigger's arithmetic.

Pure: the occurrences since the last claimed one; at most the latest runs,
only within the grace; everything earlier is coalesced into one missed
record — never a catch-up storm. The day a run counts against is the
occurrence's calendar day in the delegation's own zone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from server.agents.triggers import NOTHING_DUE, expiry_notice_due, local_day, plan_occurrences

T0 = datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc)
GRACE = timedelta(minutes=15)


def hourly(after: datetime) -> datetime:
    return (after.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))


def test_nothing_is_due_before_the_first_occurrence():
    assert plan_occurrences(next_after=hourly, anchor=T0, now=T0 + timedelta(minutes=59), grace=GRACE) == NOTHING_DUE


def test_an_occurrence_within_the_grace_runs():
    plan = plan_occurrences(next_after=hourly, anchor=T0, now=T0 + timedelta(hours=1, minutes=5), grace=GRACE)
    assert (plan.run_at, plan.claim_through, plan.missed) == (T0 + timedelta(hours=1),) * 2 + (0,)


def test_a_long_outage_runs_once_and_coalesces_the_rest():
    now = T0 + timedelta(days=7, minutes=3)
    plan = plan_occurrences(next_after=hourly, anchor=T0, now=now, grace=GRACE)
    assert plan.run_at == T0 + timedelta(days=7) and plan.claim_through == plan.run_at
    assert plan.missed == 7 * 24 - 1


def test_past_the_grace_nothing_runs_and_everything_is_missed():
    plan = plan_occurrences(next_after=hourly, anchor=T0, now=T0 + timedelta(hours=3, minutes=30), grace=GRACE)
    assert (plan.run_at, plan.claim_through, plan.missed) == (None, T0 + timedelta(hours=3), 3)


def test_a_claimed_occurrence_is_never_planned_again():
    first = plan_occurrences(next_after=hourly, anchor=T0, now=T0 + timedelta(hours=1, minutes=1), grace=GRACE)
    again = plan_occurrences(next_after=hourly, anchor=first.claim_through, now=T0 + timedelta(hours=1, minutes=9),
                             grace=GRACE)
    assert again == NOTHING_DUE


def test_a_schedule_that_does_not_move_forward_is_not_followed():
    plan = plan_occurrences(next_after=lambda after: after, anchor=T0, now=T0 + timedelta(days=1), grace=GRACE)
    assert plan == NOTHING_DUE


def test_the_scan_is_bounded():
    def every_second(after: datetime) -> datetime:
        return after + timedelta(seconds=1)

    plan = plan_occurrences(next_after=every_second, anchor=T0, now=T0 + timedelta(days=30), grace=GRACE,
                            max_scan=1000)
    assert plan.claim_through == T0 + timedelta(seconds=1000) and plan.run_at is None


def test_the_day_is_the_delegations_own():
    # 01:30 UTC is 07:00 in Kolkata: that day starts at 18:30 UTC the evening before.
    start, end = local_day(datetime(2026, 10, 3, 1, 30, tzinfo=timezone.utc), "Asia/Kolkata")
    assert (start, end) == (datetime(2026, 10, 2, 18, 30, tzinfo=timezone.utc),
                            datetime(2026, 10, 3, 18, 30, tzinfo=timezone.utc))


def test_the_expiry_notice_comes_once_three_days_before():
    expires = T0 + timedelta(days=10)
    assert not expiry_notice_due(expires_at=expires, now=expires - timedelta(days=3, seconds=1), noticed_at=None)
    assert expiry_notice_due(expires_at=expires, now=expires - timedelta(days=3), noticed_at=None)
    assert not expiry_notice_due(expires_at=expires, now=expires - timedelta(days=1), noticed_at=T0)
    assert not expiry_notice_due(expires_at=expires, now=expires, noticed_at=None)
