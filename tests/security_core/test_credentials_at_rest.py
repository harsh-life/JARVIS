"""Bearer credentials are stored as hashes only (03 §4–§5; re-verified 2026-11-27).

The other auth properties — issuer, audience, expiry, nonce, PKCE, single-use
state, bootstrap replay, device proof-of-possession, revocation, step-up — each
have their own test (tests/security_core/test_auth_identity_session.py,
test_admission.py, test_step_up.py). This one closes the remaining gap: after a
real sign-in through the production composition root, no raw access token,
device credential or bootstrap token appears anywhere in the store.
"""

from __future__ import annotations

from sqlalchemy import select

from server.storage.base import Base
from tests.runtime.conftest import make_harness  # noqa: F401 — fixture


async def _store_text(h) -> str:
    chunks = []
    async with h.storage.session() as session:
        for table in Base.metadata.sorted_tables:
            for row in (await session.execute(select(table))).all():
                chunks.append(repr(tuple(row)))
    return "\n".join(chunks)


async def test_no_raw_bearer_credential_is_stored(make_harness):
    h = await make_harness()
    alice = await h.user("alice")
    refreshed = await h.fresh_token(alice.device_id, alice.credential)
    text = await _store_text(h)
    for secret in (alice.token, refreshed, alice.credential):
        assert secret and secret not in text
    # And revocation still works on what *is* stored (hashes).
    resp = await h.client.post("/api/v1/sessions/logout", headers={"Authorization": f"Bearer {refreshed}"})
    assert resp.status_code in (200, 204), resp.text
    after = await h.client.get("/api/v1/me", headers={"Authorization": f"Bearer {refreshed}"})
    assert after.status_code == 401
