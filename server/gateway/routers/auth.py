"""Auth / identity / device endpoints — 02 §3, realized by `03`.

Public endpoints here are exactly the two 02 §1.1 permits without a token: the
OIDC start and callback. Everything else requires a credential.

## Why device registration does not use an `Idempotency-Key`

02 §1.4 lists device registration among the endpoints that must be idempotent,
and 02 §3 `[LOCKED]`s that the device credential "is returned exactly once at
registration, never retrievable again, never in any log/audit/usage record".

Those two cannot both be satisfied by a stored-response idempotency record: the
stored response *is* a copy of the credential, sitting in the database. So the
`[LOCKED]` SECRET-004 rule wins, and the duplicate-registration guarantee is
provided by the mechanism that was already there and is strictly stronger — the
**single-use bootstrap token** (03 §3.2). A retried registration cannot create a
second device because the token is spent, and 02 §3's own error list anticipates
this with `409 (idempotency)`. The client's recovery is to re-authenticate, which
is also the only way it could legitimately receive a new credential.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from server.gateway.deps import (
    bearer_token,
    get_audit_logger,
    get_db_session,
    get_resolved_session,
    get_security_core,
)
from server.gateway.errors import AppError
from server.gateway.security import SecurityCore
from server.auth.sessions import ResolvedSession
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from shared.schemas.enums import AuditActor, AuditResult, DevicePlatform
from shared.schemas.errors import ErrorCode

router = APIRouter(tags=["auth"])


class OIDCStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    redirect_url: str


class OIDCCallbackResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    needs_device_registration: bool
    bootstrap_token: str


class DeviceRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: DevicePlatform = DevicePlatform.ANDROID


class DeviceRegistrationResponse(BaseModel):
    """02 §3 — `device_credential` is delivered here and nowhere else, ever."""

    model_config = ConfigDict(extra="forbid")

    device_id: uuid.UUID
    device_credential: str


class DeviceRotationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: uuid.UUID
    device_credential: str


@router.get("/auth/oidc/start", response_model=OIDCStartResponse)
async def oidc_start(
    session: AsyncSession = Depends(get_db_session),
    core: SecurityCore = Depends(get_security_core),
    audit: AuditLogger = Depends(get_audit_logger),
) -> OIDCStartResponse:
    """AUTH-001/005 — begin login with state, nonce, and PKCE (03 §2.1).

    Public by necessity: the caller has no Hypermind credential yet. It creates
    only a short-lived, single-use login-state row and grants nothing.
    """

    start = await core.login_flow.start(session, audit=audit)
    return OIDCStartResponse(redirect_url=start.redirect_url)


@router.get("/auth/oidc/callback", response_model=OIDCCallbackResponse)
async def oidc_callback(
    code: str,
    state: str,
    session: AsyncSession = Depends(get_db_session),
    core: SecurityCore = Depends(get_security_core),
    audit: AuditLogger = Depends(get_audit_logger),
) -> OIDCCallbackResponse:
    """AUTH-004/005 — validate everything 03 §2.3 locks, then map `sub` → User.

    Returns a bootstrap token, **not** an access token: at this point the user is
    authenticated but has no registered device, and 03 §1 is explicit that a
    Google token never authorizes a Hypermind API call.
    """

    completion = await core.login_flow.complete(
        session, code=code, state=state, audit=audit
    )
    return OIDCCallbackResponse(
        needs_device_registration=completion.needs_device_registration,
        bootstrap_token=completion.bootstrap_token,
    )


@router.post(
    "/devices",
    response_model=DeviceRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_device(
    request: Request,
    body: DeviceRegistrationRequest,
    session: AsyncSession = Depends(get_db_session),
    core: SecurityCore = Depends(get_security_core),
    audit: AuditLogger = Depends(get_audit_logger),
) -> DeviceRegistrationResponse:
    """DEVICE-001 (02 §3, 03 §3.1).

    Authenticated by the **bootstrap token** from the callback, carried as a
    bearer credential. It is not an access token and is accepted by no other
    endpoint (03 §3.2's register-only scope, which holds by there being no other
    consumer of `consume_bootstrap_token`).
    """

    registered = await core.devices.register(
        session,
        bootstrap_token=bearer_token(request),
        platform=body.platform,
        audit=audit,
    )
    return DeviceRegistrationResponse(
        device_id=registered.device.device_id,
        device_credential=registered.device_credential,
    )


@router.post("/devices/{device_id}/rotate", response_model=DeviceRotationResponse)
async def rotate_device_credential(
    device_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    core: SecurityCore = Depends(get_security_core),
    resolved: ResolvedSession = Depends(get_resolved_session),
    audit: AuditLogger = Depends(get_audit_logger),
) -> DeviceRotationResponse:
    """03 §4.3 — rotate a device credential without a full Google re-login.

    Step-up required (03 §5.5, SESSION-003): rotating the credential that
    authenticates every future refresh is exactly the "sensitive operation" the
    doc names, so a valid-but-stale access token is not enough (AUTH-T9).

    `[IMPL]` 02 §3's endpoint table does not list a rotate path, but 03 §4.3
    requires rotation to happen "via an authenticated rotate call". This is that
    call, added under 02's own versioned namespace rather than by overloading an
    existing endpoint.
    """

    core.sessions.require_step_up(resolved)

    device = await core.auth_repository.get_device(session, device_id)
    if device is None or device.user_id != resolved.principal.user_id:
        # 04 §7: do not confirm another user's device id exists.
        raise AppError(ErrorCode.NOT_FOUND, "not found")
    if device.revoked:
        raise AppError(ErrorCode.NOT_FOUND, "not found")

    credential = await core.devices.rotate_credential(session, device=device, audit=audit)
    return DeviceRotationResponse(device_id=device.device_id, device_credential=credential)


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_device(
    device_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    core: SecurityCore = Depends(get_security_core),
    resolved: ResolvedSession = Depends(get_resolved_session),
    audit: AuditLogger = Depends(get_audit_logger),
) -> Response:
    """SESSION-002/005 (02 §3, 03 §4.4) — the lost-or-stolen-phone path.

    Deliberately **not** step-up gated. 03 §7's stolen-device row makes
    revocation the mitigation for a compromised device; requiring fresh
    re-attestation to revoke would mean the owner of a stolen phone might be
    unable to perform the one action that helps them.
    """

    device = await core.devices.revoke(
        session,
        device_id=device_id,
        actor_user_id=resolved.principal.user_id,
        audit=audit,
    )
    if device is None:
        raise AppError(ErrorCode.NOT_FOUND, "not found")

    await audit.record(
        actor=AuditActor.USER,
        action=AuditAction.SESSION_LOGGED_OUT,
        resource=f"device:{device_id}",
        result=AuditResult.SUCCESS,
        user_id=resolved.principal.user_id,
        device_id=device_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
