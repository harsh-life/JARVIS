"""Step-up by re-attestation (03 §5.5 SESSION-003; docs/23 §3).

Through the real endpoints: a device registers a user-presence-bound step-up
key only in its enrolment window (or replaces it after re-attesting with the
old one); a step-up is a single-use, short-lived server challenge signed with
that key; approving a `high_irreversible` action needs one within the window —
a freshly refreshed access token alone never counts.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import update

from server.auth.step_up import attestation_message
from server.gateway.app import API_V1_PREFIX
from server.storage.models import Device
from tests.security_core.helpers import audit_actions, database_contains

KEY = f"{API_V1_PREFIX}/devices/me/step-up-key"
CHALLENGE = f"{API_V1_PREFIX}/sessions/step-up/challenge"
ATTEST = f"{API_V1_PREFIX}/sessions/step-up"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _spki(private_key) -> str:
    return _b64(private_key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo))


async def _register(api, device, key=None):
    key = key or ec.generate_private_key(ec.SECP256R1())
    resp = await api.client.post(KEY, json={"public_key": _spki(key)}, headers=device.auth)
    return key, resp


async def _attest(api, device, key, *, challenge=None, message=None):
    if challenge is None:
        resp = await api.client.post(CHALLENGE, headers=device.auth)
        assert resp.status_code == 200, resp.text
        challenge = resp.json()["challenge"]
    signed = message or attestation_message(device_id=device.device_id, challenge=challenge)
    signature = _b64(key.sign(signed, ec.ECDSA(hashes.SHA256())))
    return challenge, await api.client.post(ATTEST, json={"challenge": challenge, "signature": signature},
                                            headers=device.auth)


async def _age_registration(api, device, minutes=30):
    async with api.storage.session() as s:
        await s.execute(update(Device).where(Device.device_id == device.device_id)
                        .values(registered_at=datetime.now(timezone.utc) - timedelta(minutes=minutes)))
        await s.commit()


async def test_a_key_registered_at_enrolment_attests_and_the_challenge_is_single_use(api):
    phone = await api.onboard("alice")
    key, resp = await _register(api, phone)
    assert resp.status_code == 204, resp.text
    challenge, attested = await _attest(api, phone, key)
    assert attested.status_code == 200, attested.text
    # The same challenge again is refused: single use.
    _, replay = await _attest(api, phone, key, challenge=challenge)
    assert replay.status_code == 401
    async with api.storage.session() as s:
        assert not await database_contains(s, challenge)  # stored as a hash only
        actions = await audit_actions(s)
    assert "device.step_up_key.registered" in actions
    assert "session.step_up.attested" in actions
    assert "session.step_up.failed" in actions


async def test_a_stolen_credential_cannot_plant_a_key_after_enrolment(api):
    phone = await api.onboard("alice")
    await _age_registration(api, phone)
    _, resp = await _register(api, phone)
    assert resp.status_code == 401


async def test_replacing_a_key_needs_a_fresh_attestation_by_the_old_one(api):
    phone = await api.onboard("alice")
    old, _ = await _register(api, phone)
    await _age_registration(api, phone)
    _, refused = await _register(api, phone)
    assert refused.status_code == 401
    _, attested = await _attest(api, phone, old)
    assert attested.status_code == 200
    new, replaced = await _register(api, phone)
    assert replaced.status_code == 204
    # The old key no longer attests; the new one does.
    _, stale = await _attest(api, phone, old)
    assert stale.status_code == 401
    _, fresh = await _attest(api, phone, new)
    assert fresh.status_code == 200


async def test_only_a_p256_spki_is_accepted(api):
    phone = await api.onboard("alice")
    ed = ed25519.Ed25519PrivateKey.generate()
    assert (await api.client.post(KEY, json={"public_key": _spki(ed)}, headers=phone.auth)).status_code == 401
    p384 = ec.generate_private_key(ec.SECP384R1())
    assert (await api.client.post(KEY, json={"public_key": _spki(p384)}, headers=phone.auth)).status_code == 401
    assert (await api.client.post(KEY, json={"public_key": "not-a-key"}, headers=phone.auth)).status_code == 401


async def test_a_signature_by_another_key_or_over_another_message_fails(api):
    phone = await api.onboard("alice")
    key, _ = await _register(api, phone)
    _, wrong_key = await _attest(api, phone, ec.generate_private_key(ec.SECP256R1()))
    assert wrong_key.status_code == 401
    other = await api.onboard("bob")
    resp = await api.client.post(CHALLENGE, headers=phone.auth)
    challenge = resp.json()["challenge"]
    # Signed for another device id: not this device's attestation.
    _, wrong_device = await _attest(api, phone, key, challenge=challenge,
                                    message=attestation_message(device_id=other.device_id, challenge=challenge))
    assert wrong_device.status_code == 401


async def test_an_expired_challenge_is_refused(api):
    phone = await api.onboard("alice")
    key, _ = await _register(api, phone)
    challenge = (await api.client.post(CHALLENGE, headers=phone.auth)).json()["challenge"]
    async with api.storage.session() as s:
        await s.execute(update(Device).where(Device.device_id == phone.device_id)
                        .values(step_up_challenge_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await s.commit()
    _, expired = await _attest(api, phone, key, challenge=challenge)
    assert expired.status_code == 401


async def test_a_device_without_a_step_up_key_gets_no_challenge(api):
    phone = await api.onboard("alice")
    assert (await api.client.post(CHALLENGE, headers=phone.auth)).status_code == 401


async def test_a_challenge_is_bound_to_the_calling_device(api):
    alice = await api.onboard("alice")
    bob = await api.onboard("bob")
    alice_key, _ = await _register(api, alice)
    bob_key, _ = await _register(api, bob)
    challenge = (await api.client.post(CHALLENGE, headers=alice.auth)).json()["challenge"]
    # Bob presents Alice's challenge, signed by Bob's key over Bob's device id.
    _, stolen = await _attest(api, bob, bob_key, challenge=challenge)
    assert stolen.status_code == 401
