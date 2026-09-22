"""request_id propagation (02_API_PROTOCOL.md §1.3).

"Every request carries/receives an X-Request-Id (client-supplied UUID
accepted; server generates if absent). It correlates the request across
AuditEvent, PermissionDecision, and UsageEvent — the single thread for
tracing one request end to end."

[IMPL] choice, documented per this branch's instructions: the doc says a
UUID is "accepted" when client-supplied but does not lock what happens on
a malformed one. Treating request_id as a tracing/correlation header rather
than a security-relevant validation gate, a malformed client-supplied value
is replaced with a freshly generated one (never a 422) — this keeps a
client's own retry logic from being blocked over what is, by design, purely
a tracing convenience.
"""

from __future__ import annotations

import uuid
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-Id"


def _valid_or_new(candidate: str | None) -> uuid.UUID:
    if candidate:
        try:
            return uuid.UUID(candidate)
        except ValueError:
            pass
    return uuid.uuid4()


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], "Response"]
    ) -> Response:
        request_id = _valid_or_new(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = str(request_id)
        return response


def get_request_id(request: Request) -> str:
    """FastAPI dependency: the current request's correlation id, as a
    string, for handlers/logging to consume."""

    return str(request.state.request_id)
