"""Mapping security failures onto 02 §1.7's canonical error codes.

Installed as exception handlers rather than translated in each router, so an
endpoint cannot forget — and so no endpoint can accidentally render a *different*
status for the same failure, which is how anti-enumeration guarantees erode.

`[LOCKED]` (02 §1.7) the mapping:

| Failure | code | HTTP |
|---|---|---|
| any authentication failure | `unauthenticated` | 401 |
| expired access token | `token_expired` | 401 |
| absolute-floor action | `prohibited` | 403 |
| authorization denied, resource visible to caller | `unauthorized` | 403 |
| authorization denied, resource not visible | `not_found` | 404 |
| action needs human confirmation | `confirmation_required` | 403 |
| the SecretStore is locked or unusable | `dependency_unavailable` | 503 |

`[LOCKED]` (02 §1.6) no message here carries a diagnostic reason. Every
`AuthError` keeps its internal `reason` for the audit trail and renders only its
client-safe text — "the audience claim did not match" would tell a prober
exactly which check to work around.

Step-up (03 §5.5) has no dedicated code in 02 §1.7's table, and inventing one
would be an unratified addition to a `[LOCKED]` registry. It is surfaced as
`401 unauthenticated` — which is accurate, since the remedy is to
re-authenticate — with a `step_up_required` flag in `details` so the client can
prompt for re-attestation rather than a full Google login.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from server.auth.errors import AccessTokenExpired, AuthError, StepUpRequired
from server.capabilities.floor import AbsoluteFloorViolation
from server.gateway.errors import AppError, app_error_handler
from server.graph.service import GraphOperationRefused
from server.secrets.errors import SecretDenied, SecretNotFound, SecretStoreError
from shared.schemas.errors import ErrorCode

logger = logging.getLogger("hypermind.gateway.security")


async def auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
    if isinstance(exc, AccessTokenExpired):
        code = ErrorCode.TOKEN_EXPIRED
        details: dict = {}
    elif isinstance(exc, StepUpRequired):
        code = ErrorCode.UNAUTHENTICATED
        details = {"step_up_required": True}
    else:
        code = ErrorCode.UNAUTHENTICATED
        details = {}

    return await app_error_handler(
        request, AppError(code, exc.client_message, details=details)
    )


async def absolute_floor_handler(
    request: Request, exc: AbsoluteFloorViolation
) -> JSONResponse:
    """PERM-006 → `403 prohibited`.

    The prohibition's category is included: unlike an authorization denial, this
    leaks nothing about another principal's data — it names a class of action
    that is prohibited for everyone — and it tells an operator reading a client
    report which floor was hit.
    """

    return await app_error_handler(
        request,
        AppError(
            ErrorCode.PROHIBITED,
            "this action is prohibited and cannot be confirmed",
            details={"category": exc.category.value},
        ),
    )


async def secret_store_error_handler(
    request: Request, exc: SecretStoreError
) -> JSONResponse:
    """12 §8 — a locked or unusable SecretStore is an **explicit** failure state.

    "the server surfaces an explicit 'locked' state rather than running in a
    degraded, silently-secretless mode that fabricates success." 02 §1.7's
    `dependency_unavailable` is the code for exactly this — its own description
    names the secret backend — and it is `retryable`, which is accurate: an
    operator unlocking the store fixes it without the client changing anything.

    The two authorization-shaped outcomes are separated out, because a refusal is
    not an outage and a client should not retry it. Note that nothing here names
    the handle or the requester: a caller learns that the operation could not be
    completed, not which secret was involved.

    This handler does **not** commit the request's transaction (`SecretStoreError`
    is deliberately absent from `SECURITY_REFUSALS` in `server/gateway/deps.py`),
    so a half-finished operation — a spent bootstrap token, a device row with no
    credential — is rolled back rather than persisted.
    """

    if isinstance(exc, SecretDenied):
        return await app_error_handler(
            request, AppError(ErrorCode.UNAUTHORIZED, "not permitted")
        )
    if isinstance(exc, SecretNotFound):
        return await app_error_handler(request, AppError(ErrorCode.NOT_FOUND, "not found"))

    logger.error("secret store unusable: %s", type(exc).__name__)
    return await app_error_handler(
        request,
        AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "a required credential store is unavailable",
            details={"dependency": "secret_store"},
        ),
    )


async def graph_operation_handler(
    request: Request, exc: GraphOperationRefused
) -> JSONResponse:
    """04 §7's two surfaces, chosen by the refusal rather than by the handler.

    `GraphOperationRefused` carries `surface_as_not_found` precisely so this
    function never has to guess — the guess is the enumeration oracle.
    """

    if exc.surface_as_not_found:
        return await app_error_handler(
            request, AppError(ErrorCode.NOT_FOUND, "not found")
        )
    return await app_error_handler(
        request, AppError(ErrorCode.UNAUTHORIZED, "not permitted")
    )


def install_security_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AuthError, auth_error_handler)
    app.add_exception_handler(AbsoluteFloorViolation, absolute_floor_handler)
    app.add_exception_handler(GraphOperationRefused, graph_operation_handler)
    app.add_exception_handler(SecretStoreError, secret_store_error_handler)
