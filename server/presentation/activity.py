"""Central AI activity scoring for the character (OD-CHAR-4).

The character expresses JARVIS's *operational activity*. This module is the
server's ("Central AI's") half: it reads the caller's own task records — rows
the runtime already keeps in `agent_tasks` for operational reasons — and
turns a bounded, decaying window of them into an `ActivityPresentation`: a
1–10 score per expression and the 0–100 components behind them. Clients turn
a stream of these into one shown expression with `ActivityPolicy`
(`shared/schemas/presentation.py`), which owns hysteresis and cooldowns.

What it is, and is not:

* **Deterministic code, not a model.** No LLM or classifier is involved; the
  same rows at the same instant give the same report (`score_activity` is
  pure). Every constant is below, and `docs/CHARACTER_PRESENTATION.md` §6
  states the formulas.
* **Operational, not personal.** Its inputs are counts, statuses, failure
  codes, timestamps and the runtime's own work counters. It never reads task
  text, results, memory, the vault or anything about the user beyond "asked
  for something at time t". Nothing is stored, logged or cached: the report
  is computed per request and returned to the owner's own client only.
* **Bounded.** Only `WINDOW` of history counts (at most `MAX_ROWS` rows), plus
  one timestamp — the latest activity within `LOOKBACK` — for inactivity.
  Older history cannot influence anything, so there is no longer-term
  profile to build (EMO-001/004 as amended by OD-CHAR-4).
* **Never an authority.** Nothing that authorizes, executes, confirms,
  classifies risk or meters budgets reads it (import-linter: "Character
  presentation never reaches authority", "Presentation settings are a store,
  not an authority").

Semantics the formulas keep (OD-CHAR-4):

* inactivity produces `sleepy` only — absence is never sad or angry;
* the user's own cancellations count as nothing at all — a user decision is
  never presented as a failure;
* `angry` reads only JARVIS's own workload (live tasks, model and tool calls,
  ceiling and rate-limit stops) — the request count is not one of its inputs,
  so it presents strain, not anger at anyone;
* unattended (delegated) runs are JARVIS's work but not the user's
  interaction: they count towards productivity, failure and pressure, never
  towards engagement.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.storage.models import AgentTask
from shared.schemas.agent import AgentFailureCode, AgentTaskStatus
from shared.schemas.presentation import (
    COMPONENT_MAX,
    SCORE_MAX,
    SCORE_MIN,
    ActivityComponents,
    ActivityPresentation,
    ActivityScores,
)

# --- the window --------------------------------------------------------------

WINDOW = timedelta(minutes=30)
LOOKBACK = timedelta(hours=24)
MAX_ROWS = 500

# --- half-lives: how fast each kind of evidence fades --------------------------

ENGAGEMENT_HALF_LIFE = timedelta(minutes=10)
OUTCOME_HALF_LIFE = timedelta(minutes=10)
FAILURE_HALF_LIFE = timedelta(minutes=5)
WORK_HALF_LIFE = timedelta(minutes=10)
SURPRISE_HALF_LIFE = timedelta(seconds=45)

# --- saturation points: the decayed amount that counts as "fully" ------------

ENGAGEMENT_SATURATION = 4.0      # ~4 requests in the last few minutes
PRODUCTIVITY_SATURATION = 3.0    # ~3 substantive completions
STEADINESS_SATURATION = 1.0      # one recent completion
FAILURE_SATURATION = 1.25        # one fresh failure is 0.8
CONCURRENCY_SATURATION = 3.0     # live tasks at once
WORK_SATURATION = 160.0          # model + tool calls (a task's ceilings are 16 + 24)
STRAIN_SATURATION = 2.0          # ceiling / rate-limit stops
SURPRISE_SATURATION = 1.0        # one fresh unexpected event

# A completion that used no tool (an answer) is still work, but less of it.
ANSWER_ONLY_WEIGHT = 0.6

# Inactivity rises linearly from IDLE_START to full at IDLE_FULL.
IDLE_START = timedelta(minutes=2)
IDLE_FULL = timedelta(minutes=10)

# The neutral baseline's fixed score: below every other state's default
# threshold, so any state with enough evidence outranks it.
DEFAULT_LEVEL = 0.45
# `satisfied` is the steady-operation state; its ceiling keeps it below a
# fully-evidenced `happy` (score 8 at most, which `happy` wins on a tie).
SATISFIED_CEILING = 0.78
# A live task under low pressure is steady operation.
WORKING_LEVEL = 0.7
WORKING_PRESSURE_LIMIT = 0.5
# How much one recent completion offsets recent failure in `sad`.
RECOVERY_WEIGHT = 0.6

_LIVE = frozenset({
    AgentTaskStatus.RUNNING.value,
    AgentTaskStatus.AWAITING_CONFIRMATION.value,
    AgentTaskStatus.WAITING_FOR_PLATFORM.value,
})
# Stops at a ceiling or a rate/budget limit: JARVIS was asked for more than it
# could do. Counted as workload strain (and, being failures, as failure).
STRAIN_CODES = frozenset(code.value for code in (
    AgentFailureCode.MAX_ITERATIONS,
    AgentFailureCode.MAX_MODEL_CALLS,
    AgentFailureCode.MAX_TOOL_CALLS,
    AgentFailureCode.TIMEOUT,
    AgentFailureCode.RATE_LIMITED,
    AgentFailureCode.BUDGET_EXCEEDED,
    AgentFailureCode.AGENT_BUDGET_EXHAUSTED,
))
# Stops nobody planned for: an internal error, a stall, the circuit breaker, a
# lost dependency or state. Counted as a surprise (and as failure).
UNEXPECTED_CODES = frozenset(code.value for code in (
    AgentFailureCode.INTERNAL_ERROR,
    AgentFailureCode.STALLED,
    AgentFailureCode.WORKER_CHAIN_EXHAUSTED,
    AgentFailureCode.EMERGENCY_STOP,
    AgentFailureCode.CONFIRMATION_STATE_LOST,
    AgentFailureCode.PLATFORM_UNAVAILABLE,
    AgentFailureCode.PRINCIPAL_REVOKED,
    AgentFailureCode.MODEL_UNAVAILABLE,
    AgentFailureCode.SPEC_CHANGED,
))


@dataclass(frozen=True)
class TaskFacts:
    """What the scorer may know about one task: operational fields only."""

    from_user: bool          # a present user's request (device), not a delegated run
    status: str
    failure_code: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    model_calls: int
    tool_calls: int
    worker_switches: int


# --- the pure scorer ----------------------------------------------------------


def _decay(now: datetime, at: datetime, half_life: timedelta) -> float:
    """1.0 for something that happened now, 0.5 one half-life ago. A time in
    the future (clock skew) counts as now."""

    age = max((now - at).total_seconds(), 0.0)
    return 0.5 ** (age / half_life.total_seconds())


def _clamp(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def _score(level: float) -> int:
    """A 0–1 level onto the 1–10 scale, rounded half up."""

    return SCORE_MIN + math.floor((SCORE_MAX - SCORE_MIN) * _clamp(level) + 0.5)


def _order(t: TaskFacts) -> tuple:
    return (t.updated_at, t.created_at, t.finished_at or t.updated_at, t.status, t.failure_code or "",
            t.model_calls, t.tool_calls, t.worker_switches, t.from_user)


def _percent(level: float) -> int:
    return math.floor(COMPONENT_MAX * _clamp(level) + 0.5)


def score_activity(
    tasks: list[TaskFacts],
    last_activity: datetime | None,
    now: datetime,
) -> ActivityPresentation:
    """The activity report for these tasks at `now`. Pure and deterministic.

    `tasks` are the caller's tasks touching the window (created, updated or
    finished within it, or still live); `last_activity` is the latest
    activity within the lookback, or None.
    """

    engagement = productivity = steadiness = failure = work = strain = surprise = 0.0
    live = 0
    # A fixed order, so floating-point sums cannot depend on how rows arrived.
    for t in sorted(tasks, key=_order):
        if t.status in _LIVE:
            live += 1
        if t.from_user:
            engagement += _decay(now, t.created_at, ENGAGEMENT_HALF_LIFE)
        work += (t.model_calls + t.tool_calls) * _decay(now, t.updated_at, WORK_HALF_LIFE)
        if t.status == AgentTaskStatus.WAITING_FOR_PLATFORM.value:
            surprise += _decay(now, t.updated_at, SURPRISE_HALF_LIFE)
        ended = t.finished_at or t.updated_at
        if t.worker_switches > 0 and t.status not in _LIVE:
            # The runtime records no switch time, and a live row's updated_at
            # moves with every step, so a switch counts once: when its task ends.
            surprise += _decay(now, ended, SURPRISE_HALF_LIFE)
        if t.status == AgentTaskStatus.COMPLETED.value:
            d = _decay(now, ended, OUTCOME_HALF_LIFE)
            productivity += d * (1.0 if t.tool_calls > 0 else ANSWER_ONLY_WEIGHT)
            steadiness += d
        elif t.status == AgentTaskStatus.FAILED.value:
            failure += _decay(now, ended, FAILURE_HALF_LIFE)
            if t.failure_code in STRAIN_CODES:
                strain += _decay(now, ended, OUTCOME_HALF_LIFE)
            if t.failure_code in UNEXPECTED_CODES:
                surprise += _decay(now, ended, SURPRISE_HALF_LIFE)
        # CANCELLED is the user's own decision: it counts as nothing.

    e = _clamp(engagement / ENGAGEMENT_SATURATION)
    p = _clamp(productivity / PRODUCTIVITY_SATURATION)
    st = _clamp(steadiness / STEADINESS_SATURATION)
    f = _clamp(failure / FAILURE_SATURATION)
    pressure = _clamp(max(
        live / CONCURRENCY_SATURATION,
        work / WORK_SATURATION,
        strain / STRAIN_SATURATION,
    ))
    sur = _clamp(surprise / SURPRISE_SATURATION)
    if live > 0:
        idle = 0.0
    elif last_activity is None:
        idle = 1.0
    else:
        idle_for = max(now - last_activity, timedelta(0))
        idle = _clamp((idle_for - IDLE_START) / (IDLE_FULL - IDLE_START))

    working = WORKING_LEVEL if live > 0 and pressure < WORKING_PRESSURE_LIMIT else 0.0
    # Above the limit, workload pressure damps the positive states, so a
    # heavily loaded JARVIS presents strain rather than delight.
    overload = _clamp((pressure - WORKING_PRESSURE_LIMIT) / (1.0 - WORKING_PRESSURE_LIMIT))
    levels = {
        "happy": math.sqrt(e * p) * (1.0 - f) * (1.0 - overload),
        "satisfied": min(SATISFIED_CEILING, max(st, working)) * (1.0 - f) * (1.0 - overload),
        "default": DEFAULT_LEVEL,
        "sleepy": idle,
        "surprised": sur,
        "angry": pressure,
        # A completion after a failure is recovery: it lifts sadness quickly.
        "sad": f * (1.0 - RECOVERY_WEIGHT * st),
    }
    return ActivityPresentation(
        computed_at=now,
        window_s=int(WINDOW.total_seconds()),
        scores=ActivityScores(**{state: _score(level) for state, level in levels.items()}),
        components=ActivityComponents(
            engagement=_percent(e),
            productivity=_percent(p),
            steadiness=_percent(st),
            failure=_percent(f),
            pressure=_percent(pressure),
            surprise=_percent(sur),
            inactivity=_percent(idle),
        ),
    )


# --- measurement (read-only) ----------------------------------------------------


def _aware(value: datetime | None) -> datetime | None:
    # SQLite hands timezone-aware columns back naive; they were written in UTC.
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


async def measure(session: AsyncSession, user_id: uuid.UUID, now: datetime) -> ActivityPresentation:
    """Read the caller's own task rows for the window and score them. SELECT
    only, owner-scoped; nothing is written, cached or logged."""

    since = now - WINDOW
    mine = AgentTask.user_id == user_id
    rows = (await session.execute(
        select(
            AgentTask.session_id, AgentTask.status, AgentTask.failure_code,
            AgentTask.created_at, AgentTask.updated_at, AgentTask.finished_at,
            AgentTask.model_calls, AgentTask.tool_calls, AgentTask.worker_switches,
        )
        .where(mine, or_(
            AgentTask.created_at >= since,
            AgentTask.updated_at >= since,
            AgentTask.finished_at >= since,
            AgentTask.status.in_(_LIVE),
        ))
        .order_by(AgentTask.updated_at.desc())
        .limit(MAX_ROWS)
    )).all()
    tasks = [
        TaskFacts(
            from_user=row.session_id is not None,
            status=row.status,
            failure_code=row.failure_code,
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
            finished_at=_aware(row.finished_at),
            model_calls=row.model_calls,
            tool_calls=row.tool_calls,
            worker_switches=row.worker_switches,
        )
        for row in rows
    ]
    last_activity = max((t.updated_at for t in tasks), default=None)
    if last_activity is None:
        last_activity = _aware((await session.execute(
            select(func.max(AgentTask.updated_at)).where(mine, AgentTask.updated_at >= now - LOOKBACK)
        )).scalar_one_or_none())
    return score_activity(tasks, last_activity, now)
