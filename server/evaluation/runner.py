"""The Judge runs beside tasks, never inside them (19 §6: "It runs
asynchronously; the task does not wait for it").

`EvaluationQueue` is a bounded queue drained by one background worker. The
runtime's observer only *enqueues* — a synchronous, non-blocking call — so an
evaluation can never delay, fail or alter the task it is about. A full queue
drops the job (the drop is counted and logged, never retried into the task's
path). The worker starts on first use, so it runs wherever the app's event
loop runs; it is also a lifespan service so shutdown cancels it cleanly.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger("hypermind.evaluation.runner")

Job = Callable[[], Awaitable[object]]


class EvaluationQueue:
    def __init__(self, *, maxsize: int) -> None:
        self._queue: asyncio.Queue[tuple[str, Job]] = asyncio.Queue(maxsize=maxsize)
        self._worker: asyncio.Task | None = None
        self.dropped = 0
        self.failed = 0
        self.completed = 0

    def submit(self, name: str, job: Job) -> bool:
        try:
            self._queue.put_nowait((name, job))
        except asyncio.QueueFull:
            self.dropped += 1
            logger.warning("evaluation queue full; dropped %s", name)
            return False
        self._ensure_worker()
        return True

    def pending(self) -> int:
        return self._queue.qsize()

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._loop(), name="evaluation-runner")

    async def _loop(self) -> None:
        while True:
            name, job = await self._queue.get()
            try:
                await job()
                self.completed += 1
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the runner outlives any one evaluation
                self.failed += 1
                logger.exception("evaluation job %s failed", name)
            finally:
                self._queue.task_done()

    async def drain(self) -> None:
        """Wait until every queued evaluation has finished (tests, shutdown)."""

        if self._queue.qsize() or self._queue._unfinished_tasks:  # type: ignore[attr-defined]
            self._ensure_worker()
        await self._queue.join()

    async def start(self) -> None:
        return None  # started lazily by `submit`

    async def stop(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass


__all__ = ["EvaluationQueue"]
