"""A user's second endpoint, against the runtime (docs/24 preparation).

docs/24 (`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]`) adds a desktop client
as another endpoint of the same user. These tests pin how the *existing*
runtime treats a second endpoint of one user, so that later desktop work is
built against measured behaviour rather than assumed behaviour. They decide
nothing: the second endpoint here is an ordinary registered device (the only
platform the registry has is `android`), and each test names the open owner
decision whose current state it records.

* OD-DEV-1 (ratified) / OD-EP-6 (open): an operation runs only on the device
  of the principal that authorized it — never on the user's *other* device,
  connected or not.
* OD-EP-5 (open): same-user approval from another device already works
  (`test_confirmation_boundary.py::test_the_same_user_may_approve_from_another_device`,
  OD-A1's same-user continuity). What these tests add: a tier-4 approval needs
  the *approving* endpoint's own re-attestation; the originating endpoint's
  does not carry over.
"""

from __future__ import annotations

import asyncio
import json

from server.execution.device_hub import DeviceHub
from server.tools.platforms import AndroidDeviceAdapter
from tests.device_channel_support import FakeConnection
from tests.runtime.conftest import android_ui_tool, ask, call, final, pending_of

SCOPE = {"package_name": "com.example.invoices"}


def _read_screen() -> str:
    return call("ui.app", "read_screen_element", args={"view_id": "invoice_total"}, platform="android")


async def _two_endpoints(make_harness):
    hub = DeviceHub()
    h = await make_harness(extra_tools=[android_ui_tool(AndroidDeviceAdapter("app.interact", hub))])
    first = await h.user("alice")
    second = await h.user("alice")
    assert second.user_id == first.user_id and second.device_id != first.device_id
    # User-scoped: the grant covers both endpoints, so the only thing that can
    # keep an operation off the other endpoint is the exact-device rule.
    await h.grant(first, "app.interact", resource_scope=SCOPE)
    return h, hub, first, second


async def test_an_operation_never_goes_to_the_users_other_connected_endpoint(make_harness):
    """OD-DEV-1: the task was authorized on the second endpoint, which is
    offline. The first endpoint is online and holds the same user-wide grant.
    The operation still fails `device_unavailable` for the second endpoint and
    the first receives nothing — there is no "any device of this user"."""

    h, hub, first, second = await _two_endpoints(make_harness)
    first_socket = FakeConnection()
    await hub.attach(user_id=first.user_id, device_id=first.device_id, connection=first_socket)

    h.model.push(ask("app.interact", scope=SCOPE), _read_screen(), final("That device is not connected."))
    body = (await h.submit(second, "what is the invoice total?")).json()

    assert body["status"] == "completed", body
    assert first_socket.sent == []
    assert hub._pending == {}  # nothing was queued for a reconnect (ANDC-T2)
    assert "device_unavailable" in h.model.all_text()


async def test_each_endpoint_receives_only_the_operations_it_authorized(make_harness):
    """Both endpoints online: an operation from the second endpoint's task is
    delivered to the second endpoint's socket, addressed to it, and to no
    other. (A desktop joining this channel therefore receives exactly what its
    own tasks authorize. Nothing server-side reads `Device.platform` today, so
    docs/24 §10.3's platform vocabulary, and a platform check before an
    Android operation is dispatched, must exist before a desktop may join.)"""

    h, hub, first, second = await _two_endpoints(make_harness)
    first_socket, second_socket = FakeConnection(), FakeConnection()
    await hub.attach(user_id=first.user_id, device_id=first.device_id, connection=first_socket)
    second_session = await hub.attach(user_id=second.user_id, device_id=second.device_id,
                                      connection=second_socket)

    h.model.push(ask("app.interact", scope=SCOPE), _read_screen(), final("The total is 42."))
    submitted = asyncio.ensure_future(h.submit(second, "what is the invoice total?"))
    [frame] = await second_socket.wait_frames(1)
    assert frame["type"] == "operation" and frame["device_id"] == str(second.device_id)
    hub.deliver(second_session, json.dumps({
        "type": "result", "op_id": frame["op_id"], "status": "ok", "perception_level": "accessibility",
        "result": {"app": {"package_name": "com.example.invoices"}, "nodes": [
            {"id": 0, "role": "android.widget.TextView", "text": "42", "bounds": [0, 0, 10, 10]}]},
    }))
    body = (await asyncio.wait_for(submitted, 10)).json()

    assert body["status"] == "completed" and body["response"] == "The total is 42."
    assert first_socket.sent == []


async def test_a_tier4_approval_from_another_endpoint_needs_that_endpoints_own_step_up(h):
    """OD-EP-5's current state. The originating endpoint re-attests; the
    approval arrives from the second endpoint, which has not. Step-up is judged
    on the device that approves (docs/23 §3), so the approval is refused and
    nothing runs, until the second endpoint's own user-presence key signs."""

    alice = await h.user("alice")
    await h.grant(alice, "file.write")
    h.model.push(ask("file.write"), call("files.write", "bulk_delete", args={"pattern": "*"}))
    details = pending_of(await h.submit(alice))
    assert details["pending"]["requires_step_up"] is True

    second = await h.user("alice")
    await h.step_up(alice)

    refused = await h.confirm(second, details["task_id"], details["confirmation_token"], step_up=False)
    assert refused.status_code == 401
    assert refused.json()["error"]["details"].get("step_up_required") is True
    assert h.writes.calls == []
    assert (await h.get(second, details["task_id"])).json()["status"] == "awaiting_confirmation"

    h.model.push(final())
    approved = await h.confirm(second, details["task_id"], details["confirmation_token"])
    assert approved.status_code == 200, approved.text
    assert h.writes.operations() == ["bulk_delete"]
