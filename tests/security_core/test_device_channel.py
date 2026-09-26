"""`WS /api/v1/devices/channel` — the authenticated device channel (docs/23 §4).

Driven in-process through the real endpoint, real token/proof verification
and the real hub. What is pinned: a socket is bound to exactly the device its
own verified credentials name; nothing a client sends can re-bind it; and
revocation, logout, rotation and token expiry all end it.
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import update

from server.auth.device import build_device_proof
from server.config.schema import AndroidConfig
from server.execution.android import MAPPING_VERSION, build_operation
from server.execution.device_hub import DeviceHub
from server.gateway.app import API_V1_PREFIX, create_app
from server.gateway.routers import device_channel as channel_module
from server.gateway.security import build_security_core
from server.secrets.kek import resolve_kek
from server.storage.models import AccessToken
from shared.schemas.execution import ExecutionError, ExecutionErrorCode
from tests.device_channel_support import AsgiWebSocket
from tests.security_core.conftest import TEST_KEK_ENV_VAR, Api, LocalOIDCProvider, make_test_config
from tests.security_core.helpers import audit_actions, database_contains


class Channel:
    def __init__(self, api: Api, app, hub: DeviceHub) -> None:
        self.api = api
        self.app = app
        self.hub = hub

    def socket(self) -> AsgiWebSocket:
        return AsgiWebSocket(self.app)

    async def hello(self, device, *, token=None, proof_device=None, mapping=MAPPING_VERSION) -> AsgiWebSocket:
        ws = self.socket()
        await ws.connect()
        signer = proof_device or device
        await ws.send_json({
            "type": "hello",
            "access_token": token or device.access_token,
            "device_proof": build_device_proof(device_id=signer.device_id, device_credential=signer.credential),
            "mapping_version": mapping,
            "client_version": "test",
        })
        return ws


@pytest_asyncio.fixture
async def channel(storage, kek_value):
    provider = LocalOIDCProvider()
    core = build_security_core(make_test_config(), oidc_provider=provider)
    async with storage.session() as session:
        await core.secret_store.bootstrap(session, resolve_kek(f"env:{TEST_KEK_ENV_VAR}"))
        await session.commit()
    hub = DeviceHub()
    app = create_app(storage=storage, security=core, android_config=AndroidConfig(enabled=True), device_hub=hub)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield Channel(Api(client=client, provider=provider, core=core, storage=storage), app, hub)


async def _connected(channel, device):
    ws = await channel.hello(device)
    ack = await ws.receive_json()
    assert ack["type"] == "hello_ok", ack
    return ws, ack


# ── binding ─────────────────────────────────────────────────────────────


async def test_a_verified_device_is_bound_to_exactly_itself(channel):
    phone = await channel.api.onboard("alice")
    ws, ack = await _connected(channel, phone)
    assert ack["device_id"] == str(phone.device_id)
    assert ack["mapping_version"] == MAPPING_VERSION
    assert await channel.hub.is_connected(device_id=phone.device_id)
    await ws.disconnect()
    assert not await channel.hub.is_connected(device_id=phone.device_id)


async def test_a_proof_from_another_device_is_refused(channel):
    """Two devices of the same user: device B's proof on device A's token
    must not bind the socket to either."""

    first = await channel.api.onboard("alice")
    bootstrap = await channel.api.oidc_login()
    second_id, second_cred = await channel.api.register_device(bootstrap)

    class Other:
        device_id = second_id
        credential = second_cred

    ws = await channel.hello(first, proof_device=Other)
    assert await ws.expect_close() == 4001
    assert not await channel.hub.is_connected(device_id=first.device_id)
    assert not await channel.hub.is_connected(device_id=second_id)


async def test_another_users_token_cannot_carry_this_device(channel):
    alice = await channel.api.onboard("alice")
    bob = await channel.api.onboard("bob")
    ws = await channel.hello(alice, token=bob.access_token)
    assert await ws.expect_close() == 4001


async def test_a_bad_token_is_refused(channel):
    phone = await channel.api.onboard("alice")
    ws = await channel.hello(phone, token="not-a-token")
    assert await ws.expect_close() == 4001


async def test_an_expired_token_asks_the_device_to_re_authenticate(channel):
    phone = await channel.api.onboard("alice")
    async with channel.api.storage.session() as db:
        await db.execute(update(AccessToken).values(expires_at=AccessToken.issued_at))
        await db.commit()
    ws = await channel.hello(phone)
    assert await ws.expect_close() == 4002


async def test_a_replayed_proof_is_refused(channel):
    phone = await channel.api.onboard("alice")
    proof = build_device_proof(device_id=phone.device_id, device_credential=phone.credential)
    frame = {"type": "hello", "access_token": phone.access_token, "device_proof": proof,
             "mapping_version": MAPPING_VERSION, "client_version": "t"}
    first = channel.socket()
    await first.connect()
    await first.send_json(frame)
    assert (await first.receive_json())["type"] == "hello_ok"
    replay = channel.socket()
    await replay.connect()
    await replay.send_json(frame)
    assert await replay.expect_close() == 4001


async def test_the_first_frame_must_be_a_hello(channel):
    ws = channel.socket()
    await ws.connect()
    await ws.send_json({"type": "result", "op_id": str(uuid.uuid4()), "status": "ok", "result": {}})
    assert await ws.expect_close() == 4008


async def test_a_silent_client_is_closed(channel, monkeypatch):
    monkeypatch.setattr(channel_module, "HELLO_TIMEOUT_SECONDS", 0.1)
    ws = channel.socket()
    await ws.connect()
    assert await ws.expect_close() == 4001


async def test_a_client_on_another_mapping_version_is_refused(channel):
    """ANDC-T4 at the channel: an out-of-date client cannot even connect."""

    phone = await channel.api.onboard("alice")
    ws = await channel.hello(phone, mapping="1-0000000000000000")
    assert await ws.expect_close() == 4004


async def test_the_channel_is_closed_when_disabled(storage, kek_value):
    core = build_security_core(make_test_config(), oidc_provider=LocalOIDCProvider())
    app = create_app(storage=storage, security=core)
    ws = AsgiWebSocket(app)
    await ws.connect()
    assert await ws.expect_close() == 4009


# ── revocation, logout, rotation, supersede ─────────────────────────────


async def test_revocation_closes_the_socket_and_fails_work_in_flight(channel):
    phone = await channel.api.onboard("alice")
    ws, _ = await _connected(channel, phone)
    op = build_operation(capability="device.read", operation="read_battery", package_name=None,
                         arguments={}, user_id=await _user_of(channel, phone), task_id=uuid.uuid4(),
                         device_id=phone.device_id)
    sending = asyncio.ensure_future(channel.hub.send(op))
    frame = await ws.receive_json()
    assert frame["op_id"] == str(op.op_id)

    resp = await channel.api.client.delete(f"{API_V1_PREFIX}/devices/{phone.device_id}", headers=phone.auth)
    assert resp.status_code == 204
    assert await ws.expect_close() == 4003
    with pytest.raises(ExecutionError) as exc:
        await sending
    assert exc.value.code is ExecutionErrorCode.DEVICE_UNAVAILABLE

    # Reconnecting after revocation is impossible.
    again = await channel.hello(phone)
    assert await again.expect_close() == 4001


async def test_logout_ends_the_socket(channel):
    phone = await channel.api.onboard("alice")
    ws, _ = await _connected(channel, phone)
    resp = await channel.api.client.post(f"{API_V1_PREFIX}/sessions/logout", headers=phone.auth)
    assert resp.status_code == 204
    assert await ws.expect_close() == 4002


async def test_a_second_socket_for_the_same_device_supersedes_the_first(channel):
    phone = await channel.api.onboard("alice")
    first, _ = await _connected(channel, phone)
    second, _ = await _connected(channel, phone)
    assert await first.expect_close() == 4005
    assert await channel.hub.is_connected(device_id=phone.device_id)
    await second.disconnect()


# ── reauth, revalidation, expiry ────────────────────────────────────────


async def test_reauth_extends_the_same_binding(channel):
    phone = await channel.api.onboard("alice")
    ws, _ = await _connected(channel, phone)
    fresh = (await channel.api.issue_token(phone.device_id, phone.credential)).json()["access_token"]
    await ws.send_json({"type": "reauth", "access_token": fresh})
    assert (await ws.receive_json())["type"] == "reauth_ok"
    await ws.disconnect()


async def test_reauth_cannot_rebind_to_another_device(channel):
    alice = await channel.api.onboard("alice")
    bob = await channel.api.onboard("bob")
    ws, _ = await _connected(channel, alice)
    await ws.send_json({"type": "reauth", "access_token": bob.access_token})
    assert await ws.expect_close() == 4001
    assert not await channel.hub.is_connected(device_id=alice.device_id)


async def test_a_revoked_token_is_noticed_without_any_http_call(channel, monkeypatch):
    monkeypatch.setattr(channel_module, "REVALIDATE_SECONDS", 0.2)
    phone = await channel.api.onboard("alice")
    ws, _ = await _connected(channel, phone)
    from datetime import datetime, timezone

    async with channel.api.storage.session() as db:
        await db.execute(update(AccessToken).values(revoked_at=datetime.now(timezone.utc)))
        await db.commit()
    assert await ws.expect_close() == 4002


async def test_an_unknown_frame_type_is_a_protocol_error(channel):
    phone = await channel.api.onboard("alice")
    ws, _ = await _connected(channel, phone)
    await ws.send_json({"type": "grant", "capability": "system.restricted"})
    assert await ws.expect_close() == 4008


# ── audit, and no credential in it ──────────────────────────────────────


async def test_the_channel_is_audited_and_no_credential_is_stored(channel):
    phone = await channel.api.onboard("alice")
    ws, _ = await _connected(channel, phone)
    await ws.disconnect()
    async with channel.api.storage.session() as db:
        actions = await audit_actions(db)
        assert "device.channel.opened" in actions
        assert "device.channel.closed" in actions
        assert not await database_contains(db, phone.access_token)
        assert not await database_contains(db, phone.credential)


async def _user_of(channel, onboarded) -> uuid.UUID:
    async with channel.api.storage.session() as db:
        device = await channel.api.core.auth_repository.get_device(db, onboarded.device_id)
        return device.user_id


async def test_rotating_the_credential_ends_the_socket_it_authenticated(channel):
    phone = await channel.api.onboard("alice")
    ws, _ = await _connected(channel, phone)
    resp = await channel.api.client.post(
        f"{API_V1_PREFIX}/devices/{phone.device_id}/rotate", headers=phone.auth
    )
    assert resp.status_code == 200, resp.text
    assert await ws.expect_close() == 4002


# ── composition: one hub, only when enabled ─────────────────────────────


@pytest.mark.parametrize("enabled", [True, False])
def test_the_hub_exists_only_when_enabled_and_is_the_tools_transport(storage, enabled):
    from server.composition import build_application
    from server.execution.android import UnavailableDeviceTransport
    from shared.schemas.agent import ExecutionPlatform

    config = make_test_config(android={"enabled": enabled})
    app = build_application(config, storage=storage)
    hub = app.state.device_hub
    registry = app.state.agent_tasks._tools
    transports = {
        tool_id: registry.adapter(tool_id, ExecutionPlatform.ANDROID)._transport
        for tool_id in ("device.read", "device.app_interact")
    }
    if enabled:
        assert isinstance(hub, DeviceHub)
        assert all(t is hub for t in transports.values())
    else:
        assert hub is None
        assert all(isinstance(t, UnavailableDeviceTransport) for t in transports.values())


async def test_revocation_is_committed_before_the_socket_is_closed(channel):
    """No window in which a reconnect could still authenticate against the
    pre-revocation state: by the time the hub is told to disconnect, another
    database session already sees the device revoked."""

    phone = await channel.api.onboard("alice")
    seen: list[bool] = []
    original = channel.hub.disconnect

    async def observing_disconnect(device_id, code, reason):
        async with channel.api.storage.session() as db:
            device = await channel.api.core.auth_repository.get_device(db, device_id)
            seen.append(device.revoked)
        return await original(device_id, code, reason)

    channel.hub.disconnect = observing_disconnect
    resp = await channel.api.client.delete(f"{API_V1_PREFIX}/devices/{phone.device_id}", headers=phone.auth)
    assert resp.status_code == 204
    assert seen == [True]
