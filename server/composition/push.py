"""Wiring the optional push wake (docs/23 §4) — only when configured.

`android.push.provider: none` (the default) builds nothing: no sender, no
credential lookup, no outbound call to any push provider, ever.

With `fcm`, the server's sending credential (a Google service-account key) is
resolved at the moment of a send, never cached as raw text: from an
environment variable, or from the SecretStore as a **server-owned** secret of
class `oauth_token`. Any other class is refused even when the handle exists,
so the push configuration can never be pointed at, say, a device credential or
a model API key and ship it to Google (12 §2, the same rule the model resolver
applies to its own class).
"""

from __future__ import annotations

import os
import uuid

from fastapi import FastAPI

from server.composition.secret_context import SecretUnavailable
from server.config.schema import AppConfig
from server.execution.device_hub import DeviceHub
from server.execution.device_wake import CredentialProvider, DeviceWaker, FcmWakeSender, WakeOutcome
from server.secrets.audit_port import SecretAuditEvent
from server.secrets.requester import SecretRequester
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.storage.models import SecretReference
from shared.schemas.enums import AuditActor, AuditResult, SecretClass
from shared.schemas.push import PushProvider


def push_credential(secret_ref: str, app: FastAPI) -> CredentialProvider:
    if secret_ref.startswith("env:"):
        env_name = secret_ref[len("env:"):]

        async def from_env() -> str:
            value = os.environ.get(env_name)
            if not value:
                raise SecretUnavailable()
            return value

        return from_env

    handle = secret_ref[len("secretstore:"):] if secret_ref.startswith("secretstore:") else secret_ref
    requester = SecretRequester.server()

    async def from_store() -> str:
        async with app.state.storage.session() as db:
            audit = AuditLogger(db, request_id=uuid.uuid4())
            reference = await db.get(SecretReference, handle)
            if reference is None or reference.class_ is not SecretClass.OAUTH_TOKEN:
                await audit.record_secret_event(SecretAuditEvent(
                    action="secret.get", secret_ref=handle, requester=requester.describe(),
                    result=AuditResult.BLOCKED, reason="not_a_push_credential",
                ))
                await db.commit()
                raise SecretUnavailable()
            value = await app.state.security.secret_store.get(db, handle, requester, audit)
            await db.commit()
            return value

    return from_store


def wake_recorder(app: FastAPI):
    """Audit what became of a wake: the device and user, never the token."""

    async def record(device_id: uuid.UUID, user_id: uuid.UUID, outcome: WakeOutcome) -> None:
        sent = outcome is WakeOutcome.SENT
        async with app.state.storage.session() as db:
            await AuditLogger(db, request_id=uuid.uuid4()).record(
                actor=AuditActor.SYSTEM,
                action=AuditAction.DEVICE_WAKE_SENT if sent else AuditAction.DEVICE_WAKE_FAILED,
                resource=f"device:{device_id}",
                result=AuditResult.SUCCESS if sent else AuditResult.FAILURE,
                user_id=user_id,
                device_id=device_id,
            )
            await db.commit()

    return record


def attach_push_wake(config: AppConfig, app: FastAPI, hub: DeviceHub | None) -> DeviceWaker | None:
    push = config.android.push
    if hub is None or push.provider is not PushProvider.FCM or push.fcm is None:
        app.state.device_waker = None
        return None
    sender = FcmWakeSender(
        project_id=push.fcm.client.project_id,
        credential=push_credential(push.fcm.service_account_ref, app),
    )

    async def is_connected(device_id: uuid.UUID) -> bool:
        return await hub.is_connected(device_id=device_id)

    waker = DeviceWaker(
        storage=app.state.storage, sender=sender, is_connected=is_connected, record=wake_recorder(app),
        min_interval_seconds=push.fcm.min_interval_seconds,
    )
    hub.attach_waker(waker)
    app.state.device_waker = waker
    return waker
