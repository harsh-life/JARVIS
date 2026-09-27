"""What the operator console reads from live, in-process state (28 §3).

`server.dashboard` may not import the runtime, the break-glass registry, the
device hub, memory, the vault, the Judge or the composition root (pyproject:
"The dashboard is read-only"). It declares here — as **read-only** Protocols
with plain-data results — the few live facts its views need, and the
composition root satisfies them (`server/composition/console.py`).

Every method returns a snapshot of plain data. None returns an object with a
method that acts: no task handle, no registry, no hub, no provider. Counts
only where content would be user data (memory facts), and never a secret
value (a secret is described by its handle and whether it resolves).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class LiveTask:
    task_id: uuid.UUID
    user_id: uuid.UUID
    state: str  # "running" | "awaiting_confirmation" | "waiting_for_platform"
    mode: str
    iterations: int
    model_calls: int
    tool_calls: int
    worker_switches: int
    tripped_source: str | None
    break_glass_active: bool


@dataclass(frozen=True)
class BreakGlassRecordSummary:
    record_id: str
    task_id: uuid.UUID
    user_id: uuid.UUID
    executables: tuple[str, ...]
    max_invocations: int
    remaining: int
    reason: str
    activated_at: datetime
    expires_at: datetime


class LiveStateView(Protocol):
    def live_tasks(self) -> list[LiveTask]: ...

    def latched_in_process(self) -> bool: ...

    def break_glass_enabled(self) -> bool: ...

    def break_glass_records(self) -> list[BreakGlassRecordSummary]: ...


class ComponentProbes(Protocol):
    async def components(self, *, probe_models: bool) -> dict[str, dict]:
        """Per-component status: `database`, `secret_store`, `model_providers`,
        `mem0`, `vault`, `scheduler`, `device_channel`, `judge`."""
        ...

    async def connected_device_ids(self) -> set[uuid.UUID]: ...

    async def memory_fact_counts(self, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, int] | None:
        """Facts per owner — counts only, never content. `None`: memory off."""
        ...

    async def vault_status(self) -> dict | None: ...

    def judge_state(self) -> dict: ...


class SecretResolvability(Protocol):
    async def resolves(self, handle: str) -> bool | None:
        """Whether a configured secret reference can be resolved — from
        metadata only (a SecretStore reference row, an environment variable's
        presence), never by resolving it. `None` when that cannot be told."""
        ...


__all__ = [
    "BreakGlassRecordSummary",
    "ComponentProbes",
    "LiveStateView",
    "LiveTask",
    "SecretResolvability",
]
