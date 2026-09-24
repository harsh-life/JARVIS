"""The global emergency latch's read side (18 §5.4).

`InProcessLatch` is this process's copy of the latch; `SupervisorGate` is the
runtime's read-only `SupervisorGatePort` over it and the `supervisor_latch` row.
Setting and clearing live only in `server/composition/supervisor.py`, behind a
`SuperuserPrincipal`. A submission is refused if either copy says latched, or
if the row cannot be read (fail closed).
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from server.storage.models import SupervisorLatch

logger = logging.getLogger("hypermind.supervisor")

LATCH_ID = 1


class InProcessLatch:
    """This process's copy of the latch. Set before the row is written; cleared
    only after the row is. See the module docstring."""

    def __init__(self) -> None:
        self.latched = False


class SupervisorGate:
    """The runtime's read-only `SupervisorGatePort` for one request."""

    def __init__(self, latch: InProcessLatch, session: AsyncSession) -> None:
        self._latch = latch
        self._session = session

    def latched_now(self) -> bool:
        return self._latch.latched

    async def submissions_open(self) -> bool:
        if self._latch.latched:
            return False
        try:
            row = await self._session.get(SupervisorLatch, LATCH_ID)
        except Exception:  # noqa: BLE001 — unreadable latch ⇒ latched (fail closed)
            logger.exception("the supervisor latch could not be read; refusing new tasks")
            return False
        return not (row is not None and row.latched)
