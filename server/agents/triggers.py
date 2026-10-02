"""The unattended trigger's arithmetic — docs/29 §15.5 (Phase 5).

OD-AF-2 (ratified 2026-10-02, docs/DECISION_REGISTER.md §2K): an agent with
an active StandingDelegation runs unattended on exactly its compiled
schedule. **This is not the scheduler** — the scheduler still delivers
reminders and executes nothing (AF-C4, docs/22 §0); the Agent Factory reads
its own compiled schedule here, and the composition root's trigger loop
(`server/composition/agent_triggers.py`) acts on the answer.

Pure: no store, no clock of its own, no schedule parser (the caller passes
`next_after`, built from the compiled cron and IANA zone). Two rules live
here:

* **Misfires** (docs/29 §15.5): of the occurrences since the last claimed one,
  at most the *latest* runs, and only if it is still within the grace; every
  earlier one is coalesced into a single "missed" record. A server that was
  down for a week runs once, never seven times (no catch-up storm).
* **The day** for `max_runs_per_day` is the calendar day of the occurrence in
  the delegation's own time zone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

# Bounds the scan of a very frequent schedule after a very long outage; what
# lies beyond is claimed by the next pass (and coalesced, never run).
MAX_SCAN = 100_000
# docs/29 §15.4: the owner is told this long before a delegation expires.
EXPIRY_NOTICE = timedelta(days=3)


@dataclass(frozen=True)
class OccurrencePlan:
    """`claim_through`: the latest occurrence at or before now — the value the
    delegation's `last_occurrence_at` moves to, run or not (`None`: nothing
    is due). `run_at`: the occurrence to run now (`None`: none, e.g. the
    latest one is past the grace). `missed`: how many occurrences are
    coalesced without a run."""

    claim_through: datetime | None
    run_at: datetime | None
    missed: int


NOTHING_DUE = OccurrencePlan(claim_through=None, run_at=None, missed=0)


def plan_occurrences(*, next_after: Callable[[datetime], datetime | None], anchor: datetime, now: datetime,
                     grace: timedelta, max_scan: int = MAX_SCAN) -> OccurrencePlan:
    """The occurrences after `anchor` (the last claimed occurrence, or the
    delegation's creation) up to `now`: run the latest within `grace`,
    coalesce the rest."""

    latest: datetime | None = None
    count = 0
    cursor = anchor
    while count < max_scan:
        upcoming = next_after(cursor)
        if upcoming is None or upcoming > now:
            break
        if upcoming <= cursor:  # a schedule must move forward; anything else is refused
            break
        latest, cursor, count = upcoming, upcoming, count + 1
    if latest is None:
        return NOTHING_DUE
    if now - latest <= grace:
        return OccurrencePlan(claim_through=latest, run_at=latest, missed=count - 1)
    return OccurrencePlan(claim_through=latest, run_at=None, missed=count)


def local_day(occurrence: datetime, zone: str) -> tuple[datetime, datetime]:
    """[start, end) of the occurrence's calendar day in `zone`, in UTC."""

    tz = ZoneInfo(zone)
    local = occurrence.astimezone(tz)
    start = datetime.combine(local.date(), time.min, tzinfo=tz)
    end = datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def expiry_notice_due(*, expires_at: datetime, now: datetime, noticed_at: datetime | None) -> bool:
    """docs/29 §15.4: once, from three days before the expiry."""

    return noticed_at is None and expires_at - EXPIRY_NOTICE <= now < expires_at


__all__ = [
    "EXPIRY_NOTICE",
    "MAX_SCAN",
    "NOTHING_DUE",
    "OccurrencePlan",
    "expiry_notice_due",
    "local_day",
    "plan_occurrences",
]
