"""Graph lifecycle, visibility changes, and the audit path (04 §4–§6, §17 of the
security-core scope).

RAUTH-005's "never a side effect" and RAUTH V2's "always audited" are properties
of the *whole codebase*, not of one function, so this file includes a check that
`change_visibility` is the only writer of a `visibility` field anywhere in
`server/`.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from server.gateway.app import API_V1_PREFIX
from server.graph.service import GraphOperationRefused
from server.storage.models import AuditEvent, GraphAccessRequest, GraphMembership
from shared.schemas.enums import GraphType, MembershipRole, Visibility
from tests.security_core.helpers import audit_actions

pytestmark = pytest.mark.asyncio


# ── 04 §4.1 create ─────────────────────────────────────────────────────────


async def test_creating_a_graph_makes_the_creator_an_owner_with_a_membership_row(
    db, audit, graph_service, world
):
    """GRAPH-007. The owner's membership row is created in the same transaction:
    a graph whose owner had no membership would fail D1 for its own owner."""

    graph = await graph_service.create_graph(
        db,
        name="new graph",
        graph_type=GraphType.PRIVATE,
        creator_user_id=world.alice.user_id,
        audit=audit,
    )

    memberships = (
        await db.execute(
            select(GraphMembership).where(GraphMembership.graph_id == graph.graph_id)
        )
    ).scalars().all()
    assert len(memberships) == 1
    assert memberships[0].user_id == world.alice.user_id
    assert memberships[0].role is MembershipRole.OWNER
    assert graph.owner_user_id == world.alice.user_id


# ── 04 §4.2 request → approval ─────────────────────────────────────────────


async def test_an_access_request_grants_nothing_by_itself(
    db, audit, graph_service, graph_repository, world
):
    """04 §4.2 `[LOCKED]` — membership is created only by an owner's explicit
    approval, "never self-service, never by the requester asserting it"."""

    request = await graph_service.request_access(
        db,
        graph_id=world.graph_id,
        requester_user_id=world.outsider.user_id,
        message="let me in",
        audit=audit,
    )
    assert request.status == "pending"

    assert not await graph_repository.is_active_member(
        db, graph_id=world.graph_id, user_id=world.outsider.user_id
    )


async def test_approval_creates_the_membership_and_resolves_the_request(
    db, audit, graph_service, world
):
    await graph_service.request_access(
        db,
        graph_id=world.graph_id,
        requester_user_id=world.outsider.user_id,
        message=None,
        audit=audit,
    )
    await graph_service.approve_member(
        db,
        graph_id=world.graph_id,
        approver_user_id=world.alice.user_id,
        user_id=world.outsider.user_id,
        role=MembershipRole.MEMBER,
        audit=audit,
    )

    rows = (
        await db.execute(
            select(GraphAccessRequest).where(GraphAccessRequest.user_id == world.outsider.user_id)
        )
    ).scalars().all()
    assert [r.status for r in rows] == ["approved"]
    assert rows[0].decided_by == world.alice.user_id


async def test_a_private_graph_is_not_joinable(db, audit, graph_service, world):
    """04 §4.1 — "a `private` graph has exactly one membership (its owner)".

    Surfaced as not-found rather than a distinguishable refusal: saying "that graph
    is private" would confirm it exists (04 §7).
    """

    private = await graph_service.create_graph(
        db,
        name="alice's private graph",
        graph_type=GraphType.PRIVATE,
        creator_user_id=world.alice.user_id,
        audit=audit,
    )

    with pytest.raises(GraphOperationRefused) as excinfo:
        await graph_service.request_access(
            db,
            graph_id=private.graph_id,
            requester_user_id=world.bob.user_id,
            message=None,
            audit=audit,
        )
    assert excinfo.value.surface_as_not_found


async def test_approval_cannot_create_a_second_owner(db, audit, graph_service, world):
    """04 §6 `[LOCKED]` — "a graph always has exactly one owner"."""

    with pytest.raises(GraphOperationRefused) as excinfo:
        await graph_service.approve_member(
            db,
            graph_id=world.graph_id,
            approver_user_id=world.alice.user_id,
            user_id=world.outsider.user_id,
            role=MembershipRole.OWNER,
            audit=audit,
        )
    assert excinfo.value.reason == "single_owner_invariant"


async def test_a_duplicate_approval_is_refused(db, audit, graph_service, world):
    with pytest.raises(GraphOperationRefused) as excinfo:
        await graph_service.approve_member(
            db,
            graph_id=world.graph_id,
            approver_user_id=world.alice.user_id,
            user_id=world.bob.user_id,
            role=MembershipRole.MEMBER,
            audit=audit,
        )
    assert excinfo.value.reason == "already_a_member"


# ── 04 §4.3 leave / revoke ─────────────────────────────────────────────────


async def test_a_member_may_leave(db, audit, graph_service, graph_repository, world):
    await graph_service.revoke_membership(
        db,
        graph_id=world.graph_id,
        actor_user_id=world.bob.user_id,
        target_user_id=world.bob.user_id,
        audit=audit,
    )
    assert not await graph_repository.is_active_member(
        db, graph_id=world.graph_id, user_id=world.bob.user_id
    )


async def test_a_member_cannot_revoke_another_member(db, audit, graph_service, world):
    await graph_service.request_access(
        db,
        graph_id=world.graph_id,
        requester_user_id=world.outsider.user_id,
        message=None,
        audit=audit,
    )
    await graph_service.approve_member(
        db,
        graph_id=world.graph_id,
        approver_user_id=world.alice.user_id,
        user_id=world.outsider.user_id,
        role=MembershipRole.MEMBER,
        audit=audit,
    )

    with pytest.raises(GraphOperationRefused) as excinfo:
        await graph_service.revoke_membership(
            db,
            graph_id=world.graph_id,
            actor_user_id=world.bob.user_id,
            target_user_id=world.outsider.user_id,
            audit=audit,
        )
    assert excinfo.value.reason == "owner_only"


async def test_the_graph_owner_cannot_be_revoked(db, audit, graph_service, world):
    """Revoking the owner would leave a graph with zero owners, breaking 04 §6.
    Ownership is transferred; deleting the graph is the owner's exit."""

    with pytest.raises(GraphOperationRefused) as excinfo:
        await graph_service.revoke_membership(
            db,
            graph_id=world.graph_id,
            actor_user_id=world.alice.user_id,
            target_user_id=world.alice.user_id,
            audit=audit,
        )
    assert excinfo.value.reason == "single_owner_invariant"


async def test_a_non_member_revocation_attempt_is_not_found(db, audit, graph_service, world):
    with pytest.raises(GraphOperationRefused) as excinfo:
        await graph_service.revoke_membership(
            db,
            graph_id=world.graph_id,
            actor_user_id=world.outsider.user_id,
            target_user_id=world.bob.user_id,
            audit=audit,
        )
    assert excinfo.value.surface_as_not_found


# ── 04 §6 ownership transfer ───────────────────────────────────────────────


async def test_transfer_is_atomic_and_leaves_exactly_one_owner(
    db, audit, graph_service, graph_repository, world
):
    """04 §6 — "a graph always has exactly one owner; transfer is atomic (no
    zero-owner or two-owner window)"."""

    await graph_service.transfer_ownership(
        db,
        graph_id=world.graph_id,
        current_owner_user_id=world.alice.user_id,
        new_owner_user_id=world.bob.user_id,
        audit=audit,
    )

    memberships = await graph_repository.active_memberships(db, graph_id=world.graph_id)
    owners = [m for m in memberships if m.role is MembershipRole.OWNER]
    assert len(owners) == 1
    assert owners[0].user_id == world.bob.user_id

    graph = await graph_repository.get_graph(db, world.graph_id)
    assert graph.owner_user_id == world.bob.user_id


async def test_transfer_requires_the_current_owner(db, audit, graph_service, world):
    with pytest.raises(GraphOperationRefused) as excinfo:
        await graph_service.transfer_ownership(
            db,
            graph_id=world.graph_id,
            current_owner_user_id=world.bob.user_id,
            new_owner_user_id=world.bob.user_id,
            audit=audit,
        )
    assert excinfo.value.reason == "owner_only"


async def test_transfer_targets_an_existing_member_only(db, audit, graph_service, world):
    with pytest.raises(GraphOperationRefused) as excinfo:
        await graph_service.transfer_ownership(
            db,
            graph_id=world.graph_id,
            current_owner_user_id=world.alice.user_id,
            new_owner_user_id=world.outsider.user_id,
            audit=audit,
        )
    assert excinfo.value.surface_as_not_found


# ── 04 §5 / RAUTH-005 / RAUTH V2: the explicit visibility change ───────────


async def test_sharing_is_owner_only_and_audited(db, audit, graph_service, world):
    """04 §5 `[LOCKED]` — owner-only, explicit, audited."""

    change = await graph_service.change_visibility(
        db,
        resource_type="fileresource",
        resource_ref=world.alice_private_file.file_id,
        actor_user_id=world.alice.user_id,
        new_visibility=Visibility.GRAPH,
        audit=audit,
    )
    assert change.previous is Visibility.PRIVATE
    assert change.current is Visibility.GRAPH
    assert world.alice_private_file.visibility is Visibility.GRAPH

    assert "resource.visibility.shared" in await audit_actions(db)


async def test_unsharing_is_the_same_reversible_audited_path(db, audit, graph_service, world):
    await graph_service.change_visibility(
        db,
        resource_type="fileresource",
        resource_ref=world.alice_shared_file.file_id,
        actor_user_id=world.alice.user_id,
        new_visibility=Visibility.PRIVATE,
        audit=audit,
    )
    assert world.alice_shared_file.visibility is Visibility.PRIVATE
    assert "resource.visibility.unshared" in await audit_actions(db)


async def test_a_non_owner_cannot_change_visibility(db, audit, graph_service, world):
    with pytest.raises(GraphOperationRefused) as excinfo:
        await graph_service.change_visibility(
            db,
            resource_type="fileresource",
            resource_ref=world.alice_private_file.file_id,
            actor_user_id=world.bob.user_id,
            new_visibility=Visibility.GRAPH,
            audit=audit,
        )
    assert excinfo.value.reason == "owner_only"
    assert excinfo.value.surface_as_not_found
    assert world.alice_private_file.visibility is Visibility.PRIVATE


async def test_graph_visibility_requires_a_graph_scope(db, audit, graph_service, world):
    """Making something "graph-visible" with no graph would name an audience that
    does not exist."""

    import datetime

    from server.storage.models import FileResource

    unscoped = FileResource(
        owner_user_id=world.alice.user_id,
        source_user_id=world.alice.user_id,
        graph_id=None,
        visibility=Visibility.PRIVATE,
        sandbox_root="/sandbox/x",
        relative_path="unscoped.txt",
        size_bytes=1,
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(unscoped)
    await db.flush()

    with pytest.raises(GraphOperationRefused) as excinfo:
        await graph_service.change_visibility(
            db,
            resource_type="fileresource",
            resource_ref=unscoped.file_id,
            actor_user_id=world.alice.user_id,
            new_visibility=Visibility.GRAPH,
            audit=audit,
        )
    assert excinfo.value.reason == "no_graph_scope"


async def test_change_visibility_is_the_only_writer_of_a_visibility_field(world):
    """RAUTH-005 `[LOCKED]` — "changing `visibility` […] is **never** a side effect
    of any other operation".

    That is a whole-codebase property, so it is checked as one: no module under
    `server/` assigns to a `visibility` attribute except
    `GraphService.change_visibility`. A future branch that sets `visibility` inline
    "just this once" fails here, which is the only way this guarantee survives
    heavy code generation.
    """

    # (a) No module assigns a visibility attribute directly. `change_visibility`
    #     writes through `setattr(row, visibility_attr, ...)` because it handles
    #     several resource types, so a literal `.visibility =` anywhere is by
    #     definition some *other* writer.
    direct = re.compile(r"\.visibility\s*=(?!=)")
    offenders: list[str] = []
    for path in sorted(Path("server").rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if direct.search(line):
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert offenders == [], offenders

    # (b) The indirect writer is confined to one module and one function.
    writers = [
        str(path)
        for path in sorted(Path("server").rglob("*.py"))
        if "visibility_attr" in path.read_text()
    ]
    assert writers == ["server/graph/service.py"], writers

    source = Path("server/graph/service.py").read_text()
    setattr_lines = [
        line.strip()
        for line in source.splitlines()
        if "setattr(" in line and not line.lstrip().startswith("#")
    ]
    assert setattr_lines == ["setattr(row, visibility_attr, new_visibility)"], setattr_lines

    # And that one line lives inside `change_visibility`, not in some helper the
    # rest of the module could call.
    body = source.split("async def change_visibility(", 1)[1]
    assert "setattr(row, visibility_attr, new_visibility)" in body.split("\n\n# The visibility-bearing")[0]


# ── §17 of the scope: the audit path ──────────────────────────────────────


async def test_security_sensitive_operations_all_produce_audit_events(
    db, audit, graph_service, world
):
    """Every category §17 of the security-core scope names is represented."""

    await graph_service.request_access(
        db,
        graph_id=world.graph_id,
        requester_user_id=world.outsider.user_id,
        message=None,
        audit=audit,
    )
    await graph_service.approve_member(
        db,
        graph_id=world.graph_id,
        approver_user_id=world.alice.user_id,
        user_id=world.outsider.user_id,
        role=MembershipRole.MEMBER,
        audit=audit,
    )
    await graph_service.change_visibility(
        db,
        resource_type="fileresource",
        resource_ref=world.alice_private_file.file_id,
        actor_user_id=world.alice.user_id,
        new_visibility=Visibility.GRAPH,
        audit=audit,
    )
    await graph_service.revoke_membership(
        db,
        graph_id=world.graph_id,
        actor_user_id=world.alice.user_id,
        target_user_id=world.outsider.user_id,
        audit=audit,
    )

    actions = set(await audit_actions(db))
    assert {
        "graph.created",
        "graph.access_request.created",
        "graph.membership.approved",
        "graph.membership.revoked",
        "resource.visibility.shared",
    } <= actions


async def test_the_audit_logger_refuses_an_unregistered_action(db, audit, world):
    """Actions are a closed registry, so a caller cannot smuggle data through a
    free-form action name (12 §6) or create an action no query will find."""

    from shared.schemas.enums import AuditActor, AuditResult

    with pytest.raises(ValueError):
        await audit.record(
            actor=AuditActor.USER,
            action="something.i.invented",  # type: ignore[arg-type]
            resource="x",
            result=AuditResult.SUCCESS,
        )


async def test_the_audit_resource_field_cannot_become_a_payload(db, audit, world):
    """`resource` is a reference, not a free-form field: it is length-capped, so it
    cannot be used to carry a secret or a blob into the audit log."""

    from server.security.audit import MAX_RESOURCE_LENGTH
    from server.security.events import AuditAction
    from shared.schemas.enums import AuditActor, AuditResult

    await audit.record(
        actor=AuditActor.USER,
        action=AuditAction.GRAPH_CREATED,
        resource="x" * (MAX_RESOURCE_LENGTH * 3),
        result=AuditResult.SUCCESS,
    )
    await db.flush()

    rows = (await db.execute(select(AuditEvent))).scalars().all()
    assert all(len(row.resource) <= MAX_RESOURCE_LENGTH for row in rows)


async def test_the_audit_module_exposes_no_update_or_delete_path():
    """PERM-006 — "the agent cannot disable or write false audit entries".

    Append-only is enforced by the module surface: there is no update, delete, or
    "correct an earlier event" helper to call.
    """

    from server.security.audit import AuditLogger

    public = {name for name in dir(AuditLogger) if not name.startswith("_")}
    assert public == {"record", "record_permission_decision", "record_secret_event", "request_id"}


# ── HTTP surface ──────────────────────────────────────────────────────────


async def test_graph_listing_is_scoped_to_membership(api):
    """02 §4 / ENT-002 — the listing is scoped by the membership join, so it cannot
    return a graph the caller is not in."""

    alice = await api.onboard(subject="alice-subject")
    bob = await api.onboard(subject="bob-subject")

    await api.client.post(
        f"{API_V1_PREFIX}/graphs",
        json={"name": "alice's", "type": "shared"},
        headers=alice.auth,
    )

    assert len((await api.client.get(f"{API_V1_PREFIX}/graphs", headers=alice.auth)).json()["items"]) == 1
    assert (await api.client.get(f"{API_V1_PREFIX}/graphs", headers=bob.auth)).json()["items"] == []


async def test_graph_creation_is_idempotent_under_a_repeated_key(api):
    """02 §1.4 — a retried create with the same `Idempotency-Key` returns the
    original graph rather than a second one."""

    onboarded = await api.onboard()
    headers = {**onboarded.auth, "Idempotency-Key": "client-retry-key-1"}
    body = {"name": "once", "type": "shared"}

    first = await api.client.post(f"{API_V1_PREFIX}/graphs", json=body, headers=headers)
    second = await api.client.post(f"{API_V1_PREFIX}/graphs", json=body, headers=headers)

    assert first.status_code == 201
    assert second.json()["graph_id"] == first.json()["graph_id"]

    listed = await api.client.get(f"{API_V1_PREFIX}/graphs", headers=onboarded.auth)
    assert len(listed.json()["items"]) == 1


async def test_reusing_an_idempotency_key_with_a_different_body_is_a_conflict(api):
    onboarded = await api.onboard()
    headers = {**onboarded.auth, "Idempotency-Key": "client-retry-key-1"}

    await api.client.post(
        f"{API_V1_PREFIX}/graphs", json={"name": "first", "type": "shared"}, headers=headers
    )
    clash = await api.client.post(
        f"{API_V1_PREFIX}/graphs", json={"name": "second", "type": "shared"}, headers=headers
    )
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "conflict"


async def test_a_member_approving_a_member_is_forbidden_over_http(api):
    """AZ-T6 at the HTTP boundary: `403`, since the caller is a member and the
    graph's existence is already known to them."""

    alice = await api.onboard(subject="alice-subject")
    bob = await api.onboard(subject="bob-subject")
    outsider = await api.onboard(subject="outsider-subject")

    created = await api.client.post(
        f"{API_V1_PREFIX}/graphs", json={"name": "g", "type": "shared"}, headers=alice.auth
    )
    graph_id = created.json()["graph_id"]

    async with api.storage.session() as session:
        from server.storage.models import AccessToken, User

        bob_user = (
            await session.execute(select(User).where(User.oidc_subject == "bob-subject"))
        ).scalars().one()
        outsider_user = (
            await session.execute(select(User).where(User.oidc_subject == "outsider-subject"))
        ).scalars().one()

    asked = await api.client.post(
        f"{API_V1_PREFIX}/graphs/{graph_id}/access-requests", json={}, headers=bob.auth
    )
    assert asked.status_code == 201

    approved = await api.client.post(
        f"{API_V1_PREFIX}/graphs/{graph_id}/members",
        json={"user_id": str(bob_user.user_id), "role": "member"},
        headers=alice.auth,
    )
    assert approved.status_code == 201

    # Bob is now a member; he still cannot approve anyone.
    refused = await api.client.post(
        f"{API_V1_PREFIX}/graphs/{graph_id}/members",
        json={"user_id": str(outsider_user.user_id), "role": "member"},
        headers=bob.auth,
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "unauthorized"


async def test_a_non_member_approving_gets_not_found(api):
    """A non-member must not learn the graph exists (04 §7)."""

    alice = await api.onboard(subject="alice-subject")
    outsider = await api.onboard(subject="outsider-subject")

    created = await api.client.post(
        f"{API_V1_PREFIX}/graphs", json={"name": "g", "type": "shared"}, headers=alice.auth
    )
    graph_id = created.json()["graph_id"]

    resp = await api.client.post(
        f"{API_V1_PREFIX}/graphs/{graph_id}/members",
        json={"user_id": str(uuid.uuid4()), "role": "member"},
        headers=outsider.auth,
    )
    assert resp.status_code == 404


async def test_an_access_request_for_an_unknown_graph_is_not_found(api):
    onboarded = await api.onboard()
    resp = await api.client.post(
        f"{API_V1_PREFIX}/graphs/{uuid.uuid4()}/access-requests",
        json={"message": None},
        headers=onboarded.auth,
    )
    assert resp.status_code == 404


async def test_audit_events_survive_a_refused_request(api):
    """02 §1.2 — a failed request produces "the appropriate error, an `AuditEvent`
    […] and **no side effect**".

    The refusal path commits the audit trail while the refused mutation is never
    written (see `server/gateway/deps.py`).
    """

    alice = await api.onboard(subject="alice-subject")
    outsider = await api.onboard(subject="outsider-subject")

    created = await api.client.post(
        f"{API_V1_PREFIX}/graphs", json={"name": "g", "type": "shared"}, headers=alice.auth
    )
    graph_id = created.json()["graph_id"]

    resp = await api.client.post(
        f"{API_V1_PREFIX}/sessions/active-graph",
        json={"graph_id": graph_id},
        headers=outsider.auth,
    )
    assert resp.status_code == 404

    async with api.storage.session() as session:
        decisions = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "authz.decision")
            )
        ).scalars().all()
        assert decisions, "the refused authorization left no audit trail"

        # And no membership was created as a side effect.
        memberships = (await session.execute(select(GraphMembership))).scalars().all()
        assert all(m.user_id != uuid.UUID(int=0) for m in memberships)
        assert len(memberships) == 1  # only alice's owner membership
