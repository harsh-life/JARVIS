"""Push wake (docs/23 §4) — ANDC-T9 and the device-token binding.

What is pinned:

* **ANDC-T9, serialization.** The only FCM body the server can build is
  `{"message": {"token", "data": {"type": "wake"}, "android": {...fixed}}}`:
  a data message with no `notification`, and no key or value beyond the
  registration token and fixed routing constants.
* **ANDC-T9, construction.** What `FcmWakeSender` actually puts on the wire —
  captured at the HTTP transport — is exactly that body; the OAuth assertion
  goes only to Google's fixed token endpoint.
* **Binding.** A token is bound by the authenticated device to itself only;
  one token wakes one device; another user's token cannot be taken over; a
  revoked device has none and is never woken.
* **Off by default.** With `provider: none` nothing is registered, nothing is
  built, nothing is sent.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from urllib.parse import parse_qs

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import select

from server.config.schema import AndroidConfig, AndroidPushConfig, AppConfig
from server.execution.android import build_operation
from server.execution.device_hub import DEVICE_CHANNEL, DeviceHub
from server.execution.device_wake import (
    FCM_SCOPE,
    GOOGLE_TOKEN_URL,
    DeviceWaker,
    FcmWakeSender,
    WakeOutcome,
)
from server.gateway.app import API_V1_PREFIX, create_app
from server.gateway.security import build_security_core
from server.secrets.kek import resolve_kek
from server.storage.models import AuditEvent, Device
from shared.schemas.execution import ExecutionError, ExecutionErrorCode
from shared.schemas.push import (
    WAKE_DATA,
    FcmClientOptions,
    PushClientConfig,
    PushTokenRegistration,
    WakeData,
    fcm_wake_message,
)
from tests.security_core.conftest import TEST_KEK_ENV_VAR, Api, LocalOIDCProvider, make_test_config

TOKEN = "fcm-registration-token-" + "A1b2C3d4" * 16
OTHER_TOKEN = "fcm-registration-token-" + "Z9y8X7w6" * 16
CLIENT = FcmClientOptions(
    project_id="jarvis-test-project", application_id="1:123456789012:android:0123456789abcdef",
    api_key="test-client-key-not-a-secret-000", sender_id="123456789012",
)
PUSH_ON = AndroidPushConfig.model_validate(
    {"provider": "fcm", "fcm": {"client": CLIENT.model_dump(), "service_account_ref": "env:JARVIS_TEST_FCM_SA"}}
)
PUSH_TOKEN = f"{API_V1_PREFIX}/devices/me/push-token"
PUSH_CONFIG = f"{API_V1_PREFIX}/devices/push-config"


def _strings(value) -> list[str]:
    """Every key and string value in a JSON structure."""

    if isinstance(value, dict):
        return [s for k, v in value.items() for s in [k, *_strings(v)]]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return [value] if isinstance(value, str) else []


# ── ANDC-T9: serialization ──────────────────────────────────────────────


def test_andc_t9_the_wake_data_is_exactly_type_wake():
    assert WakeData().model_dump() == {"type": "wake"} == WAKE_DATA
    assert json.loads(WakeData().model_dump_json()) == {"type": "wake"}
    with pytest.raises(ValueError):
        WakeData.model_validate({"type": "wake", "task_id": "x"})
    with pytest.raises(ValueError):
        WakeData.model_validate({"type": "run"})


def test_andc_t9_the_fcm_message_carries_nothing_but_the_token_and_fixed_routing():
    message = fcm_wake_message(TOKEN)
    assert message == {
        "message": {
            "token": TOKEN,
            "data": {"type": "wake"},
            "android": {"priority": "HIGH", "ttl": "600s", "collapse_key": "jarvis_wake"},
        }
    }
    # A data message only: the system never renders a notification from it.
    assert "notification" not in message["message"]
    assert "notification" not in message["message"]["android"]
    # Every string in it is a key, the token, or a fixed constant.
    fixed = {"message", "token", "data", "type", "wake", "android", "priority", "HIGH", "ttl", "600s",
             "collapse_key", "jarvis_wake"}
    assert set(_strings(message)) == fixed | {TOKEN}
    # The same bytes for every device, user and task — only the token differs.
    assert fcm_wake_message(OTHER_TOKEN)["message"] | {"token": TOKEN} == message["message"]


def test_the_registration_body_names_no_device_user_or_graph():
    assert set(PushTokenRegistration.model_fields) == {"provider", "token"}
    with pytest.raises(ValueError):
        PushTokenRegistration.model_validate({"provider": "fcm", "token": TOKEN, "device_id": str(uuid.uuid4())})
    for bad in ["short", "has space " * 5, "x" * 5000, TOKEN + "\n", '{"type":"wake"}' * 3]:
        with pytest.raises(ValueError):
            PushTokenRegistration.model_validate({"provider": "fcm", "token": bad})


# ── ANDC-T9: construction (what goes on the wire) ───────────────────────


def _service_account(**overrides) -> tuple[str, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    info = {"type": "service_account", "project_id": "jarvis-test-project", "private_key": pem,
            "client_email": "wake@jarvis-test-project.iam.gserviceaccount.com", "token_uri": GOOGLE_TOKEN_URL}
    info.update(overrides)
    return json.dumps(info), key


class Google:
    """Google's token endpoint and FCM, at the HTTP transport."""

    def __init__(self, *, send_status: int = 200, send_body: dict | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.send_status = send_status
        self.send_body = send_body or {"name": "projects/jarvis-test-project/messages/1"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == GOOGLE_TOKEN_URL:
            return httpx.Response(200, json={"access_token": "ya29.test-access", "expires_in": 3600})
        return httpx.Response(self.send_status, json=self.send_body)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def sends(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "fcm.googleapis.com"]


def _b64d(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


async def test_andc_t9_the_sender_puts_exactly_the_wake_on_the_wire():
    raw, key = _service_account()
    google = Google()

    async def credential() -> str:
        return raw

    sender = FcmWakeSender(project_id="jarvis-test-project", credential=credential, transport=google.transport())
    assert await sender.send(TOKEN) is WakeOutcome.SENT

    [exchange, send] = google.requests
    # The assertion goes to Google's fixed token endpoint, and is only that.
    assert str(exchange.url) == GOOGLE_TOKEN_URL
    form = parse_qs(exchange.content.decode())
    assert set(form) == {"grant_type", "assertion"}
    header, claims, signature = form["assertion"][0].split(".")
    key.public_key().verify(_b64d(signature), f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    claims = json.loads(_b64d(claims))
    assert claims["scope"] == FCM_SCOPE and claims["aud"] == GOOGLE_TOKEN_URL

    assert str(send.url) == "https://fcm.googleapis.com/v1/projects/jarvis-test-project/messages:send"
    assert send.headers["authorization"] == "Bearer ya29.test-access"
    assert json.loads(send.content) == fcm_wake_message(TOKEN)

    # The access token is reused, not re-minted per wake.
    assert await sender.send(OTHER_TOKEN) is WakeOutcome.SENT
    assert len([r for r in google.requests if str(r.url) == GOOGLE_TOKEN_URL]) == 1


async def test_a_service_account_naming_another_token_endpoint_is_never_followed():
    raw, _ = _service_account(token_uri="https://attacker.example/token")
    google = Google()

    async def credential() -> str:
        return raw

    sender = FcmWakeSender(project_id="jarvis-test-project", credential=credential, transport=google.transport())
    assert await sender.send(TOKEN) is WakeOutcome.FAILED
    assert google.requests == []  # the signed assertion went nowhere


@pytest.mark.parametrize("status,body,expected", [
    (404, {"error": {"status": "NOT_FOUND", "details": [{"errorCode": "UNREGISTERED"}]}}, WakeOutcome.TOKEN_INVALID),
    (400, {"error": {"status": "INVALID_ARGUMENT"}}, WakeOutcome.TOKEN_INVALID),
    (500, {"error": {"status": "INTERNAL"}}, WakeOutcome.FAILED),
    (429, {"error": {"status": "RESOURCE_EXHAUSTED"}}, WakeOutcome.FAILED),
])
async def test_provider_answers_are_classified(status, body, expected):
    raw, _ = _service_account()

    async def credential() -> str:
        return raw

    google = Google(send_status=status, send_body=body)
    sender = FcmWakeSender(project_id="jarvis-test-project", credential=credential, transport=google.transport())
    assert await sender.send(TOKEN) is expected


async def test_a_missing_credential_fails_the_wake_without_a_request():
    async def credential() -> str:
        raise RuntimeError("locked")

    google = Google()
    sender = FcmWakeSender(project_id="jarvis-test-project", credential=credential, transport=google.transport())
    assert await sender.send(TOKEN) is WakeOutcome.FAILED
    assert google.requests == []


# ── the device-token binding, through the real endpoints ────────────────


@pytest_asyncio.fixture
async def push_api(storage, kek_value):
    provider = LocalOIDCProvider()
    core = build_security_core(make_test_config(), oidc_provider=provider)
    async with storage.session() as session:
        await core.secret_store.bootstrap(session, resolve_kek(f"env:{TEST_KEK_ENV_VAR}"))
        await session.commit()
    app = create_app(storage=storage, security=core,
                     android_config=AndroidConfig(enabled=True, push=PUSH_ON), device_hub=DeviceHub())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield Api(client=client, provider=provider, core=core, storage=storage)


async def _device(api: Api, device_id) -> Device:
    async with api.storage.session() as db:
        return await db.get(Device, device_id)


async def _register(api: Api, phone, token=TOKEN):
    return await api.client.put(PUSH_TOKEN, json={"provider": "fcm", "token": token}, headers=phone.auth)


async def test_push_is_off_by_default_and_nothing_can_be_registered(api):
    phone = await api.onboard("alice")
    config = await api.client.get(PUSH_CONFIG, headers=phone.auth)
    assert config.status_code == 200 and config.json() == {"provider": "none"}
    refused = await _register(api, phone)
    assert refused.status_code == 409
    assert (await _device(api, phone.device_id)).push_token is None


async def test_the_push_config_carries_public_client_ids_only(push_api):
    phone = await push_api.onboard("alice")
    body = (await push_api.client.get(PUSH_CONFIG, headers=phone.auth)).json()
    assert PushClientConfig.model_validate(body).fcm == CLIENT
    assert "service_account" not in json.dumps(body) and "JARVIS_TEST_FCM_SA" not in json.dumps(body)
    assert (await push_api.client.get(PUSH_CONFIG)).status_code == 401


async def test_a_device_binds_its_own_token_and_rotation_replaces_it(push_api):
    phone = await push_api.onboard("alice")
    assert (await _register(push_api, phone)).status_code == 204
    device = await _device(push_api, phone.device_id)
    assert (device.push_provider, device.push_token) == ("fcm", TOKEN)
    # onNewToken: the rotated token replaces the old one.
    assert (await _register(push_api, phone, OTHER_TOKEN)).status_code == 204
    assert (await _device(push_api, phone.device_id)).push_token == OTHER_TOKEN
    # Turning push off on the phone clears it (idempotently).
    for _ in range(2):
        assert (await push_api.client.delete(PUSH_TOKEN, headers=phone.auth)).status_code == 204
    assert (await _device(push_api, phone.device_id)).push_token is None


async def test_the_body_cannot_name_another_device(push_api):
    alice = await push_api.onboard("alice")
    bob = await push_api.onboard("bob")
    resp = await push_api.client.put(
        PUSH_TOKEN, json={"provider": "fcm", "token": TOKEN, "device_id": str(bob.device_id)}, headers=alice.auth
    )
    assert resp.status_code == 422
    assert (await _device(push_api, bob.device_id)).push_token is None
    assert (await _device(push_api, alice.device_id)).push_token is None


async def test_one_token_wakes_one_device_and_moves_only_within_a_user(push_api):
    first = await push_api.onboard("alice")
    bootstrap = await push_api.oidc_login()
    second_id, second_cred = await push_api.register_device(bootstrap)
    second_token = (await push_api.issue_token(second_id, second_cred)).json()["access_token"]

    assert (await _register(push_api, first)).status_code == 204
    # The same app install re-enrolled as Alice's second device: it moves.
    moved = await push_api.client.put(PUSH_TOKEN, json={"provider": "fcm", "token": TOKEN},
                                      headers={"Authorization": f"Bearer {second_token}"})
    assert moved.status_code == 204
    assert (await _device(push_api, first.device_id)).push_token is None
    assert (await _device(push_api, second_id)).push_token == TOKEN

    # Bob presenting Alice's token is refused, and Alice keeps it.
    bob = await push_api.onboard("bob")
    assert (await _register(push_api, bob)).status_code == 409
    assert (await _device(push_api, second_id)).push_token == TOKEN
    assert (await _device(push_api, bob.device_id)).push_token is None


async def test_revocation_clears_the_token_and_a_revoked_device_cannot_register(push_api):
    phone = await push_api.onboard("alice")
    assert (await _register(push_api, phone)).status_code == 204
    resp = await push_api.client.delete(f"{API_V1_PREFIX}/devices/{phone.device_id}", headers=phone.auth)
    assert resp.status_code == 204
    device = await _device(push_api, phone.device_id)
    assert device.revoked and device.push_token is None and device.push_provider is None
    assert (await _register(push_api, phone)).status_code == 401


async def test_the_token_is_never_written_to_the_audit_trail(push_api):
    phone = await push_api.onboard("alice")
    await _register(push_api, phone)
    await push_api.client.delete(PUSH_TOKEN, headers=phone.auth)
    async with push_api.storage.session() as db:
        rows = (await db.execute(select(AuditEvent))).scalars().all()
    actions = [r.action for r in rows]
    assert "device.push_token.registered" in actions and "device.push_token.cleared" in actions
    assert not any(TOKEN in json.dumps({c.name: str(getattr(r, c.name)) for c in r.__table__.columns})
                   for r in rows)


# ── the waker: who is woken, how often ──────────────────────────────────


class RecordingSender:
    def __init__(self, outcome: WakeOutcome = WakeOutcome.SENT) -> None:
        self.tokens: list[str] = []
        self.outcome = outcome

    async def send(self, token: str) -> WakeOutcome:
        self.tokens.append(token)
        return self.outcome


async def _bind(api: Api, device_id, token=TOKEN) -> None:
    async with api.storage.session() as db:
        device = await db.get(Device, device_id)
        device.push_provider, device.push_token = "fcm", token
        await db.commit()


async def test_the_waker_wakes_only_an_unrevoked_registered_device_of_that_user(api):
    alice = await api.onboard("alice")
    bob = await api.onboard("bob")
    sender = RecordingSender()
    waker = DeviceWaker(storage=api.storage, sender=sender)

    alice_user = (await _device(api, alice.device_id)).user_id
    assert await waker.wake(alice.device_id, user_id=alice_user) is False  # no registration
    await _bind(api, alice.device_id)
    bob_user = (await _device(api, bob.device_id)).user_id
    assert await waker.wake(alice.device_id, user_id=bob_user) is False  # not Bob's device
    assert await waker.wake(uuid.uuid4(), user_id=alice_user) is False  # unknown
    assert await waker.wake(alice.device_id, user_id=alice_user) is True
    await waker.drain()
    assert sender.tokens == [TOKEN]

    await api.client.delete(f"{API_V1_PREFIX}/devices/{alice.device_id}", headers=alice.auth)
    fresh = DeviceWaker(storage=api.storage, sender=sender)
    assert await fresh.wake(alice.device_id, user_id=alice_user) is False  # revoked: never again
    await fresh.drain()
    assert sender.tokens == [TOKEN]


async def test_duplicate_wakes_coalesce_and_a_connected_device_is_not_woken(api):
    phone = await api.onboard("alice")
    user = (await _device(api, phone.device_id)).user_id
    await _bind(api, phone.device_id)
    sender = RecordingSender()
    connected = {"yes": False}

    async def is_connected(device_id):
        return connected["yes"]

    waker = DeviceWaker(storage=api.storage, sender=sender, is_connected=is_connected, min_interval_seconds=30)
    results = await asyncio.gather(*[waker.wake(phone.device_id, user_id=user) for _ in range(5)])
    await waker.drain()
    assert all(results)
    assert sender.tokens == [TOKEN]  # five callers, one push
    assert await waker.wake(phone.device_id, user_id=user) is True
    await waker.drain()
    before = len(sender.tokens)
    connected["yes"] = True
    assert await waker.wake(phone.device_id, user_id=user) is False
    await waker.drain()
    assert len(sender.tokens) == before


async def test_a_dead_token_is_cleared_but_a_newer_one_is_kept(api):
    phone = await api.onboard("alice")
    user = (await _device(api, phone.device_id)).user_id
    await _bind(api, phone.device_id)
    waker = DeviceWaker(storage=api.storage, sender=RecordingSender(WakeOutcome.TOKEN_INVALID))
    assert await waker.wake(phone.device_id, user_id=user) is True
    await waker.drain()
    assert (await _device(api, phone.device_id)).push_token is None

    # Compare-and-clear: the device re-registered before the answer came back.
    await _bind(api, phone.device_id, TOKEN)

    class Racing(RecordingSender):
        async def send(self, token):
            await _bind(api, phone.device_id, OTHER_TOKEN)
            return WakeOutcome.TOKEN_INVALID

    waker = DeviceWaker(storage=api.storage, sender=Racing())
    assert await waker.wake(phone.device_id, user_id=user) is True
    await waker.drain()
    assert (await _device(api, phone.device_id)).push_token == OTHER_TOKEN


# ── the hub: an offline device fails now; a wake is only a request ──────


class FixedWaker:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def wake(self, device_id, *, user_id):
        self.calls.append((device_id, user_id))
        return self.answer


def _operation(device_id, user_id):
    return build_operation(capability="app.interact", operation="read_screen_element",
                           package_name="com.example", arguments={"view_id": "total"},
                           user_id=user_id, task_id=uuid.uuid4(), device_id=device_id)


@pytest.mark.parametrize("woken", [True, False])
async def test_an_offline_device_fails_at_once_and_nothing_is_queued(woken):
    hub = DeviceHub()
    waker = FixedWaker(woken)
    hub.attach_waker(waker)
    device, user = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(ExecutionError) as caught:
        await hub.send(_operation(device, user))
    assert caught.value.code is ExecutionErrorCode.DEVICE_UNAVAILABLE
    assert caught.value.required_platform == (DEVICE_CHANNEL if woken else None)
    assert waker.calls == [(device, user)]
    assert hub._pending == {}


async def test_a_failing_waker_never_changes_how_an_offline_send_fails():
    class Broken:
        async def wake(self, device_id, *, user_id):
            raise RuntimeError("database is locked")

    hub = DeviceHub()
    hub.attach_waker(Broken())
    with pytest.raises(ExecutionError) as caught:
        await hub.send(_operation(uuid.uuid4(), uuid.uuid4()))
    assert caught.value.code is ExecutionErrorCode.DEVICE_UNAVAILABLE
    assert caught.value.required_platform is None


async def test_without_push_the_hub_behaves_exactly_as_before():
    hub = DeviceHub()
    with pytest.raises(ExecutionError) as caught:
        await hub.send(_operation(uuid.uuid4(), uuid.uuid4()))
    assert caught.value.code is ExecutionErrorCode.DEVICE_UNAVAILABLE
    assert caught.value.required_platform is None


# ── configuration and composition ───────────────────────────────────────


def test_push_config_defaults_to_none_and_fcm_needs_its_section():
    assert AndroidConfig().push.provider.value == "none"
    with pytest.raises(ValueError):
        AndroidPushConfig.model_validate({"provider": "fcm"})
    with pytest.raises(ValueError):  # a literal credential is a load-time failure
        AndroidPushConfig.model_validate(
            {"provider": "fcm", "fcm": {"client": CLIENT.model_dump(), "service_account_ref": '{"type":"x"}'}}
        )


def _app_config(push: dict | None) -> AppConfig:
    from tests.runtime.conftest import base_config_payload

    payload = base_config_payload()
    payload["android"] = {"enabled": True, **({"push": push} if push else {})}
    return AppConfig.model_validate(payload)


def test_no_waker_exists_unless_fcm_is_configured(tmp_path, monkeypatch):
    from server.composition import build_application
    from server.storage import SQLAlchemyStorageBackend

    from tests.dbsupport import database_url_for

    monkeypatch.setenv(TEST_KEK_ENV_VAR, "unused")
    storage = SQLAlchemyStorageBackend(database_url_for(tmp_path / 'a.db'))
    app = build_application(_app_config(None), storage=storage, extra_tools=[])
    assert app.state.device_waker is None and app.state.device_hub._waker is None

    storage = SQLAlchemyStorageBackend(database_url_for(tmp_path / 'b.db'))
    app = build_application(_app_config(PUSH_ON.model_dump(mode="json")), storage=storage, extra_tools=[])
    assert app.state.device_waker is not None and app.state.device_hub._waker is app.state.device_waker


async def test_wakes_are_audited_by_device_and_the_credential_class_is_enforced(api, monkeypatch):
    from fastapi import FastAPI

    from server.composition.push import push_credential, wake_recorder
    from server.composition.secret_context import SecretUnavailable
    from server.secrets.requester import SecretRequester
    from server.security.audit import AuditLogger
    from shared.schemas.enums import SecretClass, SecretOwnerScopeType

    phone = await api.onboard("alice")
    user = (await _device(api, phone.device_id)).user_id
    app = FastAPI()
    app.state.storage, app.state.security = api.storage, api.core

    await wake_recorder(app)(phone.device_id, user, WakeOutcome.SENT)
    await wake_recorder(app)(phone.device_id, user, WakeOutcome.TOKEN_INVALID)
    async with api.storage.session() as db:
        rows = (await db.execute(select(AuditEvent).where(AuditEvent.action.like("device.wake.%")))).scalars().all()
    assert [(r.action, r.result.value if hasattr(r.result, "value") else r.result) for r in rows] == [
        ("device.wake.sent", "success"), ("device.wake.failed", "failure")]
    assert all(r.device_id == phone.device_id for r in rows)

    # env: form.
    monkeypatch.setenv("JARVIS_TEST_FCM_SA", '{"type": "service_account"}')
    assert await push_credential("env:JARVIS_TEST_FCM_SA", app)() == '{"type": "service_account"}'
    monkeypatch.delenv("JARVIS_TEST_FCM_SA")
    with pytest.raises(SecretUnavailable):
        await push_credential("env:JARVIS_TEST_FCM_SA", app)()

    # secretstore: only a server-owned `oauth_token` secret is handed over.
    async def store(value: str, secret_class: SecretClass) -> str:
        async with api.storage.session() as db:
            handle = await api.core.secret_store.set(
                db, value=value, secret_class=secret_class, owner_scope_type=SecretOwnerScopeType.SERVER,
                owner_scope_id=None, requester=SecretRequester.server(),
                audit=AuditLogger(db, request_id=uuid.uuid4()),
            )
            await db.commit()
        return handle

    good = await store('{"type": "service_account", "k": 1}', SecretClass.OAUTH_TOKEN)
    assert await push_credential(f"secretstore:{good}", app)() == '{"type": "service_account", "k": 1}'
    wrong = await store("sk-model-key", SecretClass.MODEL_API_KEY)
    with pytest.raises(SecretUnavailable):
        await push_credential(f"secretstore:{wrong}", app)()
