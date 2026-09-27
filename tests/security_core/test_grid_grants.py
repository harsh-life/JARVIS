"""The Android per-app grid's grants, through the real endpoints (PRD §13,
docs/23 §5.2).

The phone sends exactly `shared/android/grant_samples.json`'s request body —
a device-scoped grant narrowed to one app — with its own device id. The server
records it for that device only, lists it back in the shape the phone parses,
refuses one naming another device, and revokes it on the toggle going off.
"""

from __future__ import annotations

import json

from server.gateway.app import API_V1_PREFIX
from tests.tools.export_grant_samples import PATH

GRANTS = f"{API_V1_PREFIX}/capabilities"


def _sample_request(device_id) -> dict:
    body = json.loads(PATH.read_text(encoding="ascii"))["grant_request"]
    return {**body, "scope_id": str(device_id)}


async def test_the_grid_request_creates_a_per_app_grant_for_this_device(api):
    phone = await api.onboard("alice")
    created = await api.client.post(GRANTS, json=_sample_request(phone.device_id), headers=phone.auth)
    assert created.status_code == 201, created.text
    grant = created.json()
    assert grant["scope_type"] == "device" and grant["principal_id"] == str(phone.device_id)
    assert grant["resource_scope"] == {"package_name": "com.example.notes"}

    listed = (await api.client.get(GRANTS, headers=phone.auth)).json()["items"]
    sample_keys = set(json.loads(PATH.read_text(encoding="ascii"))["grant_list"]["items"][0])
    assert [set(item) for item in listed] == [sample_keys]  # the shape the phone parses strictly

    revoked = await api.client.delete(f"{GRANTS}/{grant['grant_id']}", headers=phone.auth)
    assert revoked.status_code == 204
    assert (await api.client.get(GRANTS, headers=phone.auth)).json()["items"] == []


async def test_a_phone_cannot_grant_for_another_device(api):
    alice = await api.onboard("alice")
    bob = await api.onboard("bob")
    refused = await api.client.post(GRANTS, json=_sample_request(bob.device_id), headers=alice.auth)
    assert refused.status_code == 403
    assert (await api.client.get(GRANTS, headers=bob.auth)).json()["items"] == []


async def test_a_grid_grant_cannot_be_scoped_by_an_unknown_key(api):
    phone = await api.onboard("alice")
    body = {**_sample_request(phone.device_id), "resource_scope": {"package": "com.example.notes"}}
    refused = await api.client.post(GRANTS, json=body, headers=phone.auth)
    assert refused.status_code == 422
