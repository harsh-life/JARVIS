"""docs/23 §4 push wake, end to end through the runtime (ANDC-T9, ANDC-T2).

A task reaches a device operation while the phone is offline and push wake is
configured. What must hold:

* the operation fails at once and is **not queued** (ANDC-T2); the hub sends
  nothing to any device;
* the push that goes out — captured at the HTTP transport, exactly as it would
  reach Google — is the fixed wake and nothing else: not the task, the user's
  words, the app, the capability, the operation, or any id (ANDC-T9);
* the *task* waits (bounded) for the channel; the push itself resumes nothing;
* only when that device's channel is back does the call run again — proposed
  and authorized afresh, as a **new** operation with a new op_id.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from server.execution.device_hub import DeviceHub
from server.execution.device_wake import GOOGLE_TOKEN_URL, DeviceWaker, FcmWakeSender
from server.security.audit import AuditLogger
from server.storage.models import Device
from server.tools.platforms import AndroidDeviceAdapter
from shared.schemas.push import fcm_wake_message
from tests.device_channel_support import FakeConnection
from tests.runtime.conftest import android_ui_tool, ask, call, final

SCOPE = {"package_name": "com.example.invoices"}
TOKEN = "fcm-registration-token-" + "Q7r8S9t0" * 16
SECRET_WORDS = "what is the total on invoice SECRET-INVOICE-7731?"


class CapturingGoogle:
    def __init__(self) -> None:
        self.sends: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == GOOGLE_TOKEN_URL:
            return httpx.Response(200, json={"access_token": "ya29.test", "expires_in": 3600})
        self.sends.append(request)
        return httpx.Response(200, json={"name": "projects/p/messages/1"})


def _service_account() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return json.dumps({"type": "service_account", "private_key": pem,
                       "client_email": "wake@p.iam.gserviceaccount.com", "token_uri": GOOGLE_TOKEN_URL})


async def _setup(make_harness):
    hub = DeviceHub()
    h = await make_harness(extra_tools=[android_ui_tool(AndroidDeviceAdapter("app.interact", hub))])
    google = CapturingGoogle()
    credential = _service_account()

    async def resolve() -> str:
        return credential

    waker = DeviceWaker(
        storage=h.storage,
        sender=FcmWakeSender(project_id="jarvis-test-project", credential=resolve,
                             transport=httpx.MockTransport(google.handler)),
        is_connected=lambda device_id: hub.is_connected(device_id=device_id),
    )
    hub.attach_waker(waker)
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=SCOPE)
    async with h.storage.session() as db:
        device = await db.get(Device, alice.device_id)
        device.push_provider, device.push_token = "fcm", TOKEN
        await db.commit()
    return h, hub, waker, google, alice


def _read():
    return call("ui.app", "read_screen_element", args={"view_id": "invoice_total"}, platform="android")


async def _resume(h, device_id):
    async with h.storage.session() as db:
        results = await h.app.state.agent_tasks.resume_after_platform(
            db, device_id=device_id, dependency="device_channel", audit=AuditLogger(db, request_id=uuid.uuid4()))
        await db.commit()
    return results


async def test_an_offline_phone_is_woken_with_nothing_but_the_wake(make_harness):
    h, hub, waker, google, alice = await _setup(make_harness)
    h.model.push(ask("app.interact", scope=SCOPE), _read(), final("The total is 42."))

    resp = await h.submit(alice, SECRET_WORDS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "waiting_for_platform"
    assert body["waiting_for"]["dependency"] == "device_channel"
    assert body["waiting_for"]["device_id"] == str(alice.device_id)
    await waker.drain()

    # ANDC-T9: what reached "Google" is exactly the wake.
    [send] = google.sends
    wire = send.content.decode()
    assert json.loads(wire) == fcm_wake_message(TOKEN)
    forbidden = [
        body["task_id"], str(alice.user_id), str(alice.device_id), alice.token, "SECRET-INVOICE-7731",
        "invoice", "com.example", "app.interact", "read_screen_element", "invoice_total", "ui.app",
        "capability", "operation", "primitive", "op_id", "task", "grant", "confirmation",
    ]
    for needle in forbidden:
        assert needle not in wire, needle


async def test_nothing_is_queued_and_only_the_reconnect_resumes_with_a_fresh_operation(make_harness):
    h, hub, waker, google, alice = await _setup(make_harness)
    h.model.push(ask("app.interact", scope=SCOPE), _read(), final("The total is 42."))
    task_id = (await h.submit(alice, SECRET_WORDS)).json()["task_id"]
    await waker.drain()
    assert hub._pending == {}  # ANDC-T2: the failed operation is gone

    # A push arriving changes nothing server-side: still waiting.
    assert (await h.get(alice, task_id)).json()["status"] == "waiting_for_platform"

    # The phone's channel is back (the gateway calls this after it
    # authenticated, tests/security_core/test_device_channel.py).
    connection = FakeConnection()
    session = await hub.attach(user_id=alice.user_id, device_id=alice.device_id, connection=connection)
    resuming = asyncio.ensure_future(_resume(h, alice.device_id))
    [frame] = await connection.wait_frames(1)
    assert frame["type"] == "operation" and frame["device_id"] == str(alice.device_id)
    assert frame["operation"] == "read_screen_element" and frame["package_name"] == "com.example.invoices"
    hub.deliver(session, json.dumps({
        "type": "result", "op_id": frame["op_id"], "status": "ok", "perception_level": "accessibility",
        "result": {"app": {"package_name": "com.example.invoices"}, "nodes": [
            {"id": 0, "role": "android.widget.TextView", "text": "42", "bounds": [0, 0, 10, 10]}]},
    }))
    [result] = await asyncio.wait_for(resuming, 10)
    assert result.status.value == "completed" and result.response == "The total is 42."
    # The fresh operation's result reached the worker as a parsed observation.
    seen = h.model.all_text()
    assert "available again" in seen and '"42"' in seen and "action_failed" not in seen
    # A single wake was sent for this outage; nothing about the reconnect
    # sent another.
    assert len(google.sends) == 1


async def test_a_revoked_grant_is_honoured_on_reconnect(make_harness):
    h, hub, waker, google, alice = await _setup(make_harness)
    h.model.push(ask("app.interact", scope=SCOPE), _read(), final("I could not read it."))
    await h.submit(alice, SECRET_WORDS)
    await waker.drain()
    grants = await h.client.get("/api/v1/capabilities", headers=alice.auth)
    for grant in grants.json()["items"]:
        await h.client.delete(f"/api/v1/capabilities/{grant['grant_id']}", headers=alice.auth)

    connection = FakeConnection()
    await hub.attach(user_id=alice.user_id, device_id=alice.device_id, connection=connection)
    [result] = await _resume(h, alice.device_id)
    assert result.status.value == "completed"
    assert connection.sent == []  # the revoked authority sent nothing


async def test_without_a_registration_nothing_waits_and_nothing_is_sent(make_harness):
    h, hub, waker, google, alice = await _setup(make_harness)
    async with h.storage.session() as db:
        device = await db.get(Device, alice.device_id)
        device.push_provider, device.push_token = None, None
        await db.commit()
    h.model.push(ask("app.interact", scope=SCOPE), _read(), final("Your phone is not connected."))
    body = (await h.submit(alice, SECRET_WORDS)).json()
    await waker.drain()
    assert body["status"] == "completed"  # device_unavailable, as before push existed
    assert google.sends == []
