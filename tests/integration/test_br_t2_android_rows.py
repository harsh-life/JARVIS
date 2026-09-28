"""BR-T2, the Android dimension (14 §4, 17 §4, docs/23 §9 "BR-T2 re-run for the
Android dimensions once the client exists").

`docs/OD_A1_BR_T2.md` §4 left the Android device **PENDING** because no device
client existed. It exists now (docs/23, Phases B–G). This module measures it,
on the production composition root with the real device hub, and a device that
runs the reference guard — the Python twin of the Kotlin `DeviceGuard`, held
to it by the shared conformance vectors (`shared/android/conformance_vectors.json`).

Three attacker models, kept apart exactly as in the other BR-T2 tables:

* **authorized** — another user driving the agent through paths the
  deterministic layer allows. Anything reachable here is a cross-user
  authorization failure, never an accepted residual.
* **app-RCE** — code running inside the server process (OD-A1 (a)'s class:
  "a compromised live application process may reach data and secrets that are
  already available to that running process").
* **at-rest** — someone holding the database file without the process.

The new fact this dimension adds: the phone is a **second enforcement point
the server process does not own**. An in-process attacker can put any
envelope on the victim's socket, but the victim's own phone still refuses
anything its user's grid or cached classification does not allow — so the
app-RCE reach into a phone is bounded by what that phone's user already
permits, not by the server.

Asserted in both directions (INV-20): contained rows must stay contained and
reachable rows must stay reachable; a change in either is a re-measurement,
not a test to delete.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from server.auth.step_up import attestation_message
from server.execution.android import build_operation, primitive_spec
from server.execution.device_hub import DeviceHub
from server.execution.device_wake import GOOGLE_TOKEN_URL, DeviceWaker, FcmWakeSender
from server.storage.models import Device
from server.tools.platforms import AndroidDeviceAdapter
from shared.schemas.device_channel import ResultKind
from shared.schemas.execution import ExecutionError
from shared.schemas.push import fcm_wake_message
from tests.dbsupport import store_contents
from tests.fake_device import DeviceLocalState, FakeDevice, default_result
from tests.runtime.conftest import android_ui_tool, ask, call, final

pytestmark = pytest.mark.asyncio

ALLOWED_APP = "com.example"  # classified non_sensitive by the harness config
OFF_APP = "com.example.notes"  # classified non_sensitive, but B never turned it on
UNCLASSIFIED_APP = "com.unclassified.app"  # B turned it on; nobody classified it
MARKER = "TEST-ONLY B's on-screen balance 91,442.17"
PUSH_TOKEN = "fcm-registration-token-" + "B0b5eCr" * 20


@dataclass
class Row:
    number: str
    attempt: str
    model: str
    reachable: bool
    detail: str

    def render(self) -> str:
        mark = "REACHABLE" if self.reachable else "contained"
        return f"  [{mark:>9}] {self.number} ({self.model}) {self.attempt} — {self.detail}"


def _tap(package: str) -> dict:
    """A UI-acting operation: both the cached classification and the per-app
    grid's `ui_interaction` toggle govern it on the phone."""

    return {"capability": "app.interact", "operation": "tap", "package_name": package,
            "arguments": {"view_id": "pay_now"}}


class _Google:
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


def _files_containing(root: Path, needle: bytes) -> list[str]:
    return [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and needle in p.read_bytes()]


async def test_br_t2_android_dimension(make_harness, tmp_path, capsys):
    hub = DeviceHub()
    h = await make_harness(extra_tools=[android_ui_tool(AndroidDeviceAdapter("app.interact", hub))])
    alice, bob = await h.user("alice"), await h.user("bob")  # A the attacker, B the victim
    element = primitive_spec("app.interact", "read_screen_element")
    screen = default_result({"capability": "app.interact", "operation": "read_screen_element",
                             "package_name": ALLOWED_APP})[0]
    if element.result is ResultKind.SCREEN_READ:
        screen["nodes"][0]["text"] = MARKER
    bob_phone = await FakeDevice(
        device_id=bob.device_id,
        state=DeviceLocalState(packages={ALLOWED_APP: {"screen_read", "ui_interaction"}},
                               app_policy={"non_sensitive": [ALLOWED_APP, OFF_APP]}),
        results={element.primitive: (screen, "accessibility")},
    ).attach(hub, user_id=bob.user_id)
    rows: list[Row] = []

    # ── 29. A's own agent task tries to drive B's phone ─────────────────────
    await h.grant(alice, "app.interact", resource_scope={"package_name": ALLOWED_APP})
    h.model.push(ask("app.interact", scope={"package_name": ALLOWED_APP}),
                 call("ui.app", "read_screen_element", args={"view_id": "total"}, platform="android"),
                 final("nothing"))
    resp = await h.submit(alice, "read the total on the phone")
    assert resp.status_code == 200, resp.text
    rows.append(Row("29", "A's agent task reaches B's connected phone", "authorized",
                    bool(bob_phone.received),
                    "every operation carries the authorizing principal's own device_id (OD-DEV-1) and the "
                    "hub delivers to exactly that device; A's phone is offline, so A's call fails and "
                    "B's phone receives nothing"))

    # ── 30–32. in-process code puts a crafted envelope on B's socket ────────
    async def crafted(package: str) -> tuple[bool, str]:
        """Bypass the engine entirely: build an operation for B's phone and
        put it on B's socket. Measured by what *B's phone executed*."""

        op = build_operation(**_tap(package), user_id=bob.user_id, task_id=uuid.uuid4(),
                             device_id=bob.device_id)
        before = len(bob_phone.executed)
        try:
            await hub.send(op)
            outcome = "ok"
        except ExecutionError as exc:
            outcome = exc.code.value
        return len(bob_phone.executed) > before, outcome

    executed, _ = await crafted(ALLOWED_APP)
    rows.append(Row("30", "in-process code has B's phone tap in an app B allows", "app-RCE", executed,
                    "the hub is in-process: bypassing the engine, the attacker reaches whatever B's own "
                    "grid and cached classification already allow — inside OD-A1 (a)"))
    executed, why = await crafted(OFF_APP)
    rows.append(Row("31", "in-process code has B's phone tap in an app B toggled off", "app-RCE", executed,
                    f"B's phone refuses on its own per-app grid ({why}) — the device guard is not the "
                    "server's to disable (08 §4 two-layer enforcement)"))
    bob_phone.state.packages[UNCLASSIFIED_APP] = {"screen_read", "ui_interaction"}
    executed, why = await crafted(UNCLASSIFIED_APP)
    rows.append(Row("32", "in-process code has B's phone tap in an app B enabled but nobody classified",
                    "app-RCE", executed,
                    f"B's phone refuses from its cached classification ({why}) even with B's toggle on "
                    "(docs/23 §5.5)"))

    # ── 33. forge B's step-up (biometric) attestation from server material ──
    async with h.storage.session() as db:
        bob_row = await db.get(Device, bob.device_id)
        spki = bob_row.step_up_public_key
        bob_row.push_provider, bob_row.push_token = "fcm", PUSH_TOKEN
        await db.commit()
    public = serialization.load_der_public_key(base64.urlsafe_b64decode(spki + "=" * (-len(spki) % 4)))
    forged = ec.generate_private_key(ec.SECP256R1()).sign(
        attestation_message(device_id=bob.device_id, challenge="c"), ec.ECDSA(hashes.SHA256()))
    try:
        public.verify(forged, attestation_message(device_id=bob.device_id, challenge="c"), ec.ECDSA(hashes.SHA256()))
        verified = True
    except InvalidSignature:
        verified = False
    rows.append(Row("33", "forge B's step-up attestation from anything the server holds", "app-RCE", verified,
                    "the server holds only B's step-up *public* key; the private half never leaves B's "
                    "Keystore, usable only after B's biometric (03 §5.5). In-process code need not attest "
                    "at all to call a tool directly — that is row 2's class, not a forged presence"))

    # ── 34. wake B's phone with B's stored push token ───────────────────────
    google = _Google()
    credential = _service_account()

    async def resolve() -> str:
        return credential

    async def offline(device_id) -> bool:
        return False

    waker = DeviceWaker(storage=h.storage,
                        sender=FcmWakeSender(project_id="p", credential=resolve,
                                             transport=httpx.MockTransport(google.handler)),
                        is_connected=offline)
    woke = await waker.wake(bob.device_id, user_id=bob.user_id)
    await waker.drain()
    payloads = [json.loads(r.content) for r in google.sends]
    rows.append(Row("34", "send B's phone a push using B's stored registration token", "app-RCE",
                    bool(woke and payloads),
                    "the token and the sending credential are in process, so a wake can be sent — but its "
                    "only possible content is the fixed {type: wake}: it reconnects the phone and "
                    "authorizes, carries and runs nothing (ANDC-T9)"))
    assert payloads == [fcm_wake_message(PUSH_TOKEN)]

    # ── 35–36. what the database file holds about B's phone ─────────────────
    raw = await store_contents(h.storage)
    private_markers = [b"BEGIN PRIVATE KEY", b"BEGIN EC PRIVATE KEY", bob.credential.encode()]
    rows.append(Row("35", "recover B's device credential or step-up private key from the store", "at-rest",
                    any(m in raw for m in private_markers),
                    "the store holds public verifiers only (Ed25519 device key, P-256 step-up key); the "
                    "private halves exist only in B's Keystore (03 §4.2 [REC])"))
    rows.append(Row("36", "read B's push registration token from the store", "at-rest",
                    PUSH_TOKEN.encode() in raw,
                    "plaintext column; it identifies B's app to Google and lets only the holder of the "
                    "server's separate FCM credential send the content-free wake (row 34)"))

    # ── 37. B's own screen content, after B's own task, anywhere on disk ────
    await h.grant(bob, "app.interact", resource_scope={"package_name": ALLOWED_APP})
    h.model.push(ask("app.interact", scope={"package_name": ALLOWED_APP}),
                 call("ui.app", "read_screen_element", args={"view_id": "total"}, platform="android"),
                 final("done"))
    done = await h.submit(bob, "read my balance")
    assert done.status_code == 200 and done.json()["status"] == "completed", done.text
    assert MARKER in h.model.all_text() or element.result is not ResultKind.SCREEN_READ
    leftovers = _files_containing(tmp_path, MARKER.encode())
    if MARKER.encode() in await store_contents(h.storage):  # the store, wherever it lives
        leftovers.append("relational store")
    rows.append(Row("37", "recover B's screen content from the server's disk after B's task", "at-rest",
                    bool(leftovers),
                    "screen results are transient task context: validated, shown to the model as untrusted "
                    "data, never persisted or written to memory (08 §8, docs/23 §6)"))

    with capsys.disabled():
        print("\nBR-T2 Android dimension (docs/OD_A1_BR_T2.md §3d):")
        for row in rows:
            print(row.render())

    measured = {row.number: row.reachable for row in rows}
    assert measured == {
        "29": False, "30": True, "31": False, "32": False, "33": False,
        "34": True, "35": False, "36": True, "37": False,
    }, measured
    # No *authorized* path reaches another user's phone.
    assert not any(row.reachable for row in rows if row.model == "authorized")
