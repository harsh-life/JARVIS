"""Device-generated credentials (docs/23 §3, 03 §4.2's asymmetric option).

The Android client generates its Ed25519 key pair inside the Keystore and
registers only the public half, with a signature proving it holds the private
one. The server then never possesses a private key at all — not even for the
instant 03 §3.1's server-minted path does. The keys here stand in for the
Keystore's: the server sees exactly what it would see from a phone.
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from server.auth.device import registration_message, rotation_message
from server.gateway.app import API_V1_PREFIX
from tests.security_core.helpers import database_contains


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class DeviceKey:
    """What the Keystore holds: a private key the test never sends anywhere."""

    def __init__(self) -> None:
        self._key = Ed25519PrivateKey.generate()
        self.public = _b64(self._key.public_key().public_bytes_raw())

    def sign(self, message: bytes) -> str:
        return _b64(self._key.sign(message))

    @property
    def credential(self) -> str:
        # Only for `build_device_proof` in the test harness, which signs
        # locally — the value is never sent to the server.
        return _b64(self._key.private_bytes_raw())


async def _register(api, bootstrap: str, key: DeviceKey, *, proof: str | None = None):
    return await api.client.post(
        f"{API_V1_PREFIX}/devices",
        json={
            "platform": "android",
            "public_key": key.public,
            "key_proof": proof
            or key.sign(registration_message(bootstrap_token=bootstrap, public_key=key.public)),
        },
        headers={"Authorization": f"Bearer {bootstrap}"},
    )


async def test_a_device_held_key_registers_and_nothing_private_is_returned(api):
    key = DeviceKey()
    bootstrap = await api.oidc_login()
    resp = await _register(api, bootstrap, key)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "device_credential" not in body

    import uuid

    device_id = uuid.UUID(body["device_id"])
    token = await api.issue_token(device_id, key.credential)
    assert token.status_code == 200, token.text

    async with api.storage.session() as session:
        assert not await database_contains(session, key.credential)


async def test_a_key_the_device_does_not_hold_is_refused_and_the_login_survives(api):
    """Registering someone else's public key (or garbage) fails before the
    single-use bootstrap token is spent, so the user can retry."""

    key, impostor = DeviceKey(), DeviceKey()
    bootstrap = await api.oidc_login()
    wrong = impostor.sign(registration_message(bootstrap_token=bootstrap, public_key=key.public))
    refused = await _register(api, bootstrap, key, proof=wrong)
    assert refused.status_code == 401

    retry = await _register(api, bootstrap, key)
    assert retry.status_code == 201, retry.text


async def test_a_registration_signature_is_bound_to_its_bootstrap_token(api):
    key = DeviceKey()
    first = await api.oidc_login()
    second = await api.oidc_login()
    signed_for_first = key.sign(registration_message(bootstrap_token=first, public_key=key.public))
    resp = await _register(api, second, key, proof=signed_for_first)
    assert resp.status_code == 401


async def test_a_public_key_without_its_proof_is_refused(api):
    key = DeviceKey()
    bootstrap = await api.oidc_login()
    resp = await api.client.post(
        f"{API_V1_PREFIX}/devices",
        json={"platform": "android", "public_key": key.public},
        headers={"Authorization": f"Bearer {bootstrap}"},
    )
    assert resp.status_code == 401


async def test_a_malformed_public_key_is_refused(api):
    key = DeviceKey()
    bootstrap = await api.oidc_login()
    short = _b64(b"\x01" * 31) + "AAAAAAAAAA"
    resp = await api.client.post(
        f"{API_V1_PREFIX}/devices",
        json={"platform": "android", "public_key": short, "key_proof": key.sign(b"x")},
        headers={"Authorization": f"Bearer {bootstrap}"},
    )
    assert resp.status_code == 401


async def _onboard_with_device_key(api):
    import uuid

    key = DeviceKey()
    bootstrap = await api.oidc_login()
    resp = await _register(api, bootstrap, key)
    device_id = uuid.UUID(resp.json()["device_id"])
    token = (await api.issue_token(device_id, key.credential)).json()["access_token"]
    return device_id, key, {"Authorization": f"Bearer {token}"}


async def test_rotating_to_a_new_device_held_key_retires_the_old_one(api):
    device_id, old, auth = await _onboard_with_device_key(api)
    new = DeviceKey()
    resp = await api.client.post(
        f"{API_V1_PREFIX}/devices/{device_id}/rotate",
        json={
            "public_key": new.public,
            "key_proof": new.sign(rotation_message(device_id=device_id, public_key=new.public)),
        },
        headers=auth,
    )
    assert resp.status_code == 200, resp.text
    assert "device_credential" not in resp.json()

    # AUTH-T6: no window where both work.
    assert (await api.issue_token(device_id, old.credential)).status_code == 401
    assert (await api.issue_token(device_id, new.credential)).status_code == 200


async def test_rotation_requires_possession_of_the_new_key(api):
    device_id, old, auth = await _onboard_with_device_key(api)
    new = DeviceKey()
    forged = old.sign(rotation_message(device_id=device_id, public_key=new.public))
    resp = await api.client.post(
        f"{API_V1_PREFIX}/devices/{device_id}/rotate",
        json={"public_key": new.public, "key_proof": forged},
        headers=auth,
    )
    assert resp.status_code == 401
    # Nothing changed: the old key still works.
    assert (await api.issue_token(device_id, old.credential)).status_code == 200
