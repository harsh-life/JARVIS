"""The internal Model Gateway listener's one route (docs/29 §12.2, Phase 6 slice 6A).

`POST /v1/chat/completions`, OpenAI-compatible, for an external agent
runtime. This app is **separate** from the public API: it is built by
`build_model_gateway_app` and served only on the internal binding
`agents.model_gateway.listen` (`server/composition/model_gateway.py`); it is
never included in `create_app`. It has no docs, no schema, no other route.

The body is read up to `max_request_bytes` and no further — a larger one is
refused unread — then handed, with the `Authorization` header, to the
`ModelGatewayPort`, which authenticates first and decides everything else.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from server.gateway.model_gateway_port import CLIENT_CLOSED, ModelGatewayPort

COMPLETIONS_PATH = "/v1/chat/completions"


def _too_large() -> JSONResponse:
    return JSONResponse(status_code=413, content={"error": {"code": "schema_invalid",
                                                            "message": "the request body is too large"}})


async def _bounded_body(request: Request, limit: int) -> bytes | None:
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        return None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def build_model_gateway_app(port: ModelGatewayPort, *, max_request_bytes: int,
                            run_id: "uuid.UUID | None" = None) -> FastAPI:
    """`run_id`: a per-run listener (an external run's own socket) serves that
    run's token only — another run's is refused even if valid."""

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post(COMPLETIONS_PATH, include_in_schema=False)
    async def chat_completions(request: Request) -> Response:
        body = await _bounded_body(request, max_request_bytes)
        if body is None:
            return _too_large()
        reply = await port.chat_completion(authorization=request.headers.get("authorization"), body=body,
                                           disconnected=request.is_disconnected, run_id=run_id)
        if reply.status == CLIENT_CLOSED:
            return Response(status_code=CLIENT_CLOSED)
        return JSONResponse(status_code=reply.status, content=dict(reply.payload),
                            headers={"Cache-Control": "no-store"})

    return app


__all__ = ["COMPLETIONS_PATH", "build_model_gateway_app"]
