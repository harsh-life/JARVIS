"""02 §5 — the HTTP surface, exercised end to end through the real gateway
app (`create_app`), the real Security Core, and the real runtime wiring.

Reuses `tests/security_core/conftest.py`'s `api`/`onboard` HTTP harness —
the same one security-core's own endpoint tests use — so identity here comes
from a real OIDC login + device registration + token issuance, exactly as it
would for any other endpoint.
"""

from __future__ import annotations

import pytest

from server.gateway.app import API_V1_PREFIX
from shared.schemas.enums import CapabilityScopeType
from tests.security_core.conftest import Api, Onboarded, api, kek_value, storage  # noqa: F401 (fixtures)

pytestmark = pytest.mark.asyncio


async def _grant_capability(api: Api, onboarded: Onboarded, capability: str, scope_id) -> None:
    resp = await api.client.post(
        f"{API_V1_PREFIX}/capabilities",
        json={"capability": capability, "scope_type": CapabilityScopeType.USER.value, "scope_id": str(scope_id)},
        headers=onboarded.auth,
    )
    assert resp.status_code == 201, resp.text


async def test_agent_task_endpoint_requires_authentication(api: Api):
    resp = await api.client.post(f"{API_V1_PREFIX}/agent/tasks", json={"input": "hello"})
    assert resp.status_code == 401


async def test_agent_task_endpoint_rejects_a_body_with_no_input(api: Api):
    onboarded = await api.onboard()
    resp = await api.client.post(f"{API_V1_PREFIX}/agent/tasks", json={}, headers=onboarded.auth)
    assert resp.status_code == 422


async def test_get_unknown_task_is_not_found(api: Api):
    onboarded = await api.onboard()
    resp = await api.client.get(f"{API_V1_PREFIX}/agent/tasks/does-not-exist", headers=onboarded.auth)
    assert resp.status_code == 404


async def test_cancel_unknown_task_is_not_found(api: Api):
    onboarded = await api.onboard()
    resp = await api.client.post(
        f"{API_V1_PREFIX}/agent/tasks/does-not-exist/cancel", headers=onboarded.auth
    )
    assert resp.status_code == 404


async def test_confirm_unknown_task_is_not_found(api: Api):
    onboarded = await api.onboard()
    resp = await api.client.post(
        f"{API_V1_PREFIX}/agent/tasks/does-not-exist/confirm",
        json={"confirmation_token": "x", "approve": True},
        headers=onboarded.auth,
    )
    assert resp.status_code == 404


async def test_a_task_that_would_need_an_unavailable_model_fails_explicitly(api: Api):
    """No Ollama server is running in this test environment — the default
    config's primary model is genuinely unreachable, so this proves
    FAIL-CORE-002/05 §5's "explicit failure, never a fabricated answer" end
    to end over real HTTP, not merely at the orchestrator's unit level."""

    onboarded = await api.onboard()
    resp = await api.client.post(
        f"{API_V1_PREFIX}/agent/tasks", json={"input": "hello agent"}, headers=onboarded.auth
    )
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["code"] == "dependency_unavailable"
