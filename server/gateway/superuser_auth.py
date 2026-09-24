"""Superuser HTTP authentication — the dependency operator routes will use.

    Authorization: Superuser <token>

`[LOCKED]` (02 §12, DASH-004, SUPER-001) an operator surface requires a
**superuser principal**, distinct from any user session: an ordinary Bearer
token can never reach it. This module is the one HTTP entry to that principal,
and it adds no authority of its own:

* **The credential is the existing out-of-band one.** The presented token goes
  straight to `server.security.superuser.authenticate_superuser()` — still the
  only place a `SuperuserGrant` is minted — which compares it in constant time
  against `HYPERMIND_SUPERUSER_TOKEN`. Nothing here reads a session, a user, a
  device, a capability grant, or a model output.
* **Two schemes, never crossed.** A `Bearer` credential is refused here without
  being looked up, and a `Superuser` credential is refused by the user path
  (`server.gateway.deps.bearer_token` accepts only `Bearer`). The superuser
  token is not an access token, so presenting it as `Bearer` fails as well.
* **A separate principal type.** `SuperuserPrincipal` is not a `Principal`: it
  has no `user_id`, `session_id`, `device_id` or graph, so it cannot be passed
  where a user identity is expected, and no user-scoped check can be satisfied
  with it.
* **Every attempt is audited** — `superuser.authenticated` or
  `superuser.rejected` — with only the credential's non-reversible fingerprint,
  never the credential.

A deployment with no superuser credential configured has no superuser: every
attempt is refused, with the same response as a wrong credential, so the
response does not reveal whether one is configured.

No route depends on this yet; operator controls (18 §5.4), break-glass
activation (20 §2.2) and the operator console (28) will.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from fastapi import Depends, Request

from server.gateway.deps import get_audit_logger
from server.gateway.errors import AppError
from server.secrets.requester import SuperuserGrant
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.security.superuser import (
    SuperuserAuthenticationFailed,
    SuperuserNotConfigured,
    authenticate_superuser,
)
from shared.schemas.enums import AuditActor, AuditResult
from shared.schemas.errors import ErrorCode

logger = logging.getLogger("hypermind.gateway.superuser")

SUPERUSER_SCHEME = "Superuser"
_PREFIX = f"{SUPERUSER_SCHEME} "
# Well above the 32-character minimum, well below anything a header needs.
_MAX_CREDENTIAL_CHARS = 1024


@dataclass(frozen=True)
class SuperuserPrincipal:
    """The authenticated server administrator for one request.

    Deliberately shares nothing with `shared.schemas.authorization.Principal`.
    `grant` is the verified `SuperuserGrant` itself — the proof later routes
    hand to the components that require superuser authority.
    """

    grant: SuperuserGrant
    request_id: uuid.UUID

    @property
    def token_fingerprint(self) -> str:
        return self.grant.token_fingerprint


class _Refused(Exception):
    """Internal: the header is absent or not a well-formed Superuser credential."""


def superuser_credential(request: Request) -> str:
    """The credential from `Authorization: Superuser <token>`, or `_Refused`.

    Exact scheme spelling, one header, one separating space, and a token with
    no whitespace. Anything else — including `Bearer` — is refused before any
    comparison, so a user token is never even looked at here.
    """

    values = request.headers.getlist("Authorization")
    if len(values) != 1:
        raise _Refused("missing_or_ambiguous_authorization")
    header = values[0]
    if not header.startswith(_PREFIX):
        raise _Refused("wrong_scheme")
    token = header[len(_PREFIX):]
    if not token or len(token) > _MAX_CREDENTIAL_CHARS or any(c.isspace() for c in token):
        raise _Refused("malformed_credential")
    return token


async def get_superuser(
    request: Request,
    audit: AuditLogger = Depends(get_audit_logger),
) -> SuperuserPrincipal:
    """Authenticate the request's superuser, or refuse with `401`.

    The refusal is an `AppError`, so the request's transaction still commits
    the `superuser.rejected` audit row (02 §1.2) while nothing else changes.
    """

    try:
        grant = authenticate_superuser(superuser_credential(request))
    except SuperuserNotConfigured:
        # Operators need to know; the caller must not.
        logger.warning("superuser authentication attempted, but no superuser credential is configured")
        await _reject(audit, "superuser:not_configured")
    except (_Refused, SuperuserAuthenticationFailed):
        await _reject(audit, "superuser:rejected")

    await audit.record(
        actor=AuditActor.SUPERUSER,
        action=AuditAction.SUPERUSER_AUTHENTICATED,
        resource=f"superuser:{grant.token_fingerprint}",
        result=AuditResult.SUCCESS,
    )
    return SuperuserPrincipal(grant=grant, request_id=audit.request_id)


async def _reject(audit: AuditLogger, resource: str) -> None:
    await audit.record(
        actor=AuditActor.SYSTEM,
        action=AuditAction.SUPERUSER_REJECTED,
        resource=resource,
        result=AuditResult.BLOCKED,
    )
    raise AppError(ErrorCode.UNAUTHENTICATED, "superuser authentication required")
