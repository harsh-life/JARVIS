"""Canonical error envelope + error code registry.

Source: 02_API_PROTOCOL.md §1.6/§1.7. This is a shared contract (not
gateway-specific) because the eventual Android client and any other caller
need the exact same shape and code registry the server emits — it belongs
at the shared/contracts layer, consumed downward by server/gateway, per
this branch's module-boundary diagram (§4 of the task).

Do NOT casually invent new error codes here (§9 of this branch's
instructions). The table below is transcribed verbatim from 02 §1.7.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode(str, Enum):
    """02_API_PROTOCOL.md §1.7 — canonical error codes. Adding a value here
    that is not in that table is an architecture change, not a bugfix."""

    UNAUTHENTICATED = "unauthenticated"
    TOKEN_EXPIRED = "token_expired"
    UNAUTHORIZED = "unauthorized"
    CONFIRMATION_REQUIRED = "confirmation_required"
    PROHIBITED = "prohibited"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    VALIDATION_FAILED = "validation_failed"
    RATE_LIMITED = "rate_limited"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    INTERNAL_ERROR = "internal_error"


# (http_status, retryable-by-default) per 02 §1.7. `retryable` is a default;
# a specific raise may still override it (e.g. dependency_unavailable is
# usually retryable but a specific instance could reasonably say otherwise).
ERROR_CODE_TABLE: dict[ErrorCode, tuple[int, bool]] = {
    ErrorCode.UNAUTHENTICATED: (401, False),
    ErrorCode.TOKEN_EXPIRED: (401, False),
    ErrorCode.UNAUTHORIZED: (403, False),
    ErrorCode.CONFIRMATION_REQUIRED: (403, False),
    ErrorCode.PROHIBITED: (403, False),
    ErrorCode.NOT_FOUND: (404, False),
    ErrorCode.CONFLICT: (409, False),
    ErrorCode.VALIDATION_FAILED: (422, False),
    ErrorCode.RATE_LIMITED: (429, True),
    ErrorCode.DEPENDENCY_UNAVAILABLE: (503, True),
    ErrorCode.INTERNAL_ERROR: (500, False),
}


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    request_id: str
    retryable: bool
    details: dict = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    """02_API_PROTOCOL.md §1.6 — the uniform error body every endpoint
    returns on failure."""

    model_config = ConfigDict(extra="forbid")

    error: ErrorDetail
