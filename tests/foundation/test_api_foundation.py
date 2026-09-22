"""API foundation tests: versioning, request_id, error envelope, health.

02_API_PROTOCOL.md acceptance hooks this branch can honestly claim:
none of API-T1..T10 (§15) are testable yet — every one of them requires
auth/authz/agent/tool/metering machinery this branch does not implement.
What foundation *can* and does test is the protocol plumbing those hooks
will run on top of: request_id propagation, the error envelope shape, and
versioning.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel

from server.gateway.app import API_V1_PREFIX, create_app
from server.gateway.security import build_security_core
from tests.support import make_test_config
from server.gateway.errors import install_error_handlers
from server.gateway.request_context import RequestIdMiddleware
from server.storage import SQLAlchemyStorageBackend


@pytest.fixture
async def app_client(storage: SQLAlchemyStorageBackend):
    # security-core: the app now requires a security core alongside storage, so
    # the factory is given one built from a test config. The core is constructed
    # *locked* (12 §3), which is exactly the state the protocol-plumbing
    # assertions below want — none of them touches a secret.
    app = create_app(storage=storage, security=build_security_core(make_test_config()))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_health_endpoint_is_versioned_and_public(app_client: httpx.AsyncClient) -> None:
    resp = await app_client.get(f"{API_V1_PREFIX}/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"


async def test_request_id_generated_when_absent(app_client: httpx.AsyncClient) -> None:
    resp = await app_client.get(f"{API_V1_PREFIX}/health")
    request_id = resp.headers.get("x-request-id")
    assert request_id is not None
    assert uuid.UUID(request_id)  # must be a valid UUID


async def test_request_id_echoed_when_client_supplies_valid_uuid(
    app_client: httpx.AsyncClient,
) -> None:
    supplied = str(uuid.uuid4())
    resp = await app_client.get(f"{API_V1_PREFIX}/health", headers={"X-Request-Id": supplied})
    assert resp.headers.get("x-request-id") == supplied


async def test_malformed_request_id_replaced_not_rejected(app_client: httpx.AsyncClient) -> None:
    """[IMPL] choice documented in request_context.py: a malformed
    client-supplied request_id is replaced, not a validation error."""

    resp = await app_client.get(f"{API_V1_PREFIX}/health", headers={"X-Request-Id": "not-a-uuid"})
    assert resp.status_code == 200
    returned = resp.headers.get("x-request-id")
    assert returned != "not-a-uuid"
    assert uuid.UUID(returned)


async def test_unmatched_route_returns_canonical_not_found_envelope(
    app_client: httpx.AsyncClient,
) -> None:
    resp = await app_client.get(f"{API_V1_PREFIX}/this-route-does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "not_found"
    assert "request_id" in body["error"]
    assert body["error"]["retryable"] is False
    # SECRET-004 spirit: no stack trace / internal detail leaked.
    assert "Traceback" not in body["error"]["message"]


async def test_error_envelope_never_leaks_a_stack_trace_on_internal_error() -> None:
    """Exercise the shared validation/internal-error handlers directly
    against a throwaway app, to prove the *infrastructure* those handlers
    provide works — not to stand up a fake production endpoint. No route
    below is part of the real Track B API surface (see server/gateway/routers/).
    """

    class Body(BaseModel):
        required_field: str

    scratch_app = FastAPI()
    scratch_app.add_middleware(RequestIdMiddleware)
    install_error_handlers(scratch_app)

    @scratch_app.post("/test-only/validated")
    async def _validated(body: Body) -> dict:
        return {"ok": True}

    @scratch_app.get("/test-only/boom")
    async def _boom() -> dict:
        raise RuntimeError("some internal secret detail that must never leak")

    transport = httpx.ASGITransport(app=scratch_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        validation_resp = await client.post("/test-only/validated", json={})
        assert validation_resp.status_code == 422
        v_body = validation_resp.json()
        assert v_body["error"]["code"] == "validation_failed"
        assert v_body["error"]["retryable"] is False

        boom_resp = await client.get("/test-only/boom")
        assert boom_resp.status_code == 500
        b_body = boom_resp.json()
        assert b_body["error"]["code"] == "internal_error"
        assert "some internal secret detail" not in b_body["error"]["message"]
        assert "RuntimeError" not in b_body["error"]["message"]


def test_no_authentication_or_authorization_is_implemented() -> None:
    """§8/§9 of this branch's instructions: no fake auth, no authorization
    logic. This test documents that guarantee structurally: the real
    router module exposes no dependency named after auth/session/current-
    user, and no route requires a bearer token."""

    from server.gateway.routers import health

    import inspect

    source = inspect.getsource(health)
    for forbidden in ("Authorization", "Bearer", "current_user", "verify_token", "oidc"):
        assert forbidden not in source
