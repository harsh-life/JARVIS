"""Regressions for the integration-hardening findings that only show when the
layers are composed — each test fails on the pre-hardening code.

| Finding | Test |
|---|---|
| a stored model config could name `env:` — the server's own environment | `test_a_stored_config_cannot_name_the_process_environment` |
| a model key resolver could be pointed at a non-model credential | `test_a_model_config_cannot_be_pointed_at_a_non_model_credential` |
| a suspended user's in-flight task kept running | `test_a_user_suspended_mid_task_stops_at_the_next_step` |
| device operations were not bound to the authorizing device | `test_tool_invocations_carry_the_principals_own_device` |
| task temp roots were never removed | `test_a_finished_task_releases_its_temp_root` |
| a paused response's confirmation token persisted in plaintext for replay | `test_the_idempotency_replay_copy_carries_no_confirmation_token` |
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import httpx
import pytest

from server.secrets.requester import SecretRequester
from server.security.audit import AuditLogger
from server.storage.models import AgentConfiguration, IdempotencyKey, User
from shared.schemas.enums import AgentConfigScopeType, SecretClass, SecretOwnerScopeType, UserStatus
from tests.runtime.conftest import ask, call, failure_of, final, pending_of
from tests.support import TEST_KEK_ENV_VAR, TEST_PROCESS_CONFINEMENT

pytestmark = pytest.mark.asyncio

PAID = {"input_per_1k_tokens": 0.1, "output_per_1k_tokens": 0.1}


class _Wire:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": final("ok")}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })


def _paid_server_config() -> dict:
    return {
        "models_as_tools": [{"id": "priced", "provider": "openai_compatible", "model": "gpt-test",
                             "endpoint": "https://llm.test/v1", "pricing": PAID, "enabled": False}],
        "agent": {"bounds": {"per_task_budget": 5.0}},
        "security": {"budgets": {"per_user_daily_cost_limit": 50.0, "global_daily_cost_limit": 500.0}},
    }


async def _config_row(h, user_id: uuid.UUID, secret_ref: str) -> None:
    async with h.storage.session() as s:
        s.add(AgentConfiguration(
            scope_type=AgentConfigScopeType.USER, scope_id=user_id,
            primary_model={"provider": "openai_compatible", "model": "gpt-test",
                           "secret_ref": secret_ref, "timeout_seconds": 5},
            updated_at=datetime.now(timezone.utc),
        ))
        await s.commit()


# ── secrets ───────────────────────────────────────────────────────────────


async def test_a_stored_config_cannot_name_the_process_environment(make_harness):
    """`env:NAME` reads the server's environment with no requester and no audit.
    From a stored row it would ship the KEK (here, the test KEK variable) to the
    provider as a bearer token."""

    wire = _Wire()
    h = await make_harness(config=_paid_server_config(), transport=httpx.MockTransport(wire.handler))
    bob = await h.user("bob")
    kek = os.environ[TEST_KEK_ENV_VAR]
    await _config_row(h, bob.user_id, f"env:{TEST_KEK_ENV_VAR}")

    resp = await h.submit(bob)

    assert resp.status_code == 503
    assert failure_of(resp) == "model_unavailable"
    assert wire.requests == []
    assert kek not in resp.text


async def test_a_model_config_cannot_be_pointed_at_a_non_model_credential(make_harness):
    """The model-key resolver hands a secret to a provider as its API key. A
    user's own credential of another class — here an OAuth token — must never
    leave that way, even though the scope (the user's own) matches."""

    wire = _Wire()
    h = await make_harness(config=_paid_server_config(), transport=httpx.MockTransport(wire.handler))
    bob = await h.user("bob")
    oauth = f"TEST-ONLY-oauth-{uuid.uuid4().hex}"
    async with h.storage.session() as s:
        handle = await h.core.secret_store.set(
            s, owner_scope_type=SecretOwnerScopeType.USER, owner_scope_id=str(bob.user_id),
            secret_class=SecretClass.OAUTH_TOKEN, value=oauth,
            requester=SecretRequester.user(bob.user_id), audit=AuditLogger(s, request_id=uuid.uuid4()),
        )
        await s.commit()
    await _config_row(h, bob.user_id, handle)

    resp = await h.submit(bob)

    assert resp.status_code == 503
    assert wire.requests == []
    assert oauth not in resp.text


# ── identity freshness ────────────────────────────────────────────────────


async def test_a_user_suspended_mid_task_stops_at_the_next_step(h):
    """The runtime re-checks its principal before every step (SESSION-002). That
    check looked at the device and session only, so a user suspended while a
    task was running kept executing until the task's wall clock ran out."""

    from server.composition.security_port import RuntimeSecurityAdapter

    alice = await h.user("alice")
    async with h.storage.session() as s:
        principal = (await h.core.sessions.resolve_principal(s, alice.token)).principal

    async def active() -> bool:
        async with h.storage.session() as s:
            adapter = RuntimeSecurityAdapter(core=h.core, session=s, audit=AuditLogger(s, request_id=uuid.uuid4()))
            return await adapter.principal_active(principal)

    assert await active()
    async with h.storage.session() as s:
        (await s.get(User, alice.user_id)).status = UserStatus.SUSPENDED
        await s.commit()
    assert not await active()


# ── device binding ────────────────────────────────────────────────────────


async def test_tool_invocations_carry_the_principals_own_device(h):
    """A grant can be device-scoped (PRD §13's per-app grid is per phone); the
    invocation names exactly the device of the principal that was authorized."""

    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope={"package_name": "com.example"})
    h.model.push(
        ask("app.interact", scope={"package_name": "com.example"}),
        call("ui.app", "read_screen_element", args={"id": "x"}, platform="android"),
        final("read"),
    )
    resp = await h.submit(alice)

    assert resp.status_code == 200, resp.text
    assert [c.device_id for c in h.ui.calls] == [alice.device_id]


# ── resources ─────────────────────────────────────────────────────────────


async def test_a_finished_task_releases_its_temp_root(make_harness, tmp_path):
    h = await make_harness(
        config={"execution": {"filesystem": {"base_root": str(tmp_path / "sandboxes")},
                              "process": {"allowed_executables": ["sh"],
                                          "confinement_mode": TEST_PROCESS_CONFINEMENT}}},
        use_real_execution_tools=True,
    )
    alice = await h.user("alice")
    await h.grant(alice, "system.restricted")
    h.model.push(
        ask("system.restricted"),
        call("system.shell", "run_shell_command", args={"argv": ["sh", "-c", "echo scratch > f.txt"]}),
    )
    paused = pending_of(await h.submit(alice))
    h.model.push(final("done"))
    resp = await h.confirm(alice, paused["task_id"], paused["confirmation_token"])

    assert resp.status_code == 200, resp.text
    assert not (tmp_path / "sandboxes" / "tasks" / paused["task_id"]).exists()


# ── credentials at rest ───────────────────────────────────────────────────


async def test_the_idempotency_replay_copy_carries_no_confirmation_token(h):
    """Only the confirmation token's hash is persisted (ConfirmationToken). The
    response the human sees carries the token once; the stored replay copy does
    not, and a retry recovers it through the owner-only task read."""

    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope={"package_name": "com.example"})
    h.model.push(
        ask("app.interact", scope={"package_name": "com.example"}),
        call("ui.app", "input_text", args={"text": "hello"}, platform="android"),
    )
    first = await h.submit(alice, key="submit-1")
    token = pending_of(first)["confirmation_token"]
    assert token

    stored = json.dumps([row.response_body for row in await h.rows(IdempotencyKey)])
    assert token not in stored

    replay = pending_of(await h.submit(alice, key="submit-1"))
    assert replay["confirmation_token"] is None
    assert replay["task_id"] == pending_of(first)["task_id"]

    fetched = await h.get(alice, replay["task_id"])
    assert fetched.json()["pending"]["confirmation_token"] == token
