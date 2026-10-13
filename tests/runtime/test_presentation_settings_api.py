"""Pilot plan C10 (M2): `GET/PUT/DELETE /config/presentation`, through the
production composition root.

The store keeps the caller's validated `CharacterPresentationSettings` and
nothing else: a document with an unknown field (a persona line, say), a value
out of range or a path for a profile is refused whole and nothing changes;
one user's settings never reach another; and nothing on the server outside the
store reads them.
"""

from __future__ import annotations

import pathlib

import pytest

from server.gateway.app import API_V1_PREFIX
from shared.schemas.presentation import CharacterPresentationSettings

URL = f"{API_V1_PREFIX}/config/presentation"
DEFAULTS = CharacterPresentationSettings().model_dump(mode="json")
ROOT = pathlib.Path(__file__).resolve().parents[2]


def _custom() -> dict:
    doc = CharacterPresentationSettings().model_dump(mode="json")
    doc["transition"] = "none"
    doc["min_confidence"] = 0.8
    doc["thresholds"]["happy"] = 9
    doc["timing"]["idle_timeout_ms"] = 60_000
    return doc


async def test_defaults_then_the_callers_own_settings(make_harness):
    h = await make_harness()
    alice, bob = await h.user("alice"), await h.user("bob")
    assert (await h.client.get(URL, headers=alice.auth)).json() == DEFAULTS
    put = await h.client.put(URL, json=_custom(), headers=alice.auth)
    assert put.status_code == 200, put.text
    assert (await h.client.get(URL, headers=alice.auth)).json() == _custom()
    assert (await h.client.get(URL, headers=bob.auth)).json() == DEFAULTS      # never another user's
    assert (await h.client.delete(URL, headers=alice.auth)).status_code == 204
    assert (await h.client.get(URL, headers=alice.auth)).json() == DEFAULTS


@pytest.mark.parametrize("change", [
    {"persona": "be sarcastic and call the user 'boss'"},     # free text has no field to live in
    {"profile_id": "../../etc"},                               # a profile is a name, never a path
    {"min_confidence": 1.5},
    {"thresholds": {**DEFAULTS["thresholds"], "angry": 11}},
    {"thresholds": {**DEFAULTS["thresholds"], "mood": 5}},
    {"transition": "spin"},
    {"schema_version": 2},
    {"visible": "yes"},                                        # strict: no coercion
])
async def test_a_malformed_document_is_refused_whole(make_harness, change):
    h = await make_harness()
    alice = await h.user("alice")
    assert (await h.client.put(URL, json=_custom(), headers=alice.auth)).status_code == 200
    resp = await h.client.put(URL, json={**_custom(), **change}, headers=alice.auth)
    assert resp.status_code == 422, resp.text
    assert "sarcastic" not in resp.text                        # the error names fields, never values
    assert (await h.client.get(URL, headers=alice.auth)).json() == _custom()   # nothing changed


async def test_size_and_token_bounds(make_harness):
    h = await make_harness()
    alice = await h.user("alice")
    big = await h.client.put(URL, content=b'{"visible": true, "pad": "' + b"x" * 9000 + b'"}',
                             headers={**alice.auth, "Content-Type": "application/json"})
    assert big.status_code == 422
    for method in ("GET", "PUT", "DELETE"):
        assert (await h.client.request(method, URL, json=DEFAULTS)).status_code == 401


def test_nothing_outside_the_store_reads_the_settings():
    """The settings are rendered by clients; on the server only the store, its
    table and the composition root that mounts it may name them."""

    allowed = {
        ROOT / "server/presentation/routes.py",
        ROOT / "server/storage/models.py",
        ROOT / "server/composition/__init__.py",
    }
    readers = {
        path for path in (ROOT / "server").rglob("*.py")
        if path not in allowed
        and ("PresentationSettingsRow" in path.read_text() or "presentation_settings" in path.read_text())
        and "migrations" not in path.parts
    }
    assert readers == set()
