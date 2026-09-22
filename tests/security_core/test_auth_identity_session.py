"""03's acceptance hooks — AUTH-T1..AUTH-T10.

These drive the real HTTP surface end to end: OIDC start → callback → device
registration → token → authenticated call. The OIDC provider mints genuine RS256
id_tokens with a locally generated key, so each negative case fails for the same
reason it would against Google rather than because a stub said no.
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from server.auth.device import build_device_proof
from server.auth.errors import InvalidIdToken
from server.auth.oidc import SCOPES, validate_id_token
from server.gateway.app import API_V1_PREFIX
from server.storage.models import AuditEvent, Session, User
from tests.support import OMIT

pytestmark = pytest.mark.asyncio


# ── AUTH-T1: a tampered id_token is rejected, and no user is created ────────


async def test_auth_t1_forged_signature_is_rejected_and_creates_no_user(api):
    """AUTH-T1 (03 §2.3 check 1) — an id_token signed by a key the provider does
    not publish is rejected with 401, and no `User` row appears.

    "no user created" is the half that is easy to get wrong: a flow that maps the
    subject before validating would leave a real account behind after a forged
    login.
    """

    api.provider.sign_with_foreign_key = True

    start = await api.client.get(f"{API_V1_PREFIX}/auth/oidc/start")
    state = parse_qs(urlparse(start.json()["redirect_url"]).query)["state"][0]

    resp = await api.client.get(
        f"{API_V1_PREFIX}/auth/oidc/callback", params={"code": "c", "state": state}
    )

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthenticated"

    async with api.storage.session() as session:
        users = (await session.execute(select(User))).scalars().all()
        assert users == []


@pytest.mark.parametrize(
    ("claim_overrides", "label"),
    [
        ({"iss": "https://accounts.evil.test"}, "issuer"),
        ({"aud": "some-other-app.apps.googleusercontent.example"}, "audience"),
        ({"exp": int(time.time()) - 3600}, "expiry"),
        ({"nonce": "a-nonce-from-a-different-login"}, "nonce"),
        ({"nonce": OMIT}, "missing nonce"),
        ({"sub": OMIT}, "missing subject"),
    ],
)
async def test_auth_t1_each_locked_claim_check_rejects(api, claim_overrides, label):
    """AUTH-T1 across 03 §2.3's checks 2, 3, 4 and 5, one at a time.

    Parametrized rather than combined so a regression names *which* check
    regressed — a single "bad token rejected" test would still pass if only one
    of the five were left.
    """

    api.provider.claim_overrides = claim_overrides

    start = await api.client.get(f"{API_V1_PREFIX}/auth/oidc/start")
    state = parse_qs(urlparse(start.json()["redirect_url"]).query)["state"][0]

    resp = await api.client.get(
        f"{API_V1_PREFIX}/auth/oidc/callback", params={"code": "c", "state": state}
    )
    assert resp.status_code == 401, f"{label} check did not reject"

    async with api.storage.session() as session:
        assert (await session.execute(select(User))).scalars().all() == []


async def test_auth_t1_unsigned_token_is_rejected(api):
    """Algorithm confusion: `alg: none` must be refused because RS256 is the only
    accepted algorithm, not because a library default happened to catch it."""

    import jwt as pyjwt

    unsigned = pyjwt.encode({"iss": api.provider.issuer, "sub": "x"}, key="", algorithm="none")

    with pytest.raises(InvalidIdToken):
        validate_id_token(
            unsigned,
            issuer=api.provider.issuer,
            audience=api.provider.client_id,
            expected_nonce="n",
            jwks=await api.provider.jwks(),
        )


async def test_oidc_requests_only_identity_scopes():
    """AUTH-002/003 (03 §2.2 `[LOCKED]`) — only `openid`, `email`, `profile`.

    A Gmail/Drive/Calendar scope creeping into login would silently convert a
    "sign in" into a grant of access to the user's Google data.

    Asserted against the **production** `GoogleOIDCProvider`, not the suite's
    local provider: the redirect URL sent to Google is what determines what the
    user is asked to consent to, so a test that checked the local provider's
    hand-built URL would prove nothing about the real one.
    """

    from server.auth.oidc import GoogleOIDCProvider

    assert set(SCOPES) == {"openid", "email", "profile"}

    provider = GoogleOIDCProvider(client_id="client-id-under-test")
    url = provider.authorization_url(
        state="s", nonce="n", code_challenge="c", redirect_uri="https://host.invalid/cb"
    )
    query = parse_qs(urlparse(url).query)

    assert set(query["scope"][0].split()) == {"openid", "email", "profile"}
    assert not any(
        forbidden in query["scope"][0]
        for forbidden in ("gmail", "drive", "calendar", "contacts", "photos")
    )
    # PKCE, with the S256 method — never `plain` (03 §2.3 check 7).
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == ["c"]
    # A public client (03 §2): no client_secret is sent, and none can be configured.
    assert "client_secret" not in query


async def test_oidc_start_returns_a_pkce_bound_redirect(api):
    """The flow half: `/auth/oidc/start` returns a redirect carrying a state the
    server recorded, and the PKCE verifier behind it never leaves the server."""

    start = await api.client.get(f"{API_V1_PREFIX}/auth/oidc/start")
    assert start.status_code == 200
    query = parse_qs(urlparse(start.json()["redirect_url"]).query)
    state = query["state"][0]

    async with api.storage.session() as session:
        from server.storage.models import OIDCLoginState

        row = await session.get(OIDCLoginState, state)
        assert row is not None
        assert row.used_at is None
        # The verifier is server-side only — the client receives the challenge.
        assert row.code_verifier not in start.text


# ── AUTH-T2: replay of a used state / bootstrap token ───────────────────────


async def test_auth_t2_replaying_a_used_state_fails(api):
    """AUTH-T2 (03 §2.3 check 6) — `state` is single-use.

    The first callback succeeds; replaying the identical callback fails. This is
    both the CSRF defense and what stops a captured redirect being reused.
    """

    start = await api.client.get(f"{API_V1_PREFIX}/auth/oidc/start")
    state = parse_qs(urlparse(start.json()["redirect_url"]).query)["state"][0]

    first = await api.client.get(
        f"{API_V1_PREFIX}/auth/oidc/callback", params={"code": "c", "state": state}
    )
    assert first.status_code == 200

    replay = await api.client.get(
        f"{API_V1_PREFIX}/auth/oidc/callback", params={"code": "c", "state": state}
    )
    assert replay.status_code == 401


async def test_auth_t2_unknown_state_fails(api):
    resp = await api.client.get(
        f"{API_V1_PREFIX}/auth/oidc/callback",
        params={"code": "c", "state": "a-state-nobody-issued"},
    )
    assert resp.status_code == 401


async def test_auth_t2_replaying_a_used_bootstrap_token_fails(api):
    """AUTH-T2 (03 §3.2) — a bootstrap token registers exactly one device."""

    bootstrap = await api.oidc_login()

    first = await api.client.post(
        f"{API_V1_PREFIX}/devices",
        json={"platform": "android"},
        headers={"Authorization": f"Bearer {bootstrap}"},
    )
    assert first.status_code == 201

    second = await api.client.post(
        f"{API_V1_PREFIX}/devices",
        json={"platform": "android"},
        headers={"Authorization": f"Bearer {bootstrap}"},
    )
    assert second.status_code == 401


# ── AUTH-T3: subject-keyed identity survives an email change ────────────────


async def test_auth_t3_changing_google_email_keeps_the_same_user_id(api):
    """AUTH-T3 (03 §2.4 `[LOCKED]`) — the stable key is `(iss, sub)`, never email.

    Email is mutable; a user who changes their Google address must remain the same
    Hypermind user, with the same graphs and the same resources.
    """

    await api.oidc_login()
    async with api.storage.session() as session:
        first = (await session.execute(select(User))).scalars().one()
        original_id = first.user_id

    api.provider.email = "renamed@example.test"
    api.provider.display_name = "Renamed Person"
    await api.oidc_login()

    async with api.storage.session() as session:
        users = (await session.execute(select(User))).scalars().all()
        assert len(users) == 1, "an email change created a second account"
        assert users[0].user_id == original_id
        # The label follows; identity does not.
        assert users[0].display_name == "Renamed Person"


async def test_a_different_subject_creates_a_different_user(api):
    """The converse: a genuinely different Google account is a different user."""

    await api.oidc_login()
    api.provider.subject = "google-subject-000002"
    await api.oidc_login()

    async with api.storage.session() as session:
        assert len((await session.execute(select(User))).scalars().all()) == 2


# ── AUTH-T4: the device credential is delivered exactly once ────────────────


async def test_auth_t4_device_credential_appears_once_and_never_again(api):
    """AUTH-T4 (SECRET-004, 02 §3) — the raw credential is returned at
    registration and appears in no later response, audit record, or database
    column.

    The credential is an Ed25519 *private* key; the server keeps only the public
    verifier, so there is nothing server-side that could re-emit it even by
    mistake.
    """

    bootstrap = await api.oidc_login()
    device_id, credential = await api.register_device(bootstrap)
    assert credential

    token_resp = await api.issue_token(device_id, credential)
    assert token_resp.status_code == 200
    assert credential not in token_resp.text

    auth = {"Authorization": f"Bearer {token_resp.json()['access_token']}"}
    for path in ("/graphs", "/capabilities"):
        resp = await api.client.get(f"{API_V1_PREFIX}{path}", headers=auth)
        assert credential not in resp.text

    async with api.storage.session() as session:
        events = (await session.execute(select(AuditEvent))).scalars().all()
        assert events, "registration emitted no audit events at all"
        for event in events:
            assert credential not in event.action
            assert credential not in event.resource

        # And nowhere in the database as a whole (SS-T1's discipline applied to
        # the device credential specifically).
        from tests.security_core.helpers import database_contains

        assert not await database_contains(session, credential)


# ── AUTH-T5 / T6: revocation and rotation ──────────────────────────────────


async def test_auth_t5_revoked_device_cannot_refresh(api):
    """AUTH-T5 (03 §4.4) — revocation is immediate: the next refresh fails."""

    onboarded = await api.onboard()

    revoke = await api.client.delete(
        f"{API_V1_PREFIX}/devices/{onboarded.device_id}", headers=onboarded.auth
    )
    assert revoke.status_code == 204

    refreshed = await api.issue_token(onboarded.device_id, onboarded.credential)
    assert refreshed.status_code == 401


async def test_revoking_a_device_also_kills_its_live_access_tokens(api):
    """03 §4.4 — the exposure window after revocation is closed rather than
    TTL-bounded, since the opaque-token design makes that free."""

    onboarded = await api.onboard()

    before = await api.client.get(f"{API_V1_PREFIX}/graphs", headers=onboarded.auth)
    assert before.status_code == 200

    await api.client.delete(
        f"{API_V1_PREFIX}/devices/{onboarded.device_id}", headers=onboarded.auth
    )

    after = await api.client.get(f"{API_V1_PREFIX}/graphs", headers=onboarded.auth)
    assert after.status_code == 401


async def test_auth_t5_a_user_cannot_revoke_another_users_device(api):
    """Device ids must not be probeable across users (04 §7)."""

    alice = await api.onboard(subject="alice-subject")
    bob = await api.onboard(subject="bob-subject")

    resp = await api.client.delete(
        f"{API_V1_PREFIX}/devices/{alice.device_id}", headers=bob.auth
    )
    assert resp.status_code == 404

    # Alice's device still works — the attempt changed nothing.
    assert (await api.issue_token(alice.device_id, alice.credential)).status_code == 200


async def test_auth_t6_rotation_invalidates_the_old_credential_with_no_dual_valid_window(
    api,
):
    """AUTH-T6 (03 §4.3 `[LOCKED]`) — rotation has no window where both work.

    Asserted in the strong direction: immediately after rotation the *old*
    credential fails and the *new* one succeeds, with no refresh, sleep, or cache
    expiry in between.
    """

    onboarded = await api.onboard()

    rotate = await api.client.post(
        f"{API_V1_PREFIX}/devices/{onboarded.device_id}/rotate", headers=onboarded.auth
    )
    assert rotate.status_code == 200, rotate.text
    new_credential = rotate.json()["device_credential"]
    assert new_credential != onboarded.credential

    old = await api.issue_token(onboarded.device_id, onboarded.credential)
    assert old.status_code == 401

    new = await api.issue_token(onboarded.device_id, new_credential)
    assert new.status_code == 200


# ── AUTH-T7: body-asserted identity is ignored (PHONE-003) ─────────────────


async def test_auth_t7_body_asserted_user_id_is_ignored(api):
    """AUTH-T7 (PHONE-003, 02 §1.1) — identity comes from the token.

    A request whose body asserts another `user_id` is served as the *token's*
    user. Asserted structurally as well as behaviourally: the endpoint's request
    model forbids extra fields, so the claim cannot even be submitted — which is
    a stronger guarantee than ignoring it.
    """

    alice = await api.onboard(subject="alice-subject")
    bob = await api.onboard(subject="bob-subject")

    async with api.storage.session() as session:
        users = {u.oidc_subject: u.user_id for u in (await session.execute(select(User))).scalars()}

    # Alice creates a graph while asserting she is Bob.
    resp = await api.client.post(
        f"{API_V1_PREFIX}/graphs",
        json={
            "name": "graph",
            "type": "shared",
            "user_id": str(users["bob-subject"]),
        },
        headers=alice.auth,
    )
    # extra="forbid" on the request model: the claim is not merely ignored, it is
    # unrepresentable (02 §1.6 validation).
    assert resp.status_code == 422

    # And the legitimate request is attributed to the token's user, not to Bob.
    ok = await api.client.post(
        f"{API_V1_PREFIX}/graphs",
        json={"name": "graph", "type": "shared"},
        headers=alice.auth,
    )
    assert ok.status_code == 201
    assert ok.json()["owner_user_id"] == str(users["alice-subject"])

    # Bob sees none of it.
    bob_graphs = await api.client.get(f"{API_V1_PREFIX}/graphs", headers=bob.auth)
    assert bob_graphs.json()["items"] == []


async def test_auth_t7_a_graph_id_the_caller_is_not_in_is_a_claim_not_a_grant(api):
    """The same rule for `graph_id` (04 §0): a body-supplied graph is checked,
    and a non-member gets 404 — never the graph."""

    alice = await api.onboard(subject="alice-subject")
    bob = await api.onboard(subject="bob-subject")

    created = await api.client.post(
        f"{API_V1_PREFIX}/graphs",
        json={"name": "alice's graph", "type": "shared"},
        headers=alice.auth,
    )
    graph_id = created.json()["graph_id"]

    resp = await api.client.post(
        f"{API_V1_PREFIX}/sessions/active-graph",
        json={"graph_id": graph_id},
        headers=bob.auth,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


# ── AUTH-T8: expiry tells the client to refresh ────────────────────────────


async def test_auth_t8_expired_access_token_yields_token_expired(api):
    """AUTH-T8 (03 §5.3/§5.4, 02 §1.7) — an expired token is `token_expired`, and
    the client recovers by refreshing rather than by a full Google login."""

    onboarded = await api.onboard()

    async with api.storage.session() as session:
        from server.auth.repository import utcnow
        from server.storage.models import AccessToken

        rows = (await session.execute(select(AccessToken))).scalars().all()
        for row in rows:
            row.expires_at = utcnow() - timedelta(seconds=1)
        await session.commit()

    expired = await api.client.get(f"{API_V1_PREFIX}/graphs", headers=onboarded.auth)
    assert expired.status_code == 401
    assert expired.json()["error"]["code"] == "token_expired"

    # The documented recovery: refresh with the device credential, no re-login.
    refreshed = await api.issue_token(onboarded.device_id, onboarded.credential)
    assert refreshed.status_code == 200


async def test_unknown_and_missing_bearer_tokens_are_rejected(api):
    for headers in (
        {},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer not-a-real-token"},
        {"Authorization": "Basic abc"},
    ):
        resp = await api.client.get(f"{API_V1_PREFIX}/graphs", headers=headers)
        assert resp.status_code == 401, headers


async def test_logout_revokes_the_token_immediately(api):
    """03 §6 — logout ends the session; the device credential survives."""

    onboarded = await api.onboard()

    assert (await api.client.get(f"{API_V1_PREFIX}/graphs", headers=onboarded.auth)).status_code == 200

    logout = await api.client.post(f"{API_V1_PREFIX}/sessions/logout", headers=onboarded.auth)
    assert logout.status_code == 204

    assert (await api.client.get(f"{API_V1_PREFIX}/graphs", headers=onboarded.auth)).status_code == 401

    # Not a full sign-out: the device can still get a new token (03 §6).
    assert (await api.issue_token(onboarded.device_id, onboarded.credential)).status_code == 200


# ── AUTH-T9: step-up on a sensitive operation ──────────────────────────────


async def test_auth_t9_rotation_requires_step_up_even_with_a_valid_token(api):
    """AUTH-T9 (03 §5.5, SESSION-003) — a valid access token is not sufficient
    for a sensitive operation.

    The token here is genuinely valid and unexpired; only its *age* exceeds the
    step-up window. Because a token is only ever issued against a freshly verified
    device-credential proof, its `issued_at` is the last re-attestation time.
    """

    onboarded = await api.onboard()

    async with api.storage.session() as session:
        from server.auth.repository import utcnow
        from server.auth.sessions import STEP_UP_WINDOW
        from server.storage.models import AccessToken

        for row in (await session.execute(select(AccessToken))).scalars().all():
            row.issued_at = utcnow() - STEP_UP_WINDOW - timedelta(minutes=1)
        await session.commit()

    stale = await api.client.post(
        f"{API_V1_PREFIX}/devices/{onboarded.device_id}/rotate", headers=onboarded.auth
    )
    assert stale.status_code == 401
    assert stale.json()["error"]["details"]["step_up_required"] is True

    # Re-attesting with the device credential produces a fresh token that passes.
    fresh = await api.issue_token(onboarded.device_id, onboarded.credential)
    rotated = await api.client.post(
        f"{API_V1_PREFIX}/devices/{onboarded.device_id}/rotate",
        headers={"Authorization": f"Bearer {fresh.json()['access_token']}"},
    )
    assert rotated.status_code == 200


async def test_revocation_is_deliberately_not_step_up_gated(api):
    """03 §7's stolen-device row makes revocation the mitigation, so requiring
    fresh re-attestation to revoke would block the one action that helps."""

    onboarded = await api.onboard()

    async with api.storage.session() as session:
        from server.auth.repository import utcnow
        from server.auth.sessions import STEP_UP_WINDOW
        from server.storage.models import AccessToken

        for row in (await session.execute(select(AccessToken))).scalars().all():
            row.issued_at = utcnow() - STEP_UP_WINDOW - timedelta(minutes=1)
        await session.commit()

    resp = await api.client.delete(
        f"{API_V1_PREFIX}/devices/{onboarded.device_id}", headers=onboarded.auth
    )
    assert resp.status_code == 204


# ── AUTH-T10: Session.user_id == Device.user_id ────────────────────────────


async def test_auth_t10_session_user_always_matches_device_user(api):
    """AUTH-T10 (03 §5.1, 01 §2.3) — a cross-user session is impossible.

    Checked against every session the flow produced, and reinforced by
    `new_session_for_device` being the only constructor used: the mismatch is
    unconstructible rather than merely absent.
    """

    await api.onboard(subject="alice-subject")
    await api.onboard(subject="bob-subject")

    async with api.storage.session() as session:
        from server.storage.models import Device

        devices = {d.device_id: d.user_id for d in (await session.execute(select(Device))).scalars()}
        sessions = (await session.execute(select(Session))).scalars().all()
        assert sessions
        for row in sessions:
            assert row.user_id == devices[row.device_id]


async def test_auth_t10_mismatched_session_is_rejected_at_resolution(api):
    """Defense in depth for AUTH-T10.

    `new_session_for_device` makes the mismatch unconstructible through the
    normal path, so this test writes it directly into the database — the one way
    it could exist — and asserts token resolution still refuses rather than
    handing back a principal carrying the tampered user id. Without the
    re-assertion in `resolve_principal`, this would silently serve Alice's device
    as Bob.
    """

    from server.storage.models import AccessToken

    alice = await api.onboard(subject="alice-subject")
    bob = await api.onboard(subject="bob-subject")

    async with api.storage.session() as session:
        token_row = (
            await session.execute(
                select(AccessToken).where(AccessToken.device_id == alice.device_id)
            )
        ).scalars().one()
        session_row = await session.get(Session, token_row.session_id)
        bob_user = (
            await session.execute(select(User).where(User.oidc_subject == "bob-subject"))
        ).scalars().one()
        session_row.user_id = bob_user.user_id
        await session.commit()

    resp = await api.client.get(f"{API_V1_PREFIX}/graphs", headers=alice.auth)
    assert resp.status_code == 401

    # Bob's own session is untouched by the tamper.
    assert (await api.client.get(f"{API_V1_PREFIX}/graphs", headers=bob.auth)).status_code == 200


# ── device proof replay (03 §7) ────────────────────────────────────────────


async def test_device_proof_cannot_be_replayed(api):
    """03 §4/§7 — a captured proof is single-use inside its freshness window."""

    bootstrap = await api.oidc_login()
    device_id, credential = await api.register_device(bootstrap)

    proof = build_device_proof(device_id=device_id, device_credential=credential)

    first = await api.client.post(
        f"{API_V1_PREFIX}/sessions/token", json={"device_credential": proof}
    )
    assert first.status_code == 200

    replay = await api.client.post(
        f"{API_V1_PREFIX}/sessions/token", json={"device_credential": proof}
    )
    assert replay.status_code == 401


async def test_stale_device_proof_is_rejected(api):
    """A proof outside the freshness window is refused even though its signature
    is valid, so a long-captured proof does not stay usable."""

    from server.auth.device import PROOF_FRESHNESS

    bootstrap = await api.oidc_login()
    device_id, credential = await api.register_device(bootstrap)

    stale = build_device_proof(
        device_id=device_id,
        device_credential=credential,
        issued_at=int(time.time()) - int(PROOF_FRESHNESS.total_seconds()) - 60,
    )
    resp = await api.client.post(
        f"{API_V1_PREFIX}/sessions/token", json={"device_credential": stale}
    )
    assert resp.status_code == 401


async def test_forged_device_proof_is_rejected(api):
    """A proof signed with an attacker's own key does not verify against the
    stored public verifier."""

    from server.secrets.crypto import generate_key
    import base64

    bootstrap = await api.oidc_login()
    device_id, _credential = await api.register_device(bootstrap)

    attacker_credential = base64.urlsafe_b64encode(generate_key(32)).rstrip(b"=").decode()
    forged = build_device_proof(device_id=device_id, device_credential=attacker_credential)

    resp = await api.client.post(
        f"{API_V1_PREFIX}/sessions/token", json={"device_credential": forged}
    )
    assert resp.status_code == 401


async def test_malformed_device_proofs_are_rejected(api):
    for value in ("", "garbage", "v1.not-a-uuid.n.0.sig", "v2." + "a." * 4):
        resp = await api.client.post(
            f"{API_V1_PREFIX}/sessions/token", json={"device_credential": value}
        )
        assert resp.status_code == 401, value


async def test_a_proof_for_another_users_device_does_not_grant_their_identity(api):
    """SEC-B: presenting a valid proof yields the identity of *that device's*
    owner, taken from the Device row — never an identity the caller chose."""

    alice = await api.onboard(subject="alice-subject")
    bob = await api.onboard(subject="bob-subject")

    # Bob signs a proof for Alice's device id with his own key: no match.
    forged = build_device_proof(device_id=alice.device_id, device_credential=bob.credential)
    resp = await api.client.post(
        f"{API_V1_PREFIX}/sessions/token", json={"device_credential": forged}
    )
    assert resp.status_code == 401
