"""The device channel endpoint — `WS /api/v1/devices/channel` (docs/23 §4).

The socket carries operations to a phone and results back. It is where the
server binds a live connection to exactly one `(user_id, device_id)`, and it
takes that binding only from credentials the server verifies itself
(PHONE-003):

1. The first frame is `hello` — an access token **and** a fresh device proof.
   Both are verified exactly as the HTTP API verifies them, and the proof's
   device must be the token's device. Credentials never travel in the URL.
2. The client's mapping version must equal the server's (docs/23 §5.1); a
   mismatch closes the socket — the client must update, never "try anyway".
3. The binding lasts until the access token expires. `reauth` extends it with
   a fresh token for the *same* device and user; nothing can re-bind a socket
   to anyone else. The principal is re-checked every minute, so a revoked
   device, a suspended user or a logged-out session loses its socket even if
   no HTTP call happens to notice.

Every rejection is one close code, audited, with nothing about why a
credential failed revealed to the client beyond "authenticate again".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from server.auth.errors import AccessTokenExpired, AuthError
from server.auth.sessions import ResolvedSession
from server.config.schema import AndroidConfig
from server.execution.android import MAPPING_VERSION
from server.execution.device_hub import DeviceHub, DeviceSession
from server.gateway.security import SecurityCore
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from shared.schemas.device_channel import (
    MAX_CONTROL_FRAME_BYTES,
    MAX_SCREENSHOT_FRAME_BYTES,
    DeviceCloseCode,
    DeviceHello,
    DeviceHelloAck,
    DevicePlatformStatus,
    DeviceReauth,
    DeviceReauthAck,
)
from shared.schemas.enums import AuditActor, AuditResult

logger = logging.getLogger("hypermind.gateway.device_channel")

router = APIRouter(tags=["devices"])

_DEVICE_GONE = frozenset({"device_revoked", "user_not_active"})

HELLO_TIMEOUT_SECONDS = 10.0
REVALIDATE_SECONDS = 60.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class _SocketConnection:
    """Adapts a Starlette WebSocket to the hub's `DeviceConnection`. Sends are
    serialized: two operations dispatched at once never interleave frames."""

    def __init__(self, websocket: WebSocket) -> None:
        self._ws = websocket
        self._lock = asyncio.Lock()
        self._closed = False

    async def send_text(self, text: str) -> None:
        async with self._lock:
            if self._closed:
                raise ConnectionError("connection closed")
            await self._ws.send_text(text)

    async def close(self, code: int, reason: str) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            await self._ws.close(code=code, reason=reason)


async def _audit(websocket: WebSocket, action: AuditAction, result: AuditResult, **ids) -> None:
    storage = websocket.app.state.storage
    async with storage.session() as db:
        audit = AuditLogger(db, request_id=uuid.uuid4())
        await audit.record(
            actor=AuditActor.USER if ids.get("user_id") else AuditActor.SYSTEM,
            action=action,
            resource=f"device:{ids['device_id']}" if ids.get("device_id") else "device:*",
            result=result,
            **ids,
        )
        await db.commit()


async def _authenticate(websocket: WebSocket, hello: DeviceHello) -> ResolvedSession | DeviceCloseCode:
    core: SecurityCore = websocket.app.state.security
    storage = websocket.app.state.storage
    async with storage.session() as db:
        audit = AuditLogger(db, request_id=uuid.uuid4())
        try:
            resolved = await core.sessions.resolve_principal(db, hello.access_token)
            device = await core.devices.verify_proof(db, hello.device_proof, audit=audit)
        except AccessTokenExpired:
            await db.commit()
            return DeviceCloseCode.AUTH_EXPIRED
        except AuthError:
            await db.commit()
            return DeviceCloseCode.AUTH_FAILED
        if device.device_id != resolved.principal.device_id or device.user_id != resolved.principal.user_id:
            # A proof for one device riding another device's token: refused,
            # never "bound to whichever one looks right".
            await audit.record(
                actor=AuditActor.SYSTEM,
                action=AuditAction.DEVICE_CHANNEL_REJECTED,
                resource="device:*",
                result=AuditResult.BLOCKED,
            )
            await db.commit()
            return DeviceCloseCode.AUTH_FAILED
        await db.commit()
    return resolved


async def _revalidate(websocket: WebSocket, token: str, session: DeviceSession) -> ResolvedSession | DeviceCloseCode:
    core: SecurityCore = websocket.app.state.security
    async with websocket.app.state.storage.session() as db:
        try:
            resolved = await core.sessions.resolve_principal(db, token)
        except AccessTokenExpired:
            return DeviceCloseCode.AUTH_EXPIRED
        except AuthError as exc:
            # `revoked` tells the phone to wipe its enrollment, so it is sent
            # only when the device itself (or its user) is gone. A logged-out or
            # rotated-away token is `auth_expired`: the phone re-authenticates
            # with its device proof, which fails on its own if that is revoked.
            if exc.reason in _DEVICE_GONE:
                return DeviceCloseCode.REVOKED
            return DeviceCloseCode.AUTH_EXPIRED
    principal = resolved.principal
    if principal.device_id != session.device_id or principal.user_id != session.user_id:
        return DeviceCloseCode.AUTH_FAILED
    return resolved


@router.websocket("/devices/channel")
async def device_channel(websocket: WebSocket) -> None:
    await websocket.accept()
    hub: DeviceHub | None = getattr(websocket.app.state, "device_hub", None)
    android: AndroidConfig = getattr(websocket.app.state, "android", None) or AndroidConfig()
    if hub is None or not android.enabled:
        await websocket.close(code=int(DeviceCloseCode.CHANNEL_DISABLED), reason="device channel disabled")
        return

    try:
        raw = await asyncio.wait_for(websocket.receive_text(), HELLO_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        with contextlib.suppress(Exception):
            await websocket.close(code=int(DeviceCloseCode.AUTH_FAILED), reason="authenticate first")
        return
    try:
        if len(raw.encode("utf-8")) > MAX_CONTROL_FRAME_BYTES:
            raise ValueError("oversized hello")
        hello = DeviceHello.model_validate_json(raw)
    except (ValueError, ValidationError):
        await websocket.close(code=int(DeviceCloseCode.PROTOCOL_ERROR), reason="expected hello")
        return

    outcome = await _authenticate(websocket, hello)
    if isinstance(outcome, DeviceCloseCode):
        await _audit(websocket, AuditAction.DEVICE_CHANNEL_REJECTED, AuditResult.BLOCKED)
        await websocket.close(code=int(outcome), reason="authentication failed")
        return
    resolved = outcome
    principal = resolved.principal

    if hello.mapping_version != MAPPING_VERSION:
        await _audit(websocket, AuditAction.DEVICE_CHANNEL_REJECTED, AuditResult.BLOCKED,
                     user_id=principal.user_id, device_id=principal.device_id)
        await websocket.close(code=int(DeviceCloseCode.MAPPING_VERSION_MISMATCH), reason=MAPPING_VERSION)
        return

    connection = _SocketConnection(websocket)
    session = await hub.attach(user_id=principal.user_id, device_id=principal.device_id, connection=connection)
    await _audit(websocket, AuditAction.DEVICE_CHANNEL_OPENED, AuditResult.SUCCESS,
                 user_id=principal.user_id, device_id=principal.device_id)
    token = hello.access_token
    expires_at = resolved.token_expires_at or _utcnow()
    close_code: DeviceCloseCode | None = None
    try:
        await connection.send_text(
            DeviceHelloAck(
                device_id=principal.device_id, mapping_version=MAPPING_VERSION,
                server_time=_utcnow(), session_expires_at=expires_at,
            ).model_dump_json()
        )
        while True:
            wait = min(REVALIDATE_SECONDS, (expires_at - _utcnow()).total_seconds())
            if wait <= 0:
                close_code = DeviceCloseCode.AUTH_EXPIRED
                break
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), wait)
            except asyncio.TimeoutError:
                check = await _revalidate(websocket, token, session)
                if isinstance(check, DeviceCloseCode):
                    close_code = check
                    break
                continue
            if len(raw.encode("utf-8")) > MAX_SCREENSHOT_FRAME_BYTES:
                close_code = DeviceCloseCode.PROTOCOL_ERROR
                break
            try:
                frame_type = json.loads(raw).get("type")
            except (ValueError, AttributeError):
                close_code = DeviceCloseCode.PROTOCOL_ERROR
                break
            if frame_type == "result":
                hub.deliver(session, raw)
            elif frame_type == "reauth":
                try:
                    reauth = DeviceReauth.model_validate_json(raw)
                except ValidationError:
                    close_code = DeviceCloseCode.PROTOCOL_ERROR
                    break
                check = await _revalidate(websocket, reauth.access_token, session)
                if isinstance(check, DeviceCloseCode):
                    close_code = check
                    break
                token = reauth.access_token
                expires_at = check.token_expires_at or _utcnow()
                await connection.send_text(DeviceReauthAck(session_expires_at=expires_at).model_dump_json())
            elif frame_type == "platform_status":
                try:
                    status = DevicePlatformStatus.model_validate_json(raw)
                except ValidationError:
                    close_code = DeviceCloseCode.PROTOCOL_ERROR
                    break
                hub.record_platform_status(session, status.platforms)
            else:
                close_code = DeviceCloseCode.PROTOCOL_ERROR
                break
    except WebSocketDisconnect:
        pass
    except ConnectionError:
        pass
    finally:
        await hub.detach(session)
        if close_code is not None:
            with contextlib.suppress(Exception):
                await connection.close(int(close_code), close_code.name.lower())
        await _audit(websocket, AuditAction.DEVICE_CHANNEL_CLOSED, AuditResult.SUCCESS,
                     user_id=principal.user_id, device_id=principal.device_id)
