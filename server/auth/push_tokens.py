"""A device's push registration (docs/23 §4 push wake).

A phone may bind **its own** push registration token, over its own
authenticated session — the device is the caller, never a claim in the body
(PHONE-003). The binding is what lets the server wake exactly that device and
no other:

* A token is unique across devices. If the same token is presented by another
  of the **same user's** devices (the app was re-enrolled on one phone), it
  moves to the caller; the old binding is cleared. A token bound to another
  user's device is refused outright — a token cannot be taken over across
  accounts, even to redirect harmless wakes.
* A revoked device has no binding (`DeviceService.revoke` clears it) and can
  never register one: its session no longer resolves.
* The token itself is never written to the audit trail.

Holding a push token confers nothing: a wake carries no content and asks the
phone only to reconnect and authenticate like any other time.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.errors import AuthError
from server.auth.repository import utcnow
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.storage.models import Device
from shared.schemas.enums import AuditActor, AuditResult
from shared.schemas.push import PushTokenRegistration


class PushTokenRefused(AuthError):
    """A push registration was refused. One generic client message."""

    client_message = "push registration refused"


class PushTokens:
    async def register(
        self, session: AsyncSession, *, device: Device, registration: PushTokenRegistration, audit: AuditLogger
    ) -> None:
        if device.revoked:
            raise PushTokenRefused("device_revoked")
        holders = (
            await session.execute(
                select(Device).where(Device.push_token == registration.token, Device.device_id != device.device_id)
            )
        ).scalars().all()
        for holder in holders:
            if holder.user_id != device.user_id:
                await self._audit(audit, device, AuditAction.DEVICE_PUSH_TOKEN_REFUSED, AuditResult.BLOCKED)
                raise PushTokenRefused("token_bound_to_another_user")
            # The same user's stale enrollment of this very app install.
            self._clear(holder)
            await self._audit(audit, holder, AuditAction.DEVICE_PUSH_TOKEN_CLEARED, AuditResult.SUCCESS)
        await session.flush()
        device.push_provider = registration.provider
        device.push_token = registration.token
        device.push_token_registered_at = utcnow()
        await session.flush()
        await self._audit(audit, device, AuditAction.DEVICE_PUSH_TOKEN_REGISTERED, AuditResult.SUCCESS)

    async def clear(self, session: AsyncSession, *, device: Device, audit: AuditLogger) -> None:
        if device.push_token is None and device.push_provider is None:
            return
        self._clear(device)
        await session.flush()
        await self._audit(audit, device, AuditAction.DEVICE_PUSH_TOKEN_CLEARED, AuditResult.SUCCESS)

    @staticmethod
    def _clear(device: Device) -> None:
        device.push_provider = None
        device.push_token = None
        device.push_token_registered_at = None

    @staticmethod
    async def _audit(audit: AuditLogger, device: Device, action: AuditAction, result: AuditResult) -> None:
        await audit.record(
            actor=AuditActor.USER,
            action=action,
            resource=f"device:{device.device_id}",
            result=result,
            user_id=device.user_id,
            device_id=device.device_id,
        )
