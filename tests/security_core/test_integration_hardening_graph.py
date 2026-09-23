"""Integration-hardening regressions for the graph surface.

Two cross-user paths the per-branch suites did not cover because each one only
appears when two users share one server:

* an `Idempotency-Key` replay returned another user's stored response without
  running authorization (02 §1.4 × 04 §7);
* an owner could enrol a user who never asked, or grow a `private` graph past
  its single membership (GRAPH-008, 04 §4.1/§4.2).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from server.gateway.app import API_V1_PREFIX
from server.graph.service import GraphOperationRefused
from server.storage.models import User
from shared.schemas.enums import GraphType, MembershipRole


async def _user_id(api, subject: str) -> uuid.UUID:
    async with api.storage.session() as session:
        return (
            await session.execute(select(User.user_id).where(User.oidc_subject == subject))
        ).scalar_one()


# ── idempotency keys are per-user ─────────────────────────────────────────


async def test_another_users_idempotency_key_does_not_replay_their_graph(api):
    alice = await api.onboard(subject="alice-subject")
    bob = await api.onboard(subject="bob-subject")
    body = {"name": "family", "type": "shared"}
    key = {"Idempotency-Key": "client-counter-1"}

    alice_resp = await api.client.post(
        f"{API_V1_PREFIX}/graphs", json=body, headers={**alice.auth, **key}
    )
    assert alice_resp.status_code == 201
    alice_graph = alice_resp.json()

    bob_resp = await api.client.post(
        f"{API_V1_PREFIX}/graphs", json=body, headers={**bob.auth, **key}
    )
    assert bob_resp.status_code == 201
    bob_graph = bob_resp.json()

    # Bob gets his own new graph, never Alice's stored response.
    assert bob_graph["graph_id"] != alice_graph["graph_id"]
    assert bob_graph["owner_user_id"] == str(await _user_id(api, "bob-subject"))

    # And Alice's own retry still replays her original.
    retry = await api.client.post(
        f"{API_V1_PREFIX}/graphs", json=body, headers={**alice.auth, **key}
    )
    assert retry.json()["graph_id"] == alice_graph["graph_id"]


async def test_membership_approval_replay_does_not_bypass_authorization(api):
    alice = await api.onboard(subject="alice-subject")
    bob = await api.onboard(subject="bob-subject")
    outsider = await api.onboard(subject="outsider-subject")

    graph_id = (
        await api.client.post(
            f"{API_V1_PREFIX}/graphs", json={"name": "g", "type": "shared"}, headers=alice.auth
        )
    ).json()["graph_id"]
    await api.client.post(
        f"{API_V1_PREFIX}/graphs/{graph_id}/access-requests", json={}, headers=bob.auth
    )
    bob_id = await _user_id(api, "bob-subject")
    body = {"user_id": str(bob_id), "role": "member"}
    key = {"Idempotency-Key": "approve-1"}

    approved = await api.client.post(
        f"{API_V1_PREFIX}/graphs/{graph_id}/members", json=body, headers={**alice.auth, **key}
    )
    assert approved.status_code == 201

    # The outsider replays the owner's exact request and key: it must run the
    # owner-only check as the outsider (404, 04 §7), not return the stored row.
    replay = await api.client.post(
        f"{API_V1_PREFIX}/graphs/{graph_id}/members", json=body, headers={**outsider.auth, **key}
    )
    assert replay.status_code == 404
    assert "membership_id" not in replay.text


async def test_overlong_idempotency_key_is_refused(api):
    alice = await api.onboard(subject="alice-subject")
    resp = await api.client.post(
        f"{API_V1_PREFIX}/graphs",
        json={"name": "g", "type": "shared"},
        headers={**alice.auth, "Idempotency-Key": "k" * 201},
    )
    assert resp.status_code == 422


# ── membership is approval of the joiner's own request ────────────────────


async def test_owner_cannot_enrol_a_user_who_did_not_ask(db, audit, graph_service, world):
    with pytest.raises(GraphOperationRefused) as excinfo:
        await graph_service.approve_member(
            db,
            graph_id=world.graph_id,
            approver_user_id=world.alice.user_id,
            user_id=world.outsider.user_id,
            role=MembershipRole.MEMBER,
            audit=audit,
        )
    assert excinfo.value.reason == "no_pending_access_request"


async def test_a_private_graph_never_gains_a_second_member(db, audit, graph_service, world):
    private = await graph_service.create_graph(
        db,
        name="just me",
        graph_type=GraphType.PRIVATE,
        creator_user_id=world.alice.user_id,
        audit=audit,
    )
    with pytest.raises(GraphOperationRefused) as excinfo:
        await graph_service.approve_member(
            db,
            graph_id=private.graph_id,
            approver_user_id=world.alice.user_id,
            user_id=world.bob.user_id,
            role=MembershipRole.MEMBER,
            audit=audit,
        )
    assert excinfo.value.reason == "private_graph_not_joinable"


async def test_http_approval_without_a_request_is_refused(api):
    alice = await api.onboard(subject="alice-subject")
    await api.onboard(subject="bob-subject")
    graph_id = (
        await api.client.post(
            f"{API_V1_PREFIX}/graphs", json={"name": "g", "type": "shared"}, headers=alice.auth
        )
    ).json()["graph_id"]

    resp = await api.client.post(
        f"{API_V1_PREFIX}/graphs/{graph_id}/members",
        json={"user_id": str(await _user_id(api, "bob-subject")), "role": "member"},
        headers=alice.auth,
    )
    assert resp.status_code == 403
