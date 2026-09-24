"""Task modes and the supervisor's mode ceiling — 18 §3 (OD-F1).

The ceiling is the highest risk tier any operation may reach in a task of that
mode. It is enforced by the supervisor **before** the authorization engine is
asked, and an operation above it is refused outright — never offered for
confirmation, because a draft, a suggestion or an observation must not execute
even if the user would have approved the action (18 §3: "refused as an
observation, never confirmed").

It is not a second authorization system and not a new tier: the tier it reads
is the engine's own (`SecurityPort.operation_tier`), and in `execute` mode it
adds nothing at all. Everything the engine enforces — the floor, D1–D5,
confirmation, step-up — still applies to whatever the ceiling lets through.

Capability *activation* is bookkeeping, not execution, so the ceiling does not
block it — except that a capability with no operation at or under the ceiling
is refused, rather than put to the user as a confirmation that could never
lead to anything the task may do.
"""

from __future__ import annotations

from typing import Mapping

from shared.schemas.agent import TaskMode
from shared.schemas.enums import RiskCategory

_SEVERITY: dict[RiskCategory, int] = {
    RiskCategory.LOW_READ: 0,
    RiskCategory.LOW_WRITE: 1,
    RiskCategory.CONSEQUENTIAL: 2,
    RiskCategory.HIGH_IRREVERSIBLE: 3,
}

# `None` = no ceiling beyond the tier table itself.
MODE_CEILING: dict[TaskMode, RiskCategory | None] = {
    TaskMode.EXECUTE: None,
    TaskMode.DRAFT: RiskCategory.LOW_READ,
    TaskMode.SUGGEST: RiskCategory.LOW_READ,
    TaskMode.OBSERVE: RiskCategory.LOW_READ,
}


def within_ceiling(mode: TaskMode, tier: RiskCategory | None) -> bool:
    """Whether an operation of `tier` may run in a task of `mode`. An unknown
    tier is above every ceiling (fail closed)."""

    ceiling = MODE_CEILING[mode]
    if ceiling is None:
        return True
    return tier is not None and _SEVERITY[tier] <= _SEVERITY[ceiling]


def capability_usable(mode: TaskMode, operation_tiers: Mapping[str, RiskCategory]) -> bool:
    """Whether any operation of a capability fits under the mode's ceiling."""

    return any(within_ceiling(mode, tier) for tier in operation_tiers.values())


def not_activated(mode: TaskMode) -> str:
    return (
        f"not activated: none of its operations can run in a {mode.value} task "
        "(only low_read operations do)."
    )


def refusal(mode: TaskMode, what: str) -> str:
    return (
        f"{what} is not permitted in {mode.value} mode: nothing is executed in a {mode.value} "
        "task. Starting a new task in execute mode is up to the user."
    )
