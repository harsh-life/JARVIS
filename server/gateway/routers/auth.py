"""Auth / identity / device endpoints — 02 §3, realized by `03`.

Public endpoints here are the two 02 §1.1 permits without a token — the OIDC
start and callback — plus, by OD-DESK-1, the desktop handoff redemption, which
requires the one-time handoff code *and* the PKCE verifier only the companion
holds (`server/auth/desktop_handoff.py`). Everything else requires a credential.

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
from datetime import timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from server.gateway.deps import (
    bearer_token,
    get_audit_logger,
    get_db_session,
    get_resolved_session,
    get_security_core,
)
from server.gateway.device_scope import DesktopScope, desktop_scope
from server.gateway.errors import AppError
from server.gateway.security import SecurityCore
from server.auth.desktop_handoff import (
    CHALLENGE_PATTERN,
    DESKTOP_PORT_MAX,
    DESKTOP_PORT_MIN,
    DESKTOP_STATE_PATTERN,
    VERIFIER_PATTERN,
    loopback_redirect,
    redeem_desktop_handoff,
)
from server.auth.errors import AuthError, StepUpRequired
from server.auth.login import DesktopLoginRequest
from server.auth.sessions import ResolvedSession
from server.config.schema import AndroidConfig, DesktopConfig
from shared.schemas.device_channel import DeviceCloseCode
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from shared.schemas.enums import AuditActor, AuditResult, DevicePlatform
from shared.schemas.errors import ErrorCode

router = APIRouter(tags=["auth"])

# Served at the origin root, outside /api/v1: Android's App Link verification
# and the App Link path itself are fixed locations on the host (docs/23 §3).
public_router = APIRouter(tags=["auth"])

# The App Link path the Android app claims. The bootstrap token rides in the
# URL *fragment*, which a browser never sends to a server — so even when the
# link is not intercepted by the app, the token reaches no access log.
APP_LINK_PATH = "/app/login"
_APP_STATE_PATTERN = r"^[A-Za-z0-9_-]{32,128}$"
_NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


async def _disconnect_device(
    request: Request,
    session: AsyncSession,
    device_id: uuid.UUID,
    code: DeviceCloseCode,
    reason: str,
) -> None:
    """Close the device's socket *after* the change that ends its authority is
    committed — otherwise a reconnect in that window would still authenticate
    against the pre-revocation state."""

    hub = getattr(request.app.state, "device_hub", None)
    if hub is not None:
        await session.commit()
        await hub.disconnect(device_id, code, reason)


def _android(request: Request) -> AndroidConfig:
    return getattr(request.app.state, "android", None) or AndroidConfig()


def _desktop(request: Request) -> DesktopConfig:
    return getattr(request.app.state, "desktop", None) or DesktopConfig()


class OIDCStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    redirect_url: str


class DesktopHandoffRequest(BaseModel):
    """OD-DESK-1: the companion redeems its loopback handoff code with the PKCE
    verifier it generated before opening the browser."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=20, max_length=128)
    code_verifier: str = Field(pattern=VERIFIER_PATTERN)


class DesktopHandoffResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bootstrap_token: str


class OIDCCallbackResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    needs_device_registration: bool
    bootstrap_token: str


class DeviceRegistrationRequest(BaseModel):
    """`public_key`/`key_proof`: a device that generated its own Ed25519 key
    pair (docs/23 §3 — the Android client, whose private key never leaves the
    Keystore) registers only the public half, with a signature proving it
    holds the private one. Both or neither."""

    model_config = ConfigDict(extra="forbid")

    platform: DevicePlatform = DevicePlatform.ANDROID
    public_key: str | None = Field(default=None, min_length=40, max_length=64)
    key_proof: str | None = Field(default=None, min_length=80, max_length=128)


class DeviceRegistrationResponse(BaseModel):
    """02 §3 — `device_credential` is delivered here and nowhere else, ever.
    Omitted when the device supplied its own public key: the server then never
    had a private key to return."""

    model_config = ConfigDict(extra="forbid")

    device_id: uuid.UUID
    device_credential: str | None = None


class DeviceRotationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_key: str | None = Field(default=None, min_length=40, max_length=64)
    key_proof: str | None = Field(default=None, min_length=80, max_length=128)


class DeviceRotationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: uuid.UUID
    device_credential: str | None = None


@router.get("/auth/oidc/start", response_model=OIDCStartResponse)
async def oidc_start(
    request: Request,
    app_state: str | None = Query(default=None, pattern=_APP_STATE_PATTERN),
    desktop_state: str | None = Query(default=None, pattern=DESKTOP_STATE_PATTERN),
    desktop_challenge: str | None = Query(default=None, pattern=CHALLENGE_PATTERN),
    desktop_port: int | None = Query(default=None, ge=DESKTOP_PORT_MIN, le=DESKTOP_PORT_MAX),
    session: AsyncSession = Depends(get_db_session),
    core: SecurityCore = Depends(get_security_core),
    audit: AuditLogger = Depends(get_audit_logger),
) -> OIDCStartResponse:
    """AUTH-001/005 — begin login with state, nonce, and PKCE (03 §2.1).

    Public by necessity: the caller has no Hypermind credential yet. It creates
    only a short-lived, single-use login-state row and grants nothing.

    `app_state` (docs/23 §3): the Android app's own nonce for this login. With
    it, the callback hands the bootstrap token back to the app through its App
    Link instead of as JSON, and the app accepts it only if the nonce is the one
    it generated — so a login someone else started cannot be injected into it.
    """

    if app_state is not None and not _android(request).app_links.configured:
        raise AppError(
            ErrorCode.VALIDATION_FAILED,
            "the Android App Link login handoff is not configured on this server",
        )
    # OD-DESK-1: the desktop companion's loopback handoff. All three values or
    # none, never together with the Android handoff, and only when enabled.
    desktop_values = (desktop_state, desktop_challenge, desktop_port)
    desktop: DesktopLoginRequest | None = None
    if any(value is not None for value in desktop_values):
        if any(value is None for value in desktop_values) or app_state is not None:
            raise AppError(ErrorCode.VALIDATION_FAILED, "incomplete desktop login request")
        if not _desktop(request).enabled:
            raise AppError(
                ErrorCode.VALIDATION_FAILED,
                "the desktop companion login is not enabled on this server",
            )
        desktop = DesktopLoginRequest(state=desktop_state, challenge=desktop_challenge, port=desktop_port)
    start = await core.login_flow.start(session, audit=audit, app_state=app_state, desktop=desktop)
    return OIDCStartResponse(redirect_url=start.redirect_url)


@router.get("/auth/oidc/callback", response_model=OIDCCallbackResponse)
async def oidc_callback(
    request: Request,
    code: str,
    state: str,
    session: AsyncSession = Depends(get_db_session),
    core: SecurityCore = Depends(get_security_core),
    audit: AuditLogger = Depends(get_audit_logger),
) -> Response:
    """AUTH-004/005 — validate everything 03 §2.3 locks, then map `sub` → User.

    Returns a bootstrap token, **not** an access token: at this point the user is
    authenticated but has no registered device, and 03 §1 is explicit that a
    Google token never authorizes a Hypermind API call.

    A login started with an `app_state` ends in a redirect to the App Link
    instead (docs/23 §3) — same token, same single use, delivered in the
    fragment so it never enters a server log.
    """

    desktop_config = _desktop(request)
    completion = await core.login_flow.complete(
        session,
        code=code,
        state=state,
        audit=audit,
        desktop_handoff_ttl=timedelta(seconds=desktop_config.handoff_ttl_seconds),
    )
    if completion.desktop is not None:
        if not desktop_config.enabled:
            # Disabled between start and callback: fail closed. The handoff
            # code just minted is never delivered, and expires unredeemed.
            raise AppError(ErrorCode.VALIDATION_FAILED, "desktop login is not enabled")
        # OD-DESK-1: back to the companion's loopback listener with a code
        # that is worthless without the companion's verifier. No bearer
        # credential is in this URL, and the host is fixed to 127.0.0.1.
        return RedirectResponse(
            loopback_redirect(
                port=completion.desktop.port,
                code=completion.desktop.handoff_code,
                desktop_state=completion.desktop.state,
            ),
            status_code=status.HTTP_302_FOUND,
            headers=_NO_STORE,
        )
    if completion.app_state is not None:
        base = _android(request).app_links.base_url
        if base is None:
            # Configuration was removed between start and callback: fail closed
            # rather than falling back to a JSON body the browser would display.
            raise AppError(ErrorCode.VALIDATION_FAILED, "App Link handoff is not configured")
        fragment = urlencode(
            {"bootstrap_token": completion.bootstrap_token, "app_state": completion.app_state}
        )
        return RedirectResponse(
            f"{base.rstrip('/')}{APP_LINK_PATH}#{fragment}",
            status_code=status.HTTP_302_FOUND,
            headers=_NO_STORE,
        )
    return JSONResponse(
        OIDCCallbackResponse(
            needs_device_registration=completion.needs_device_registration,
            bootstrap_token=completion.bootstrap_token,
        ).model_dump(mode="json"),
        headers=_NO_STORE,
    )


@router.post("/auth/desktop/handoff", response_model=DesktopHandoffResponse)
async def desktop_handoff(
    request: Request,
    body: DesktopHandoffRequest,
    session: AsyncSession = Depends(get_db_session),
    audit: AuditLogger = Depends(get_audit_logger),
) -> Response:
    """OD-DESK-1 — redeem the loopback handoff code for a bootstrap token
    bound to the `desktop` platform.

    Public in the same sense as the OIDC callback: the caller has no Hypermind
    credential yet. What it must hold instead is the one-time code the callback
    returned to its loopback listener *and* the PKCE verifier it generated
    before the login began — the code alone is refused. Every failure is the
    same `401`, audited without the code or the verifier.
    """

    if not _desktop(request).enabled:
        raise AppError(ErrorCode.NOT_FOUND, "not found")
    try:
        user_id, bootstrap = await redeem_desktop_handoff(
            session, code=body.code, code_verifier=body.code_verifier
        )
    except AuthError:
        await audit.record(
            actor=AuditActor.SYSTEM,
            action=AuditAction.DESKTOP_HANDOFF_REJECTED,
            resource="auth:desktop_handoff",
            result=AuditResult.BLOCKED,
        )
        raise
    await audit.record(
        actor=AuditActor.SYSTEM,
        action=AuditAction.DESKTOP_HANDOFF_REDEEMED,
        resource=f"user:{user_id}",
        result=AuditResult.SUCCESS,
        user_id=user_id,
    )
    await audit.record(
        actor=AuditActor.SYSTEM,
        action=AuditAction.BOOTSTRAP_TOKEN_ISSUED,
        resource=f"user:{user_id}",
        result=AuditResult.SUCCESS,
        user_id=user_id,
    )
    return JSONResponse(
        DesktopHandoffResponse(bootstrap_token=bootstrap).model_dump(mode="json"),
        headers=_NO_STORE,
    )


class AppPolicyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    non_sensitive: list[str]
    sensitive: list[str]
    payment: list[str]


@router.get("/devices/app-policy", response_model=AppPolicyResponse)
async def app_policy(
    request: Request,
    resolved: ResolvedSession = Depends(get_resolved_session),
) -> AppPolicyResponse:
    """The sensitive-app classification, for the device to cache (docs/23
    §5.2). The device uses it only to refuse early and to show which apps can
    be controlled at all — it never makes anything allowed; the engine
    enforces the same table regardless."""

    classification = _android(request).app_classification
    return AppPolicyResponse(
        non_sensitive=sorted(classification.non_sensitive),
        sensitive=sorted(classification.sensitive),
        payment=sorted(classification.payment),
    )


@public_router.get("/.well-known/assetlinks.json", include_in_schema=False)
async def asset_links(request: Request) -> Response:
    """Digital Asset Links: lets Android verify that the JARVIS app — this
    package, signed with these certificates — owns the App Link path, so no
    other app can claim the login return (docs/23 §3)."""

    links = _android(request).app_links
    if not links.configured:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return JSONResponse(
        [
            {
                "relation": ["delegate_permission/common.handle_all_urls"],
                "target": {
                    "namespace": "android_app",
                    "package_name": links.package_name,
                    "sha256_cert_fingerprints": links.sha256_cert_fingerprints,
                },
            }
        ]
    )


@public_router.get(APP_LINK_PATH, include_in_schema=False)
async def app_link_fallback() -> HTMLResponse:
    """Reached only when the App Link was *not* intercepted (the app is not
    installed on this device, or the link was opened elsewhere). The token in
    the fragment was never sent here, and this page contains no script that
    could read it."""

    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><title>JARVIS</title>"
        "<p>Open this sign-in on the phone where the JARVIS app is installed.</p>",
        headers={**_NO_STORE, "Content-Security-Policy": "default-src 'none'"},
    )


@router.post(
    "/devices",
    response_model=DeviceRegistrationResponse,
    response_model_exclude_none=True,
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
        public_key=body.public_key,
        key_proof=body.key_proof,
    )
    return DeviceRegistrationResponse(
        device_id=registered.device.device_id,
        device_credential=registered.device_credential,
    )


@router.post(
    "/devices/{device_id}/rotate",
    response_model=DeviceRotationResponse,
    response_model_exclude_none=True,
)
async def rotate_device_credential(
    request: Request,
    device_id: uuid.UUID,
    body: DeviceRotationRequest | None = None,
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

    try:
        core.sessions.require_step_up(resolved)
    except StepUpRequired as exc:
        # `server/auth/errors.py`'s own [LOCKED] contract: an AuthError is an
        # AuditEvent too, not only a 401 — `server.auth` cannot write this
        # itself (16 §2 layering), so the one caller today keeps that half of
        # the promise. Any future `require_step_up`/`require_reattestation`
        # call site must do the same.
        await audit.record(actor=AuditActor.USER, action=AuditAction.STEP_UP_REQUIRED,
                           resource=f"auth:step_up:{exc.reason}"[:128], result=AuditResult.BLOCKED,
                           user_id=resolved.principal.user_id, device_id=resolved.principal.device_id,
                           session_id=resolved.principal.session_id)
        raise

    device = await core.auth_repository.get_device(session, device_id)
    if device is None or device.user_id != resolved.principal.user_id:
        # 04 §7: do not confirm another user's device id exists.
        raise AppError(ErrorCode.NOT_FOUND, "not found")
    if device.revoked:
        raise AppError(ErrorCode.NOT_FOUND, "not found")

    credential = await core.devices.rotate_credential(
        session,
        device=device,
        audit=audit,
        public_key=body.public_key if body else None,
        key_proof=body.key_proof if body else None,
    )
    # The socket was authenticated by tokens the rotation just revoked; the
    # device reconnects with its new key.
    await _disconnect_device(request, session, device.device_id, DeviceCloseCode.AUTH_EXPIRED, "credential rotated")
    return DeviceRotationResponse(device_id=device.device_id, device_credential=credential)


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
@desktop_scope(DesktopScope.OWN_DEVICE)
async def revoke_device(
    request: Request,
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

    # docs/23 §4: revocation closes the device's socket immediately — any
    # operation in flight on it fails, and nothing waits for a reconnect.
    await audit.record(
        actor=AuditActor.USER,
        action=AuditAction.SESSION_LOGGED_OUT,
        resource=f"device:{device_id}",
        result=AuditResult.SUCCESS,
        user_id=resolved.principal.user_id,
        device_id=device_id,
    )
    await _disconnect_device(request, session, device_id, DeviceCloseCode.REVOKED, "device revoked")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
