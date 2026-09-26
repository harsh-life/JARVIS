"""The device channel's server half (docs/23 §4) — the real `DeviceTransport`.

What it guarantees, each a direct docs/23 requirement:

* **Exact device.** A connection is bound to exactly one `(user_id,
  device_id)` when it authenticates (the gateway does that, then calls
  `attach`). `send` delivers to the connection of `operation.device_id` and
  nothing else, and only if that connection is bound to the operation's own
  user (OD-DEV-1, ANDC-T1). A second connection for the same device
  supersedes the first rather than sharing it.
* **No queue.** A device that is not connected fails the operation at once
  with `device_unavailable` (ANDC-T2). Nothing is held for a reconnect —
  authority may not go stale in a queue. An operation already past its
  `expires_at` is never sent.
* **Bounded, untrusted results.** A result must arrive before the operation
  expires, on the connection it was sent to, within the primitive's size
  bound. A result for an unknown, finished or cancelled operation is dropped
  (ANDC-T8). An accepted result is returned as data for the worker, never
  interpreted (PRD §24).
* **Cancellation.** A cancelled `send` (the runtime's `_run_cancellable`,
  a user cancel, an operator stop) tells the device `cancel: op_id`; ending a
  task tells it `cancel: task_id`. Either way the pending result is
  discarded.

This module decides nothing about authority. It sits below `graph`/
`capabilities` and cannot import them; it receives an operation that has
already been authorized, and only ever narrows what happens next.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol

from pydantic import ValidationError

from server.execution.android import DeviceOperation
from server.execution.device_observations import (
    OBSERVATION_PREAMBLE,
    parse_observation,
    render_observation,
)
from shared.schemas.device_channel import (
    MAX_SCREENSHOT_FRAME_BYTES,
    DeviceCancel,
    DeviceCloseCode,
    DevicePlatformDependency,
    DeviceRefusalReason,
    DeviceResultEnvelope,
    DeviceResultStatus,
    ResultKind,
)
from shared.schemas.execution import ExecutionError, ExecutionErrorCode, ExecutionResult

logger = logging.getLogger("hypermind.execution.device_hub")

_REFUSAL_CODES = {
    DeviceRefusalReason.OPERATION_EXPIRED: ExecutionErrorCode.OPERATION_EXPIRED,
    DeviceRefusalReason.PLATFORM_UNAVAILABLE: ExecutionErrorCode.PLATFORM_UNAVAILABLE,
    DeviceRefusalReason.CANCELLED: ExecutionErrorCode.CANCELLED,
}


class DeviceConnection(Protocol):
    """What the gateway's WebSocket handler gives the hub for one socket."""

    async def send_text(self, text: str) -> None: ...

    async def close(self, code: int, reason: str) -> None: ...


@dataclass(eq=False)
class DeviceSession:
    """One authenticated socket, bound to exactly one user and one device."""

    user_id: uuid.UUID
    device_id: uuid.UUID
    connection: DeviceConnection
    connected_at: datetime
    platforms: dict[DevicePlatformDependency, bool] = field(default_factory=dict)
    closed: bool = False


@dataclass(eq=False)
class _Pending:
    operation: DeviceOperation
    session: DeviceSession
    future: asyncio.Future


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DeviceHub:
    def __init__(self, *, clock: Callable[[], datetime] = _utcnow) -> None:
        self._clock = clock
        self._sessions: dict[uuid.UUID, DeviceSession] = {}
        self._pending: dict[uuid.UUID, _Pending] = {}
        # Fire-and-forget sends (cancel frames) keep a reference until done.
        self._background: set[asyncio.Task] = set()

    # ── connection lifecycle (called by the gateway) ────────────────────

    async def attach(
        self, *, user_id: uuid.UUID, device_id: uuid.UUID, connection: DeviceConnection
    ) -> DeviceSession:
        session = DeviceSession(
            user_id=user_id, device_id=device_id, connection=connection, connected_at=self._clock()
        )
        previous = self._sessions.get(device_id)
        self._sessions[device_id] = session
        if previous is not None:
            await self._end(previous, DeviceCloseCode.SUPERSEDED, "superseded by a newer connection")
        return session

    async def detach(self, session: DeviceSession) -> None:
        """The socket is gone. Its pending operations fail now — never linger
        waiting for a reconnect that would inherit their authority."""

        if self._sessions.get(session.device_id) is session:
            del self._sessions[session.device_id]
        session.closed = True
        self._fail_pending(session, ExecutionErrorCode.DEVICE_UNAVAILABLE, "device disconnected")

    async def disconnect(self, device_id: uuid.UUID, code: DeviceCloseCode, reason: str) -> bool:
        """Close a device's socket from the server side (revocation, logout,
        rotation). Returns whether one was connected."""

        session = self._sessions.get(device_id)
        if session is None:
            return False
        await self._end(session, code, reason)
        return True

    async def _end(self, session: DeviceSession, code: DeviceCloseCode, reason: str) -> None:
        await self.detach(session)
        try:
            await session.connection.close(int(code), reason)
        except Exception:  # noqa: BLE001 — the socket may already be gone
            logger.debug("close on an already-closed device socket", exc_info=True)

    def record_platform_status(
        self, session: DeviceSession, platforms: dict[DevicePlatformDependency, bool]
    ) -> None:
        """Informational: which on-demand dependencies the device reports
        available. Never makes anything authorized (docs/23 §5.3)."""

        session.platforms.update(platforms)

    def platform_status(self, device_id: uuid.UUID) -> dict[DevicePlatformDependency, bool]:
        session = self._sessions.get(device_id)
        return dict(session.platforms) if session is not None else {}

    # ── DeviceTransport ─────────────────────────────────────────────────

    async def is_connected(self, *, device_id: uuid.UUID) -> bool:
        return device_id in self._sessions

    async def send(self, operation: DeviceOperation) -> ExecutionResult:
        now = self._clock()
        if now >= operation.expires_at:
            raise ExecutionError(ExecutionErrorCode.OPERATION_EXPIRED, "operation expired before dispatch")
        session = self._sessions.get(operation.device_id)
        if session is None or session.user_id != operation.user_id:
            # A socket bound to another user can never receive this
            # principal's operation, even if it claims the same device id.
            raise ExecutionError(ExecutionErrorCode.DEVICE_UNAVAILABLE, "device is not connected")

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        pending = _Pending(operation=operation, session=session, future=future)
        self._pending[operation.op_id] = pending
        try:
            try:
                await session.connection.send_text(operation.envelope().model_dump_json())
            except Exception as exc:  # noqa: BLE001 — a dead socket is unavailability
                raise ExecutionError(ExecutionErrorCode.DEVICE_UNAVAILABLE, "device send failed") from exc
            remaining = (operation.expires_at - self._clock()).total_seconds()
            try:
                envelope: DeviceResultEnvelope = await asyncio.wait_for(future, timeout=max(remaining, 0))
            except asyncio.TimeoutError:
                self._cancel_on_device(session, DeviceCancel(op_id=operation.op_id))
                raise ExecutionError(ExecutionErrorCode.TIMEOUT, "device did not answer in time") from None
        except asyncio.CancelledError:
            self._cancel_on_device(session, DeviceCancel(op_id=operation.op_id))
            raise
        finally:
            self._pending.pop(operation.op_id, None)
        return _interpret(operation, envelope)

    def cancel_task(self, task_id: uuid.UUID) -> None:
        """The task ended (completed, failed, cancelled, stopped): tell every
        device with work in flight for it, and drop that work."""

        affected: dict[uuid.UUID, DeviceSession] = {}
        for op_id, pending in list(self._pending.items()):
            if pending.operation.task_id == task_id:
                affected[pending.session.device_id] = pending.session
                self._pending.pop(op_id, None)
                if not pending.future.done():
                    pending.future.set_exception(
                        ExecutionError(ExecutionErrorCode.CANCELLED, "task ended")
                    )
        for session in affected.values():
            self._cancel_on_device(session, DeviceCancel(task_id=task_id))

    # ── inbound results (called by the gateway) ─────────────────────────

    def deliver(self, session: DeviceSession, raw: str) -> bool:
        """Route one `result` frame. Returns whether it resolved a pending
        operation; everything else is dropped (and logged without content)."""

        if len(raw.encode("utf-8")) > MAX_SCREENSHOT_FRAME_BYTES:
            logger.warning("device %s sent an oversized frame; dropped", session.device_id)
            return False
        try:
            envelope = DeviceResultEnvelope.model_validate_json(raw)
        except ValidationError:
            logger.warning("device %s sent a malformed result; dropped", session.device_id)
            return False
        pending = self._pending.get(envelope.op_id)
        if pending is None or pending.future.done():
            # Late (after timeout/cancel), duplicated, or never sent.
            logger.info("device %s: result for an operation not in flight; dropped", session.device_id)
            return False
        if pending.session is not session:
            # A device answers only the operations sent on its own connection.
            logger.warning("device %s answered another connection's operation; dropped", session.device_id)
            return False
        if len(raw.encode("utf-8")) > pending.operation.max_result_bytes:
            pending.future.set_exception(
                ExecutionError(ExecutionErrorCode.RESPONSE_TOO_LARGE, "device result exceeds its bound")
            )
            return True
        pending.future.set_result(envelope)
        return True

    # ── internals ───────────────────────────────────────────────────────

    def _fail_pending(self, session: DeviceSession, code: ExecutionErrorCode, message: str) -> None:
        for op_id, pending in list(self._pending.items()):
            if pending.session is session:
                self._pending.pop(op_id, None)
                if not pending.future.done():
                    pending.future.set_exception(ExecutionError(code, message))

    def _cancel_on_device(self, session: DeviceSession, cancel: DeviceCancel) -> None:
        if session.closed:
            return

        async def _send() -> None:
            try:
                await session.connection.send_text(cancel.model_dump_json(exclude_none=True))
            except Exception:  # noqa: BLE001 — best effort; the result is dropped regardless
                logger.debug("cancel frame not delivered", exc_info=True)

        task = asyncio.ensure_future(_send())
        self._background.add(task)
        task.add_done_callback(self._background.discard)


def _interpret(operation: DeviceOperation, envelope: DeviceResultEnvelope) -> ExecutionResult:
    if envelope.status is DeviceResultStatus.OK:
        # Untrusted: validated against the primitive's declared result kind,
        # then rendered as quoted data (device_observations).
        observation = parse_observation(operation, envelope.result, envelope.perception_level)
        if observation.kind is ResultKind.SCREENSHOT:
            # docs/23 §6 level 4 needs a server-side vision rung; without one
            # the image is dropped here, unread and unstored.
            raise ExecutionError(
                ExecutionErrorCode.PLATFORM_UNSUPPORTED, "no vision model is configured; screenshot discarded"
            )
        return ExecutionResult(
            content=render_observation(observation),
            metadata={
                "primitive": operation.primitive,
                "result_kind": observation.kind.value,
                "perception_level": observation.perception_level.value if observation.perception_level else None,
            },
        )
    if envelope.status is DeviceResultStatus.REFUSED:
        reason = envelope.refusal_reason
        detail = reason.value if reason else "refused"
        if envelope.required_platform is not None:
            detail = f"{detail}: {envelope.required_platform.value}"
        raise ExecutionError(_REFUSAL_CODES.get(reason, ExecutionErrorCode.DEVICE_REFUSED), detail)
    reason = envelope.failure_reason.value if envelope.failure_reason else "failed"
    raise ExecutionError(ExecutionErrorCode.DEVICE_ACTION_FAILED, reason)


__all__ = ["OBSERVATION_PREAMBLE", "DeviceConnection", "DeviceHub", "DeviceSession"]
