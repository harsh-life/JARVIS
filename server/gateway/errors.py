"""The standard error envelope + canonical error-code registry, wired into
FastAPI (02_API_PROTOCOL.md §1.6/§1.7).

This module distinguishes exactly the concepts §9 of this branch's
instructions says must stay separate: authentication, authorization,
visibility, validation, and dependency failure are different `ErrorCode`
values, not one generic "error." Foundation wires the *registry and
plumbing* for all of them; it does not decide *when* unauthenticated/
unauthorized/prohibited fire, because no auth/authz engine exists yet in
this branch. Only validation, not_found (for unmatched routes), and
internal_error are actually raised by anything in this branch today.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from shared.schemas.errors import ERROR_CODE_TABLE, ErrorCode, ErrorDetail, ErrorEnvelope

logger = logging.getLogger("hypermind.gateway")


class AppError(Exception):
    """Raise this (not a bare HTTPException) for any Track B error that
    should render as the canonical envelope. `code` must be one of
    `ErrorCode` — see §9: "Do not casually invent new error codes."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        default_status, default_retryable = ERROR_CODE_TABLE[code]
        self.http_status = default_status
        self.retryable = retryable if retryable is not None else default_retryable


def _envelope_response(
    *, request: Request, code: ErrorCode, message: str, http_status: int, retryable: bool, details: dict
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code=code,
            message=message,
            request_id=str(request_id) if request_id else "",
            retryable=retryable,
            details=details,
        )
    )
    return JSONResponse(status_code=http_status, content=envelope.model_dump(mode="json"))


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return _envelope_response(
        request=request,
        code=exc.code,
        message=exc.message,
        http_status=exc.http_status,
        retryable=exc.retryable,
        details=exc.details,
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    http_status, retryable = ERROR_CODE_TABLE[ErrorCode.VALIDATION_FAILED]
    return _envelope_response(
        request=request,
        code=ErrorCode.VALIDATION_FAILED,
        message="request body failed validation",
        http_status=http_status,
        retryable=retryable,
        details={"errors": exc.errors()},
    )


_HTTP_STATUS_TO_CODE: dict[int, ErrorCode] = {
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.UNAUTHORIZED,
    404: ErrorCode.NOT_FOUND,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.VALIDATION_FAILED,
    429: ErrorCode.RATE_LIMITED,
    503: ErrorCode.DEPENDENCY_UNAVAILABLE,
}


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Fallback for any raw Starlette/FastAPI HTTPException (e.g. an
    unmatched route -> 404, wrong method -> 405). Maps by status code where
    the registry has an equivalent; anything else degrades to
    internal_error rather than leaking Starlette's default detail text.
    """

    code = _HTTP_STATUS_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
    http_status, retryable = ERROR_CODE_TABLE[code]
    return _envelope_response(
        request=request,
        code=code,
        message=str(exc.detail) if isinstance(exc.detail, str) else "request failed",
        http_status=exc.status_code if code != ErrorCode.INTERNAL_ERROR else http_status,
        retryable=retryable,
        details={},
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """FAIL-CORE-001/002: never a silent hang, never a fabricated success —
    but also never a leaked stack trace or internal detail (SECRET-004
    spirit: "no internal stack trace ... ever appears in an error")."""

    logger.exception("unhandled exception", exc_info=exc)
    http_status, retryable = ERROR_CODE_TABLE[ErrorCode.INTERNAL_ERROR]
    return _envelope_response(
        request=request,
        code=ErrorCode.INTERNAL_ERROR,
        message="an unexpected error occurred",
        http_status=http_status,
        retryable=retryable,
        details={},
    )


async def storage_error_handler(request: Request, exc: OperationalError) -> JSONResponse:
    """A store too busy to take this request's write — SQLite is single-writer,
    and a running task holds the write lock for its request. The request's
    transaction was rolled back, so nothing it did persisted: a retryable
    `503 dependency_unavailable`, not a 500. Any other operational error is
    still an internal error."""

    if "database is locked" not in str(getattr(exc, "orig", exc)):
        return await unhandled_exception_handler(request, exc)
    logger.warning("storage busy: request rolled back")
    http_status, _ = ERROR_CODE_TABLE[ErrorCode.DEPENDENCY_UNAVAILABLE]
    return _envelope_response(
        request=request,
        code=ErrorCode.DEPENDENCY_UNAVAILABLE,
        message="the server is busy; nothing was changed — retry shortly",
        http_status=http_status,
        retryable=True,
        details={"dependency": "storage"},
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(OperationalError, storage_error_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
