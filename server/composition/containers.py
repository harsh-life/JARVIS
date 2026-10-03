"""Container reconciliation (docs/29 §25.2, Phase 6 slice 6C).

`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]` (docs/29). Every external run's
container is labelled with its run (`server/execution/containers.py`). At
startup, and every `reconcile_interval_seconds` (10 minutes by default), the
reconciler asks the engine for every managed container and removes each one
that is not a live run's — a run whose record is missing, finished, or never
existed (a container a crash or a restart left behind, or one the engine
reports with a forged label). Each removal is audited
(`agent.orphan.deprovisioned`).

It verifies the engine first and refuses to start if the engine is not a
rootless Podman with `runsc` run by a non-root user: an operator who switched
containers on gets the boundary OD-AF-11 names, or a startup failure — never
a weaker one silently.

External runtimes never receive recovery authority: recovery is this
loop's, and it only ever removes.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

from server.execution.containers import ContainerEngine
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.storage.models import AgentRunRow
from shared.schemas.enums import AuditActor, AuditResult

if TYPE_CHECKING:
    from server.storage import StorageBackend

logger = logging.getLogger("hypermind.agents.containers")


class ContainerReconciler:
    def __init__(self, *, engine: ContainerEngine, storage: "StorageBackend", interval_seconds: float) -> None:
        self.engine = engine
        self._storage = storage
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None

    async def live_runs(self) -> set[uuid.UUID]:
        async with self._storage.session() as session:
            rows = (await session.execute(
                select(AgentRunRow.run_id).where(AgentRunRow.finished_at.is_(None)))).scalars().all()
        return set(rows)

    async def reconcile(self) -> list[str]:
        removed = await self.engine.reconcile(await self.live_runs())
        if removed:
            async with self._storage.session() as session:
                audit = AuditLogger(session, request_id=uuid.uuid4())
                for name in removed:
                    await audit.record(actor=AuditActor.SYSTEM, action=AuditAction.AGENT_ORPHAN_DEPROVISIONED,
                                       resource=f"container:{name}"[:128], result=AuditResult.SUCCESS)
                await session.commit()
        return removed

    # ── the lifespan service ────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is None:
            await self.engine.verify()
            await self.reconcile()
            self._task = asyncio.create_task(self._loop(), name="agent-container-reconciler")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the loop outlives any one failure
                logger.warning("container reconciliation failed; retrying next interval", exc_info=False)


__all__ = ["ContainerReconciler"]
