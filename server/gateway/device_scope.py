"""Device-platform scope: what a session may reach, by its device's platform
(OD-DESK-1).

A phone (`android`) session reaches the whole API, exactly as before. A
**desktop** session — the pixel-art desktop companion — reaches only the
presentation surface: its activity report, its own logout, and the
revocation of its own device. It cannot submit, cancel or confirm a task,
approve an action, step up, open the device channel, bind a push token, read
`/me`, change a setting or revoke another device. The companion presents; it
never authorizes (docs/CHARACTER_DESKTOP_PROPOSAL.md §1).

This is enforced here, at the one place every HTTP request's identity is
resolved (`server/gateway/deps.get_resolved_session`), not by the companion
choosing not to call those routes. The device channel, which authenticates
outside that dependency, refuses non-`android` devices itself
(`server/gateway/routers/device_channel.py`).

A route joins the desktop scope only by an explicit marker on its handler,
`@desktop_scope(...)`, so the set is greppable and pinned by
`tests/security_core/test_desktop_device.py`. **Fail closed:** a platform with
no entry below reaches nothing.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Callable, TypeVar

from fastapi import Request

from server.auth.sessions import ResolvedSession
from server.gateway.errors import AppError
from shared.schemas.enums import DevicePlatform
from shared.schemas.errors import ErrorCode

_MARKER = "__hypermind_desktop_scope__"

F = TypeVar("F", bound=Callable[..., object])


class DesktopScope(str, Enum):
    """Why a route is open to a desktop session."""

    # Read-only presentation data for the caller's own character.
    PRESENTATION = "presentation"
    # Ends the caller's own session.
    OWN_SESSION = "own_session"
    # Acts on a device named in the path — permitted only when that device is
    # the caller's own (`{device_id}` == the token's device).
    OWN_DEVICE = "own_device"


def desktop_scope(scope: DesktopScope) -> Callable[[F], F]:
    """Mark a route handler as reachable by a desktop session."""

    def mark(handler: F) -> F:
        setattr(handler, _MARKER, scope)
        return handler

    return mark


def desktop_scope_of(handler: object) -> DesktopScope | None:
    value = getattr(handler, _MARKER, None)
    return value if isinstance(value, DesktopScope) else None


class DeviceScopeRefused(AppError):
    """A session reached past its device's scope. `403`, one message."""

    def __init__(self) -> None:
        super().__init__(ErrorCode.UNAUTHORIZED, "not permitted from this device")


def enforce_device_scope(request: Request, resolved: ResolvedSession) -> None:
    """Raise `DeviceScopeRefused` unless this session's device may reach
    the matched route."""

    platform = resolved.device_platform
    if platform is DevicePlatform.ANDROID:
        return
    if platform is DevicePlatform.DESKTOP:
        scope = desktop_scope_of(request.scope.get("endpoint"))
        if scope is None:
            raise DeviceScopeRefused()
        if scope is DesktopScope.OWN_DEVICE:
            target = request.scope.get("path_params", {}).get("device_id")
            try:
                own = uuid.UUID(str(target)) == resolved.principal.device_id
            except ValueError:
                own = False
            if not own:
                raise DeviceScopeRefused()
        return
    # An unknown or absent platform reaches nothing.
    raise DeviceScopeRefused()
