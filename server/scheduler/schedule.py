"""What a `ScheduledJob.schedule` string means (01 §6.2: "cron or ISO datetime").

Two forms, nothing else:

* **one-shot** — an ISO-8601 datetime **with** an offset
  (`2026-10-01T09:00:00+05:30`, `...Z`). A naive datetime is refused: whose
  09:00 it is would be a guess.
* **recurring** — a standard 5-field crontab, optionally prefixed with
  `CRON_TZ=<IANA zone> ` (the cron convention) to say whose clock it runs on;
  without the prefix it is UTC.

The timezone travels inside the string so the `[LOCKED]` entity keeps exactly
its one `schedule` field. Semantics are APScheduler's triggers (docs/22 §3,
"APScheduler-class"), so DST transitions and month lengths are handled by a
well-tested implementation rather than a hand-rolled one.

Every instant leaving this module is timezone-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger

MAX_SCHEDULE_CHARS = 128
CRON_TZ_PREFIX = "CRON_TZ="
# How many upcoming occurrences a recurrence check inspects. Cron gaps are
# irregular (`0,1 9 * * *`), so one gap is not enough; a bounded look-ahead is.
_RECURRENCE_SAMPLE = 48
_ONE_SECOND = timedelta(seconds=1)


class ScheduleError(ValueError):
    """The schedule is malformed or outside the configured bounds. `reason` is
    a stable identifier; the message is safe to show the user."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def as_utc(value: datetime) -> datetime:
    """A stored instant: naive values from SQLite are UTC by construction."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class Schedule:
    raw: str
    recurring: bool
    at: datetime | None = None
    trigger: CronTrigger | None = None

    def next_after(self, instant: datetime) -> datetime | None:
        """The first occurrence strictly after `instant`, or `None`."""

        instant = as_utc(instant)
        if not self.recurring:
            assert self.at is not None
            return self.at if self.at > instant else None
        assert self.trigger is not None
        fire = self.trigger.get_next_fire_time(None, instant + _ONE_SECOND)
        return as_utc(fire) if fire is not None else None

    def first_at_or_after(self, instant: datetime) -> datetime | None:
        instant = as_utc(instant)
        if not self.recurring:
            assert self.at is not None
            return self.at if self.at >= instant else None
        assert self.trigger is not None
        fire = self.trigger.get_next_fire_time(None, instant)
        return as_utc(fire) if fire is not None else None

    def occurrences_between(self, start: datetime, end: datetime, *, limit: int) -> list[datetime]:
        """Occurrences with `start <= t <= end`, at most `limit` of them (the
        earliest ones)."""

        found: list[datetime] = []
        current = self.first_at_or_after(start)
        end = as_utc(end)
        while current is not None and current <= end and len(found) < limit:
            found.append(current)
            current = self.next_after(current)
        return found


def parse_schedule(raw: str) -> Schedule:
    """Syntax only. `ScheduleError` for anything that is not one of the two
    accepted forms."""

    if not isinstance(raw, str):
        raise ScheduleError("schedule_invalid", "schedule must be a string")
    text = raw.strip()
    if not text:
        raise ScheduleError("schedule_invalid", "schedule is empty")
    if len(text) > MAX_SCHEDULE_CHARS:
        raise ScheduleError("schedule_invalid", "schedule is too long")

    if text[0].isdigit() and ("T" in text or "t" in text) and ":" in text:
        return _parse_one_shot(text)
    return _parse_cron(text)


def _parse_one_shot(text: str) -> Schedule:
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        raise ScheduleError("schedule_invalid", "not a valid ISO-8601 datetime") from None
    if at.tzinfo is None or at.utcoffset() is None:
        raise ScheduleError(
            "schedule_timezone_missing",
            "a one-time reminder needs a UTC offset (e.g. 2026-10-01T09:00:00+05:30)",
        )
    return Schedule(raw=text, recurring=False, at=at.astimezone(timezone.utc))


def _parse_cron(text: str) -> Schedule:
    zone_name = "UTC"
    expression = text
    if text.startswith(CRON_TZ_PREFIX):
        head, _, expression = text.partition(" ")
        zone_name = head[len(CRON_TZ_PREFIX):]
        expression = expression.strip()
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError("schedule_timezone_unknown", "unknown CRON_TZ time zone") from None
    if len(expression.split()) != 5:
        raise ScheduleError(
            "schedule_invalid", "expected an ISO-8601 datetime or a 5-field cron expression"
        )
    try:
        trigger = CronTrigger.from_crontab(expression, timezone=zone)
    except (ValueError, TypeError):
        raise ScheduleError("schedule_invalid", "not a valid cron expression") from None
    return Schedule(raw=text, recurring=True, trigger=trigger)


def first_fire(
    schedule: Schedule, *, now: datetime, min_recurrence: timedelta, max_horizon: timedelta
) -> datetime:
    """Validate a *new* job's schedule against the configured bounds and return
    its first due instant. A job that would never fire, fire in the past, fire
    beyond the horizon, or recur faster than allowed is refused up front."""

    now = as_utc(now)
    first = schedule.next_after(now)
    if first is None:
        raise ScheduleError("schedule_in_past", "that time has already passed")
    if first - now > max_horizon:
        raise ScheduleError("schedule_too_far", "that is further ahead than reminders may be set")
    if schedule.recurring:
        samples = [first]
        while len(samples) < _RECURRENCE_SAMPLE:
            following = schedule.next_after(samples[-1])
            if following is None:
                break
            samples.append(following)
        gaps = [b - a for a, b in zip(samples, samples[1:])]
        if gaps and min(gaps) < min_recurrence:
            raise ScheduleError(
                "schedule_too_frequent",
                f"a recurring reminder may fire at most every {int(min_recurrence.total_seconds() // 60)} minutes",
            )
    return first


__all__ = [
    "MAX_SCHEDULE_CHARS",
    "Schedule",
    "ScheduleError",
    "as_utc",
    "first_fire",
    "parse_schedule",
]
