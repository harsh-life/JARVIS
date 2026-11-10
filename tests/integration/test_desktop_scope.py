"""OD-DESK-1 — a desktop companion's session reaches only the presentation
surface, checked against **every** route of the production composition.

The desktop companion presents; it never authorizes. Its device key and its
access tokens are ordinary JARVIS credentials, so the confinement must be the
server's, not the companion's good behaviour: a desktop token presented to any
route outside the three marked ones (its activity report, its own logout, its
own revocation) is refused `403` before the handler runs — whatever the
handler would have done, including submitting or confirming a task.

A route added later is covered automatically: it either refuses a desktop
token or this test fails until it is consciously marked
(`server/gateway/device_scope.py`).
"""

from __future__ import annotations

import base64
import secrets
import uuid
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from server.auth.desktop_handoff import s256_challenge
from server.auth.device import build_device_proof, registration_message
from server.gateway.app import API_V1_PREFIX
from tests.integration.test_route_inventory import OPERATOR, OTHER_CREDENTIAL, PUBLIC, _concrete, _routes

V1 = API_V1_PREFIX
DESKTOP_ROUTES = {
    ("GET", f"{V1}/presentation/activity"),
    ("POST", f"{V1}/sessions/logout"),
    ("DELETE", f"{V1}/devices/{{device_id}}"),
}


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


async def _desktop(h, subject: str) -> tuple[uuid.UUID, str, dict[str, str]]:
    """Sign a desktop companion in through the real handoff."""

    h.oidc.subject = subject
    verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    start = await h.client.get(f"{V1}/auth/oidc/start", params={
        "desktop_state": state, "desktop_challenge": s256_challenge(verifier), "desktop_port": 50123})
    assert start.status_code == 200, start.text
    provider_state = parse_qs(urlparse(start.json()["redirect_url"]).query)["state"][0]
    callback = await h.client.get(f"{V1}/auth/oidc/callback", params={"code": "c", "state": provider_state},
                                  follow_redirects=False)
    code = parse_qs(urlparse(callback.headers["location"]).query)["code"][0]
    bootstrap = (await h.client.post(f"{V1}/auth/desktop/handoff",
                                     json={"code": code, "code_verifier": verifier})).json()["bootstrap_token"]
    key = Ed25519PrivateKey.generate()
    public = _b64(key.public_key().public_bytes_raw())
    reg = await h.client.post(
        f"{V1}/devices", headers={"Authorization": f"Bearer {bootstrap}"},
        json={"platform": "desktop", "public_key": public,
              "key_proof": _b64(key.sign(registration_message(bootstrap_token=bootstrap, public_key=public)))})
    assert reg.status_code == 201, reg.text
    device_id = uuid.UUID(reg.json()["device_id"])
    seed = _b64(key.private_bytes_raw())
    tok = await h.client.post(f"{V1}/sessions/token", json={
        "device_credential": build_device_proof(device_id=device_id, device_credential=seed)})
    return device_id, seed, {"Authorization": f"Bearer {tok.json()['access_token']}"}


async def test_a_desktop_token_reaches_nothing_but_the_presentation_surface(make_harness):
    h = await make_harness(config={"agents": {"enabled": True}, "desktop": {"enabled": True}})
    phone = await h.user("alice")
    device_id, _, auth = await _desktop(h, "alice")
    routes = _routes(h.app)
    assert DESKTOP_ROUTES <= routes

    reached = []
    for method, path in sorted(routes - PUBLIC - OTHER_CREDENTIAL - DESKTOP_ROUTES):
        resp = await h.client.request(method, _concrete(path), json={},
                                      headers={**auth, "Idempotency-Key": uuid.uuid4().hex})
        # The operator's routes authenticate a superuser, not an access token,
        # and refuse any device token with 401; every other route refuses this
        # one with the scope's 403 before its handler runs.
        expected = (401, 403) if OPERATOR.match(path) else (403,)
        if resp.status_code not in expected:
            reached.append((method, path, resp.status_code))
    assert reached == [], reached

    # The three it may reach — and the device route only for itself.
    assert (await h.client.get(f"{V1}/presentation/activity", headers=auth)).status_code == 200
    other = await h.client.delete(f"{V1}/devices/{phone.device_id}", headers=auth)
    assert other.status_code == 403
    assert (await h.client.get(f"{V1}/me", headers=phone.auth)).status_code == 200
    assert (await h.client.post(f"{V1}/sessions/logout", headers=auth)).status_code == 204


async def test_a_desktop_sees_work_started_on_the_phone(make_harness):
    """The report is per user (docs/CHARACTER_PRESENTATION.md §8): a task the
    phone runs moves the desktop's character too."""

    from tests.runtime.conftest import final

    h = await make_harness(config={"desktop": {"enabled": True}})
    phone = await h.user("alice")
    _, _, auth = await _desktop(h, "alice")
    before = (await h.client.get(f"{V1}/presentation/activity", headers=auth)).json()
    assert before["components"]["engagement"] == 0
    h.model.push(final("done"))
    done = await h.submit(phone, "please help")
    assert done.json()["status"] == "completed", done.text
    after = (await h.client.get(f"{V1}/presentation/activity", headers=auth)).json()
    assert after["components"]["engagement"] > 0
    assert after["scores"]["satisfied"] >= 7
    # The desktop itself could not have submitted it.
    refused = await h.client.post(f"{V1}/agent/tasks", json={"input": "x"},
                                  headers={**auth, "Idempotency-Key": uuid.uuid4().hex})
    assert refused.status_code == 403
