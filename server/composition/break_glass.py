"""Break-glass records — 20 §2 (OD-EXEC-2).

Unconfined execution is not a mode of the server. It exists only as a record
here: bound to one live task, its owning user, an explicit set of executables
from the operator's separate break-glass list, a number of invocations, and a
time window. The process executor asks this store — through
`server.execution.break_glass.BreakGlassLookup`, which offers `claim` and
nothing else — whether one specific child may run without the kernel layer.

Who can do what:

* **create** — `prepare` then `install`, which require a verified `SuperuserPrincipal`
  (only `server.gateway.superuser_auth` mints one). Reached from the superuser
  control route alone (`server/composition/supervisor.py`). No user, worker,
  model, prompt, recovery path or tool can reach it: the runtime and the
  executor are handed ports that do not have it, and neither may import this
  module (pyproject import contracts).
* **spend** — `claim`, by the executor, one invocation per matching run.
* **end** — at the first of: invocations exhausted, expiry, task end
  (completed / failed / cancelled), a breaker trip, or a superuser revoke.

Records live in this process's memory only. A restart ends them all — the
fail-closed direction: nothing survives to be claimed by a task that no
longer exists.

Every activation, invocation and end is audited. Activations are written by
the control route itself; invocations and ends are queued here and written by
whichever request next settles the task (`drain`) — the task's own request
right after the run, or at the task's end, or the control route.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Iterable

from server.config.schema import BreakGlassConfig
from server.execution.break_glass import BreakGlassClaim, InvocationOutcome
from server.gateway.superuser_auth import SuperuserPrincipal

logger = logging.getLogger("hypermind.composition.break_glass")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EndReason(str, Enum):
    EXHAUSTED = "exhausted"
    EXPIRED = "expired"
    REVOKED = "revoked"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    BREAKER_TRIP = "breaker_trip"


class RefusalCode(str, Enum):
    DISABLED = "disabled"                    # execution.process.break_glass.enabled is false
    NOT_SUPERUSER = "not_superuser"
    TASK_NOT_LIVE = "task_not_live"          # unknown, terminal, stopped, or not in this process
    USER_MISMATCH = "user_mismatch"          # the named user does not own the task
    EXECUTABLE_NOT_ALLOWED = "executable_not_allowed"
    INVALID_LIMITS = "invalid_limits"
    ALREADY_ACTIVE = "already_active"        # one live record per task
    NOT_FOUND = "not_found"


class BreakGlassRefused(Exception):
    def __init__(self, code: RefusalCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class BreakGlassRecord:
    record_id: str
    task_id: uuid.UUID
    user_id: uuid.UUID
    executables: tuple[str, ...]
    max_invocations: int
    remaining: int
    reason: str
    activated_at: datetime
    expires_at: datetime
    activated_by: str  # the superuser credential's fingerprint, never the credential
    ended: EndReason | None = None

    @property
    def live(self) -> bool:
        return self.ended is None


class EventKind(str, Enum):
    INVOKED = "invoked"
    ENDED = "ended"


@dataclass(frozen=True)
class BreakGlassEvent:
    """One audit row still to be written. `resource` is identifiers and
    numbers only (the audit column is a reference, never a payload)."""

    kind: EventKind
    record_id: str
    task_id: uuid.UUID
    user_id: uuid.UUID
    resource: str


def _require_superuser(principal: SuperuserPrincipal) -> None:
    if not isinstance(principal, SuperuserPrincipal) or not principal.grant.is_valid():
        raise BreakGlassRefused(RefusalCode.NOT_SUPERUSER, "superuser authority required")


def activation_resource(record: BreakGlassRecord) -> str:
    window = int((record.expires_at - record.activated_at).total_seconds())
    return (f"bg:{record.record_id}:{record.task_id}:{record.reason}:"
            f"n{record.max_invocations}:t{window}:{','.join(record.executables)}")


class BreakGlassRegistry:
    """Implements `server.execution.break_glass.BreakGlassLookup` for the
    executor; everything else is for the superuser control path and the
    runtime's security adapter."""

    def __init__(self, config: BreakGlassConfig, *, clock: Callable[[], datetime] = _utcnow) -> None:
        self._config = config
        self._allowed = frozenset(config.allowed_executables)
        self._clock = clock
        self._live: dict[uuid.UUID, BreakGlassRecord] = {}  # task_id → its one live record
        self._events: dict[uuid.UUID, list[BreakGlassEvent]] = {}
        if config.enabled:
            logger.warning(
                "break-glass is ENABLED: a superuser can let a task run %s without kernel "
                "confinement — such a child can read anything the server's OS user can and reach "
                "the network directly (docs/20_CONFINEMENT_BREAK_GLASS.md)",
                sorted(self._allowed) or "nothing (no break_glass.allowed_executables)",
            )

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def allowed_executables(self) -> tuple[str, ...]:
        return tuple(self._config.allowed_executables) if self._config.enabled else ()

    # ── superuser: create / revoke ──────────────────────────────────────

    def prepare(
        self,
        principal: SuperuserPrincipal,
        *,
        task_id: uuid.UUID,
        task_owner: uuid.UUID,
        user_id: uuid.UUID,
        executables: Iterable[str],
        max_invocations: int,
        window_seconds: int | None,
        task_seconds_left: float,
        reason: str,
    ) -> BreakGlassRecord:
        """Validate an activation and build its record — not yet live.
        `install` makes it live; the control path audits in between, so no
        record is ever live before its activation is written.

        `task_owner` is the live task's owner as the runtime knows it;
        `user_id` is who the superuser says it is — they must agree, so an
        activation can never be pointed at one user's task on another's
        behalf. The window is capped by `max_window_minutes` and by the time
        the task itself has left."""

        _require_superuser(principal)
        if not self._config.enabled:
            raise BreakGlassRefused(RefusalCode.DISABLED, "break-glass is not enabled on this server")
        if user_id != task_owner:
            raise BreakGlassRefused(RefusalCode.USER_MISMATCH, "the task is not owned by that user")
        named = tuple(dict.fromkeys(executables))
        if not named or not set(named) <= self._allowed:
            raise BreakGlassRefused(
                RefusalCode.EXECUTABLE_NOT_ALLOWED,
                "every executable must be on execution.process.break_glass.allowed_executables",
            )
        if not 1 <= max_invocations <= self._config.max_invocations:
            raise BreakGlassRefused(
                RefusalCode.INVALID_LIMITS,
                f"max_invocations must be between 1 and {self._config.max_invocations}",
            )
        cap = self._config.max_window_minutes * 60
        requested = cap if window_seconds is None else window_seconds
        if not 1 <= requested <= cap:
            raise BreakGlassRefused(RefusalCode.INVALID_LIMITS, f"the window must be between 1 and {cap} seconds")
        window = min(requested, int(task_seconds_left))
        if window < 1:
            raise BreakGlassRefused(RefusalCode.TASK_NOT_LIVE, "the task has no time left")
        self._refuse_if_active(task_id)

        now = self._clock()
        return BreakGlassRecord(
            record_id=secrets.token_hex(4),
            task_id=task_id,
            user_id=user_id,
            executables=named,
            max_invocations=max_invocations,
            remaining=max_invocations,
            reason=reason,
            activated_at=now,
            expires_at=now + timedelta(seconds=window),
            activated_by=principal.token_fingerprint,
        )

    def install(self, principal: SuperuserPrincipal, record: BreakGlassRecord) -> None:
        """Make a prepared record live. Synchronous: the caller re-checks the
        task in the same step, with nothing able to change in between."""

        _require_superuser(principal)
        if not self._config.enabled:
            raise BreakGlassRefused(RefusalCode.DISABLED, "break-glass is not enabled on this server")
        self._refuse_if_active(record.task_id)
        if self._clock() >= record.expires_at:
            raise BreakGlassRefused(RefusalCode.INVALID_LIMITS, "the window has already passed")
        self._live[record.task_id] = record

    def _refuse_if_active(self, task_id: uuid.UUID) -> None:
        if self._current(task_id) is not None or task_id in self._live:
            raise BreakGlassRefused(RefusalCode.ALREADY_ACTIVE, "this task already has a live break-glass record")

    def revoke(self, principal: SuperuserPrincipal, *, task_id: uuid.UUID) -> BreakGlassRecord:
        _require_superuser(principal)
        record = self._current(task_id)
        if record is None:
            raise BreakGlassRefused(RefusalCode.NOT_FOUND, "no live break-glass record for that task")
        self._end(record, EndReason.REVOKED)
        return record

    # ── the executor (BreakGlassLookup) ─────────────────────────────────

    def claim(self, *, task_id: uuid.UUID, user_id: uuid.UUID, executable: str) -> BreakGlassClaim | None:
        record = self._current(task_id)
        if record is None or record.user_id != user_id or executable not in record.executables:
            return None
        record.remaining -= 1
        return BreakGlassClaim(record_id=record.record_id, task_id=task_id, user_id=user_id,
                               executable=executable, remaining=record.remaining)

    def record_invocation(
        self, claim: BreakGlassClaim, *, argv_sha256: str, outcome: InvocationOutcome,
        exit_code: int | None, duration_ms: int,
    ) -> None:
        exit_text = "-" if exit_code is None else str(exit_code)
        # Recorded even when the record ended while the child ran (a revoke, a
        # stop): the invocation still happened.
        self._queue(BreakGlassEvent(
            kind=EventKind.INVOKED, record_id=claim.record_id, task_id=claim.task_id,
            user_id=claim.user_id,
            resource=(f"bg:{claim.record_id}:{claim.task_id}:{outcome.value}:{exit_text}:"
                      f"{duration_ms}ms:{argv_sha256}:{claim.executable}"),
        ))
        record = self._live.get(claim.task_id)
        if claim.remaining == 0 and record is not None and record.record_id == claim.record_id:
            self._end(record, EndReason.EXHAUSTED)

    # ── task lifecycle / observation ────────────────────────────────────

    def end_task(self, task_id: uuid.UUID, reason: EndReason) -> None:
        self._current(task_id)  # expiry first: an expired record ended by expiring
        record = self._live.get(task_id)  # including one whose last invocation is spent
        if record is not None:
            self._end(record, reason)

    def active_for(self, task_id: uuid.UUID) -> bool:
        return self._current(task_id) is not None

    def active(self) -> list[BreakGlassRecord]:
        return [r for r in (self._current(t) for t in list(self._live)) if r is not None]

    def drain(self, task_id: uuid.UUID) -> list[BreakGlassEvent]:
        """The audit rows still owed for this task — each returned once."""

        self._current(task_id)  # an expired record ends (and queues its end) here
        return self._events.pop(task_id, [])

    def drain_except(self, live_task_ids: Iterable[uuid.UUID]) -> list[BreakGlassEvent]:
        """Rows owed for tasks no request is driving any more (e.g. one pruned
        while paused): the control path writes these."""

        for task_id in list(self._live):
            self._current(task_id)
        keep = set(live_task_ids)
        drained: list[BreakGlassEvent] = []
        for task_id in [t for t in self._events if t not in keep]:
            drained.extend(self._events.pop(task_id))
        return drained

    # ── internals ───────────────────────────────────────────────────────

    def _current(self, task_id: uuid.UUID) -> BreakGlassRecord | None:
        record = self._live.get(task_id)
        if record is None:
            return None
        if record.remaining <= 0:
            return None  # spent; its end is queued once the run is recorded
        if self._clock() >= record.expires_at:
            self._end(record, EndReason.EXPIRED)
            return None
        return record

    def _end(self, record: BreakGlassRecord, reason: EndReason) -> None:
        if not record.live:
            return
        record.ended = reason
        if self._live.get(record.task_id) is record:
            del self._live[record.task_id]
        self._queue(BreakGlassEvent(
            kind=EventKind.ENDED, record_id=record.record_id, task_id=record.task_id,
            user_id=record.user_id, resource=f"bg:{record.record_id}:{record.task_id}:{reason.value}",
        ))

    def _queue(self, event: BreakGlassEvent) -> None:
        self._events.setdefault(event.task_id, []).append(event)


__all__ = [
    "BreakGlassEvent",
    "BreakGlassRecord",
    "BreakGlassRefused",
    "BreakGlassRegistry",
    "EndReason",
    "EventKind",
    "RefusalCode",
    "activation_resource",
]
