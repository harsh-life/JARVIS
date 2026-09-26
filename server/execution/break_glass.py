"""What the process executor asks of break-glass — 20 §2.

The executor never decides on its own to run a child without confinement, and
nothing it is handed by a caller can make it: there is no argument, proposal
field, or environment variable for that. It asks one question of a
server-side record, through this Protocol, immediately before it spawns:

    "is there a live break-glass record for *this task*, owned by *this user*,
     naming *this executable*, with an invocation left?"

A yes spends one invocation (the claim) and removes exactly the kernel layer —
Landlock and seccomp — from that one child. Every other limit still applies
(20 §2.1). A no, or no lookup at all, means the ordinary confined path.

Every claim is followed by exactly one `record_invocation` — whether the child
exited, timed out, was cancelled, or never started — so the audit trail's
`break_glass.invoked` row (20 §2.4) does not depend on how the call ended.

The record store (`server/composition/break_glass.py`) is created by the
composition root and activated only through the superuser control route. This
module offers no way to create, widen, or extend a record — only to claim one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class InvocationOutcome(str, Enum):
    EXITED = "exited"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    NOT_STARTED = "not_started"


@dataclass(frozen=True)
class BreakGlassClaim:
    """One spent invocation of a live record."""

    record_id: str
    task_id: uuid.UUID
    user_id: uuid.UUID
    executable: str
    # Invocations the record still allows after this one (0 → it has ended).
    remaining: int


class BreakGlassLookup(Protocol):
    def claim(
        self, *, task_id: uuid.UUID, user_id: uuid.UUID, executable: str
    ) -> BreakGlassClaim | None:
        """Spend one invocation of the live record matching all three, or
        return `None` (no record, expired, exhausted, wrong user, or an
        executable the record does not name). Synchronous: nothing may change
        between the answer and the spawn it gates."""
        ...

    def record_invocation(
        self,
        claim: BreakGlassClaim,
        *,
        argv_sha256: str,
        outcome: InvocationOutcome,
        exit_code: int | None,
        duration_ms: int,
    ) -> None:
        """What the claimed run did — identifiers and numbers only, never the
        arguments or output. Must not raise."""
        ...


__all__ = ["BreakGlassClaim", "BreakGlassLookup", "InvocationOutcome"]
