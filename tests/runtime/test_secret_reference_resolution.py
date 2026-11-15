"""`secretstore:` references from configuration resolve: model, voice, console.

A SecretStore handle carries its own `secretstore:` prefix (`new_handle`), and
`SecretRef` admits exactly `secretstore:<id>` — so a configured reference *is*
the handle. `key_provider_for` and the console's `MetadataResolvability` used to
strip that prefix and look up a bare id that never exists: every valid
config-file `secretstore:` reference for the agent model, models-as-tools, the
Judge, voice and vision failed, and the console reported it unresolvable. The
existing tests passed only because they assigned a doubled
`secretstore:secretstore:<id>` straight onto the config object, which config
validation refuses.

Here every reference goes through `AppConfig` validation, every handle is one
`SecretStore.set` returned, and the production resolvers decide. The stored
AgentConfiguration-row form (one extra prefix, by design —
`server/gateway/routers/model_config.py:_row_ref`) keeps working, and the class,
ownership and revocation checks still refuse everything else.

`set` mints a random handle, while config must name one before the app is
built, so `new_handle` is pinned to a pre-chosen, correctly formed handle for
that one `set` call; the store, the resolvers and the checks are all real.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import delete

from server.composition.console import MetadataResolvability
from server.composition.secret_context import CURRENT_SECRET_RESOLVER, SecretUnavailable, key_provider_for
from server.config.schema import AppConfig
from server.secrets import store as store_module
from server.secrets.requester import SecretRequester
from server.security.audit import AuditLogger
from server.storage.models import AgentConfiguration, AuditEvent
from shared.schemas.enums import AgentConfigScopeType, SecretClass, SecretOwnerScopeType
from tests.runtime.conftest import base_config_payload, failure_of, final
from tests.runtime.test_secrets_and_providers import WireLog, _paid_primary
from tests.voice.conftest import KEY_VALUE as VOICE_ENV_KEY_VALUE
from tests.voice.conftest import provider, server_voice  # noqa: F401 — `provider` is a fixture
from tests.voice.test_voice_config_and_api import VOICE, WAV, audio_headers



def a_handle() -> str:
    """A correctly formed handle, as `new_handle` mints them."""

    return f"{store_module.HANDLE_PREFIX}{uuid.uuid4().hex}"


def a_key() -> str:
    return f"TEST-ONLY-key-{uuid.uuid4().hex}"


async def store_at(h, monkeypatch, handle: str, value: str, *, cls=SecretClass.MODEL_API_KEY,
                   scope=SecretOwnerScopeType.SERVER, owner: str | None = None) -> str:
    with monkeypatch.context() as pinned:
        pinned.setattr(store_module, "new_handle", lambda: handle)
        async with h.storage.session() as s:
            returned = await h.core.secret_store.set(
                s, owner_scope_type=scope, owner_scope_id=owner, secret_class=cls, value=value,
                requester=SecretRequester.server(), audit=AuditLogger(s, request_id=uuid.uuid4()))
            await s.commit()
    assert returned == handle
    return returned


async def revoke(h, handle: str) -> None:
    async with h.storage.session() as s:
        await h.core.secret_store.delete(s, handle, SecretRequester.server(), AuditLogger(s, request_id=uuid.uuid4()))
        await s.commit()


def agent_config(secret_ref: str) -> dict:
    config = _paid_primary()
    config["agent"]["secret_ref"] = secret_ref
    return config


# ── the reference contract ──────────────────────────────────────────────────


def test_config_admits_the_handle_and_refuses_the_doubled_form():
    handle = a_handle()
    payload = base_config_payload()
    payload["agent"] = {**payload.get("agent", {}), **agent_config(handle)["agent"]}
    assert AppConfig.model_validate(payload).agent.secret_ref == handle
    payload["agent"]["secret_ref"] = f"secretstore:{handle}"
    with pytest.raises(ValidationError):
        AppConfig.model_validate(payload)


async def test_key_provider_for_hands_the_resolver_exactly_the_stores_handle():
    handle = a_handle()
    seen: list[str] = []

    async def resolver(h: str, requester: SecretRequester) -> str:
        seen.append(h)
        return "k"

    token = CURRENT_SECRET_RESOLVER.set(resolver)
    try:
        await key_provider_for(handle, SecretRequester.server())()                       # config form
        await key_provider_for(f"secretstore:{handle}", SecretRequester.server())()      # stored-row form
        await key_provider_for(f"secretstore:secretstore:{handle}", SecretRequester.server())()  # malformed
    finally:
        CURRENT_SECRET_RESOLVER.reset(token)
    # Exactly one prefix is ever removed: the malformed triple form is not
    # "repaired" into a valid handle — it fails at the store.
    assert seen == [handle, handle, f"secretstore:{handle}"]


async def test_an_env_reference_still_reads_the_environment(monkeypatch):
    monkeypatch.setenv("JARVIS_TEST_MODEL_KEY", "env-key")
    assert await key_provider_for("env:JARVIS_TEST_MODEL_KEY", SecretRequester.server())() == "env-key"
    monkeypatch.delenv("JARVIS_TEST_MODEL_KEY")
    with pytest.raises(SecretUnavailable):
        await key_provider_for("env:JARVIS_TEST_MODEL_KEY", SecretRequester.server())()


# ── the agent model, from a validated config ────────────────────────────────


async def test_a_configured_handle_reaches_the_provider(make_harness, monkeypatch):
    handle, key = a_handle(), a_key()
    wire = WireLog(final("answered"))
    h = await make_harness(config=agent_config(handle), transport=wire.transport)
    assert h.config.agent.secret_ref == handle
    await store_at(h, monkeypatch, handle, key)
    resp = await h.submit(await h.user("alice"), "two plus two")
    assert resp.status_code == 200, resp.text
    assert wire.requests[0].headers["authorization"] == f"Bearer {key}"
    assert key not in resp.text


@pytest.mark.parametrize("case", ["unknown", "revoked", "oauth_token", "device_credential", "user_owned"])
async def test_a_configured_handle_to_anything_else_fails_closed(make_harness, monkeypatch, case):
    handle = a_handle()
    wire = WireLog()
    h = await make_harness(config=agent_config(handle), transport=wire.transport)
    alice = await h.user("alice")
    if case == "revoked":
        await store_at(h, monkeypatch, handle, a_key())
        await revoke(h, handle)
    elif case in ("oauth_token", "device_credential"):
        await store_at(h, monkeypatch, handle, a_key(), cls=SecretClass(case))
    elif case == "user_owned":
        await store_at(h, monkeypatch, handle, a_key(), scope=SecretOwnerScopeType.USER, owner=str(alice.user_id))
    resp = await h.submit(alice)
    assert resp.status_code == 503, (case, resp.text)
    assert failure_of(resp) == "model_unavailable"
    assert wire.requests == []
    if case in ("oauth_token", "device_credential"):
        blocked = await h.rows(AuditEvent, AuditEvent.action == "secret.get")
        assert any(r.resource == handle and r.result.value == "blocked" for r in blocked)


async def test_the_stored_row_form_still_resolves_and_a_malformed_row_fails(make_harness, monkeypatch):
    """An AgentConfiguration row carries `secretstore:` + the handle by design;
    one with any other shape resolves nothing."""

    wire = WireLog(final("from the row"))
    budgets = _paid_primary()
    h = await make_harness(
        config={"models_as_tools": [{"id": "priced", "provider": "openai_compatible", "model": "gpt-test",
                                     "endpoint": "https://llm.test/v1", "pricing": budgets["agent"]["pricing"],
                                     "enabled": False}],
                "agent": {"bounds": budgets["agent"]["bounds"]}, "security": budgets["security"]},
        transport=wire.transport,
    )
    alice = await h.user("alice")
    handle, key = a_handle(), a_key()
    await store_at(h, monkeypatch, handle, key, scope=SecretOwnerScopeType.USER, owner=str(alice.user_id))

    async def set_row(secret_ref: str) -> None:
        async with h.storage.session() as s:
            await s.execute(delete(AgentConfiguration).where(AgentConfiguration.scope_id == alice.user_id))
            s.add(AgentConfiguration(
                scope_type=AgentConfigScopeType.USER, scope_id=alice.user_id,
                primary_model={"provider": "openai_compatible", "model": "gpt-test",
                               "endpoint": "https://llm.test/v1", "secret_ref": secret_ref, "timeout_seconds": 5},
                updated_at=datetime.now(timezone.utc)))
            await s.commit()

    await set_row(f"secretstore:{handle}")                      # what model_config._row_ref writes
    resp = await h.submit(alice, "hello")
    assert resp.status_code == 200, resp.text
    assert wire.requests[-1].headers["authorization"] == f"Bearer {key}"

    sent = len(wire.requests)
    await set_row(f"secretstore:secretstore:{handle}")          # malformed: never "repaired"
    resp = await h.submit(alice, "hello again")
    assert resp.status_code == 503 and failure_of(resp) == "model_unavailable"
    assert len(wire.requests) == sent


# ── voice, from a validated config ──────────────────────────────────────────


async def test_a_configured_handle_reaches_the_voice_provider(make_harness, provider, monkeypatch):
    handle, key = a_handle(), a_key()
    h = await make_harness(config=server_voice(provider={"secret_ref": handle}), transport=provider.transport)
    await store_at(h, monkeypatch, handle, key)
    alice = await h.user("alice")
    resp = await h.client.post(f"{VOICE}/transcribe", params={"language": "en"}, content=WAV,
                               headers=audio_headers(alice))
    assert resp.status_code == 200, resp.text
    [request] = provider.requests
    assert request.headers["authorization"] == f"Bearer {key}"
    assert key != VOICE_ENV_KEY_VALUE


@pytest.mark.parametrize("case", ["unknown", "oauth_token", "revoked"])
async def test_a_voice_handle_to_anything_else_reaches_no_provider(make_harness, provider, monkeypatch, case):
    handle = a_handle()
    h = await make_harness(config=server_voice(provider={"secret_ref": handle}), transport=provider.transport)
    if case == "oauth_token":
        await store_at(h, monkeypatch, handle, a_key(), cls=SecretClass.OAUTH_TOKEN)
    elif case == "revoked":
        await store_at(h, monkeypatch, handle, a_key())
        await revoke(h, handle)
    alice = await h.user("alice")
    resp = await h.client.post(f"{VOICE}/transcribe", params={"language": "en"}, content=WAV,
                               headers=audio_headers(alice))
    assert resp.status_code >= 400, (case, resp.text)
    assert provider.requests == []


# ── the console's reference status ──────────────────────────────────────────


async def test_the_console_reports_a_configured_handle_as_resolvable(make_harness, monkeypatch):
    h = await make_harness()
    status = MetadataResolvability(h.storage)
    handle = a_handle()
    assert await status.resolves(handle) is False                       # not stored yet
    await store_at(h, monkeypatch, handle, a_key())
    assert await status.resolves(handle) is True                        # the config form
    assert await status.resolves(f"secretstore:{handle}") is True       # the stored-row form
    assert await status.resolves(f"secretstore:secretstore:{handle}") is False
    assert await status.resolves(a_handle()) is False                   # unknown
    await revoke(h, handle)
    assert await status.resolves(handle) is False                       # revoked
    monkeypatch.setenv("JARVIS_TEST_CONSOLE_KEY", "x")
    assert await status.resolves("env:JARVIS_TEST_CONSOLE_KEY") is True
    monkeypatch.delenv("JARVIS_TEST_CONSOLE_KEY")
    assert await status.resolves("env:JARVIS_TEST_CONSOLE_KEY") is False
