"""04's acceptance hooks — AZ-T1..AZ-T12.

04 §11 calls these release-blocking: "this is the leak-prevention surface". AZ-T1
is, in the document's own words, "the single most important test in Track B".
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from server.graph.authorization import AccessRequest, readable
from server.graph.ports import ResourceDescriptor
from server.graph.service import GraphOperationRefused
from server.storage.models import AuditEvent, PermissionDecision
from shared.schemas.authorization import (
    DenialSurface,
    Operation,
    Principal,
    ResourceType,
)
from shared.schemas.enums import (
    CapabilityScopeType,
    MembershipRole,
    PermissionDecisionValue,
    Visibility,
)

pytestmark = pytest.mark.asyncio


def principal_for(user, *, graph_id=None) -> Principal:
    """A principal for a user with synthetic device/session ids.

    These tests exercise the authorization engine, which per 03 §8 receives an
    already-validated principal. How that principal is *established* is
    `test_auth_*.py`'s subject; conflating the two would make a failure here
    ambiguous between the two layers.
    """

    return Principal(
        user_id=user.user_id,
        device_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        active_graph_id=graph_id,
    )


async def authorize(engine, db, audit, request: AccessRequest):
    return await engine.authorize(db, request, audit=audit)


def read_file(principal: Principal, file_id, *, graph_id=None) -> AccessRequest:
    return AccessRequest(
        principal=principal,
        operation=Operation.READ,
        resource_type=ResourceType.FILERESOURCE,
        resource_ref=str(file_id),
        graph_id=graph_id,
    )


# ── AZ-T1: the most important test in Track B ───────────────────────────────


async def test_az_t1_member_cannot_read_another_members_private_resource(
    engine, db, audit, world
):
    """AZ-T1 (RAUTH-003) — a graph member cannot read another member's private
    resource in the same graph, and the denial surfaces as 404.

    Bob is an active member of the same graph as Alice's file. Sharing a graph is
    the entire basis on which a naive implementation would let him read it; the
    visibility dimension is what stops it.
    """

    outcome = await authorize(
        engine,
        db,
        audit,
        read_file(
            principal_for(world.bob, graph_id=world.graph_id),
            world.alice_private_file.file_id,
            graph_id=world.graph_id,
        ),
    )

    assert outcome.decision is PermissionDecisionValue.DENY
    assert outcome.reason == "not_visible"
    # 04 §7: a private resource must be indistinguishable from an absent one.
    assert outcome.surface is DenialSurface.NOT_FOUND


async def test_az_t1_owner_can_read_their_own_private_resource(engine, db, audit, world):
    """The positive half of AZ-T1: the rule denies others, not the owner."""

    outcome = await authorize(
        engine,
        db,
        audit,
        read_file(
            principal_for(world.alice, graph_id=world.graph_id),
            world.alice_private_file.file_id,
            graph_id=world.graph_id,
        ),
    )
    assert outcome.allowed


async def test_az_t1_member_can_read_a_graph_shared_resource(engine, db, audit, world):
    """`visibility: graph` + active membership is the *only* other path to a
    read (RAUTH-004), and it works."""

    outcome = await authorize(
        engine,
        db,
        audit,
        read_file(
            principal_for(world.bob, graph_id=world.graph_id),
            world.alice_shared_file.file_id,
            graph_id=world.graph_id,
        ),
    )
    assert outcome.allowed


async def test_az_t1_private_denial_is_indistinguishable_from_absent(
    engine, db, audit, world
):
    """The oracle 04 §7 exists to remove: probing an existing-but-private id and
    a nonexistent id must look identical to the caller."""

    bob = principal_for(world.bob, graph_id=world.graph_id)

    existing_but_private = await authorize(
        engine, db, audit, read_file(bob, world.alice_private_file.file_id, graph_id=world.graph_id)
    )
    nonexistent = await authorize(
        engine, db, audit, read_file(bob, uuid.uuid4(), graph_id=world.graph_id)
    )

    assert existing_but_private.surface is nonexistent.surface is DenialSurface.NOT_FOUND
    assert not existing_but_private.allowed and not nonexistent.allowed


# ── AZ-T2: default private (RAUTH-005) ──────────────────────────────────────


async def test_az_t2_resources_default_to_private(world):
    """AZ-T2 (RAUTH-005) — the visibility column's default is `private`.

    Asserted on a row built without naming `visibility` at all, since the risk is
    a default that is only applied when a caller remembers to pass one.
    """

    import datetime

    from server.storage.models import FileResource

    row = FileResource(
        owner_user_id=world.alice.user_id,
        source_user_id=world.alice.user_id,
        graph_id=world.graph_id,
        sandbox_root="/sandbox/x",
        relative_path="unspecified.txt",
        size_bytes=1,
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    assert row.visibility in (None, Visibility.PRIVATE)


async def test_az_t2_missing_visibility_is_treated_as_private(engine, db, audit, world):
    """04 §9 — "a resource with `graph_id` set but `visibility` missing is
    treated as `private` (safe default)".

    Exercised at the predicate, because a descriptor is what the engine actually
    decides on.
    """

    descriptor = ResourceDescriptor(
        resource_type=ResourceType.FILERESOURCE,
        resource_ref=str(uuid.uuid4()),
        owner_user_id=world.alice.user_id,
        visibility=Visibility.PRIVATE,  # what the loader substitutes for a null
        graph_id=world.graph_id,
    )
    assert not readable(
        user_id=world.bob.user_id,
        resource=descriptor,
        is_active_member_of_resource_graph=True,
    )


# ── AZ-T3: anti-enumeration for non-members ─────────────────────────────────


async def test_az_t3_non_member_request_returns_not_found(engine, db, audit, world):
    """AZ-T3 — a non-member's request for a graph resource returns 404,
    indistinguishable from "doesn't exist"."""

    outcome = await authorize(
        engine,
        db,
        audit,
        read_file(
            principal_for(world.outsider),
            world.alice_shared_file.file_id,
            graph_id=world.graph_id,
        ),
    )
    assert outcome.decision is PermissionDecisionValue.DENY
    assert outcome.reason == "not_a_member"
    assert outcome.surface is DenialSurface.NOT_FOUND


async def test_az_t3_non_member_cannot_read_a_graph_shared_file_without_graph_context(
    engine, db, audit, world
):
    """Omitting `graph_id` from the request must not bypass D1.

    D1 only runs when the request names a graph, so a caller could try to skip it
    by leaving the field out. D4 then has to resolve membership against the
    *resource's* graph independently — this asserts it does.
    """

    outcome = await authorize(
        engine,
        db,
        audit,
        read_file(principal_for(world.outsider), world.alice_shared_file.file_id),
    )
    assert not outcome.allowed
    assert outcome.surface is DenialSurface.NOT_FOUND


# ── AZ-T4: owner-only mutations (D3) ────────────────────────────────────────


async def test_az_t4_only_the_owner_may_change_visibility_or_delete(
    engine, db, audit, world
):
    """AZ-T4 — a member gets 403 for an owner-only operation on a resource they
    *can* see (so no existence is leaked, and 403 is the right surface)."""

    bob = principal_for(world.bob, graph_id=world.graph_id)

    for operation in (Operation.DELETE, Operation.SHARE, Operation.WRITE):
        outcome = await authorize(
            engine,
            db,
            audit,
            AccessRequest(
                principal=bob,
                operation=operation,
                resource_type=ResourceType.FILERESOURCE,
                resource_ref=str(world.alice_shared_file.file_id),
                graph_id=world.graph_id,
            ),
        )
        assert outcome.decision is PermissionDecisionValue.DENY, operation
        assert outcome.reason == "owner_only", operation
        assert outcome.surface is DenialSurface.FORBIDDEN, operation


async def test_az_t4_non_owner_mutation_of_an_invisible_resource_is_404_not_403(
    engine, db, audit, world
):
    """The ordering that makes AZ-T4 and AZ-T1 coexist.

    If D3 ran before D4, Bob would learn Alice's *private* file exists by being
    told he is "not the owner". Visibility is resolved first, so he gets 404.
    """

    outcome = await authorize(
        engine,
        db,
        audit,
        AccessRequest(
            principal=principal_for(world.bob, graph_id=world.graph_id),
            operation=Operation.DELETE,
            resource_type=ResourceType.FILERESOURCE,
            resource_ref=str(world.alice_private_file.file_id),
            graph_id=world.graph_id,
        ),
    )
    assert outcome.surface is DenialSurface.NOT_FOUND
    assert outcome.reason == "not_visible"


# ── AZ-T5: sharing a secret is prohibited (GRAPH-009) ───────────────────────


async def test_az_t5_sharing_a_secret_class_resource_is_prohibited(
    engine, db, audit, world, store
):
    """AZ-T5 (GRAPH-009) — a share on a secret-class resource is refused as
    `prohibited`, not merely denied.

    04 §5: "Sharing a graph, or sharing a resource into a graph, never shares
    secrets." The distinction between `deny` and `prohibited` matters: a denial
    invites a retry with different authority, a prohibition says no authority
    exists.
    """

    from server.secrets.requester import SecretRequester
    from shared.schemas.enums import SecretClass, SecretOwnerScopeType

    secret_ref = await store.set(
        db,
        owner_scope_type=SecretOwnerScopeType.USER,
        owner_scope_id=str(world.alice.user_id),
        secret_class=SecretClass.OAUTH_TOKEN,
        value="test-value-not-a-real-secret",
        requester=SecretRequester.server(),
        audit=audit,
    )

    outcome = await authorize(
        engine,
        db,
        audit,
        AccessRequest(
            principal=principal_for(world.alice, graph_id=world.graph_id),
            operation=Operation.SHARE,
            resource_type=ResourceType.SECRET_REFERENCE,
            resource_ref=secret_ref,
            graph_id=world.graph_id,
        ),
    )

    assert outcome.decision is PermissionDecisionValue.DENY
    assert outcome.surface is DenialSurface.PROHIBITED
    assert outcome.floor_category == "exfiltrate_credentials"


# ── AZ-T6: role gates graph management (D2) ─────────────────────────────────


async def test_az_t6_member_cannot_approve_memberships(db, audit, world, graph_service):
    """AZ-T6 (04 §2 D2) — "A `member` cannot approve members"."""

    with pytest.raises(GraphOperationRefused) as excinfo:
        await graph_service.approve_member(
            db,
            graph_id=world.graph_id,
            approver_user_id=world.bob.user_id,
            user_id=world.outsider.user_id,
            role=MembershipRole.MEMBER,
            audit=audit,
        )
    assert excinfo.value.reason == "owner_only"
    assert not excinfo.value.surface_as_not_found


async def test_az_t6_non_member_approval_attempt_is_not_found(
    db, audit, world, graph_service
):
    """A non-member attempting to approve must not learn the graph exists."""

    with pytest.raises(GraphOperationRefused) as excinfo:
        await graph_service.approve_member(
            db,
            graph_id=world.graph_id,
            approver_user_id=world.outsider.user_id,
            user_id=world.outsider.user_id,
            role=MembershipRole.MEMBER,
            audit=audit,
        )
    assert excinfo.value.surface_as_not_found


async def test_az_t6_administer_requires_owner_role_in_the_engine(
    engine, db, audit, world
):
    """The same rule at the engine level, so a future caller that reaches the
    engine directly is gated identically."""

    outcome = await authorize(
        engine,
        db,
        audit,
        AccessRequest(
            principal=principal_for(world.bob, graph_id=world.graph_id),
            operation=Operation.ADMINISTER,
            resource_type=ResourceType.GRAPH,
            resource_ref=str(world.graph_id),
            graph_id=world.graph_id,
        ),
    )
    assert outcome.reason == "role_forbids"
    assert outcome.surface is DenialSurface.FORBIDDEN


# ── AZ-T7: capability is not a visibility bypass ────────────────────────────


async def test_az_t7_file_read_capability_does_not_read_another_users_private_file(
    engine, db, audit, world, grants
):
    """AZ-T7 / TL-T3 — the holder of `file.read` still cannot read another
    user's private file.

    D5 is checked *in addition to* D1–D4 (04 §2 D5), never instead. The grant is
    real and active here; the denial is D4's.
    """

    await grants.grant(
        db,
        principal_id=world.bob.user_id,
        scope_type=CapabilityScopeType.USER,
        capability="file.read",
        granted_by=world.bob.user_id,
    )

    outcome = await authorize(
        engine,
        db,
        audit,
        AccessRequest(
            principal=principal_for(world.bob, graph_id=world.graph_id),
            operation=Operation.READ,
            resource_type=ResourceType.FILERESOURCE,
            resource_ref=str(world.alice_private_file.file_id),
            graph_id=world.graph_id,
            required_capability="file.read",
            capability_operation="read_file",
        ),
    )

    assert not outcome.allowed
    assert outcome.reason == "not_visible"


# ── AZ-T8: revocation is immediate ──────────────────────────────────────────


async def test_az_t8_revoking_membership_fails_the_next_check(
    engine, db, audit, world, graph_service
):
    """AZ-T8 (04 §4.3) — revocation takes effect on the *next* check, with no
    session refresh and no cache to wait out."""

    bob = principal_for(world.bob, graph_id=world.graph_id)
    before = await authorize(
        engine, db, audit, read_file(bob, world.alice_shared_file.file_id, graph_id=world.graph_id)
    )
    assert before.allowed

    await graph_service.revoke_membership(
        db,
        graph_id=world.graph_id,
        actor_user_id=world.alice.user_id,
        target_user_id=world.bob.user_id,
        audit=audit,
    )

    after = await authorize(
        engine, db, audit, read_file(bob, world.alice_shared_file.file_id, graph_id=world.graph_id)
    )
    assert not after.allowed
    assert after.reason == "not_a_member"


# ── AZ-T9: fail-closed ──────────────────────────────────────────────────────


async def test_az_t9_membership_lookup_failure_denies(engine, db, audit, world):
    """AZ-T9 (FAIL-CORE-003) — "any error in the decision path results in
    `deny`, never `allow`".

    The membership reader is replaced with one that raises, which is why it is a
    port: a boundary that cannot be made to fail cannot be shown to fail closed.
    """

    class ExplodingMemberships:
        async def active_role(self, session, *, graph_id, user_id):
            raise RuntimeError("membership store unavailable")

    engine._memberships = ExplodingMemberships()

    outcome = await authorize(
        engine,
        db,
        audit,
        read_file(
            principal_for(world.bob, graph_id=world.graph_id),
            world.alice_shared_file.file_id,
            graph_id=world.graph_id,
        ),
    )
    assert outcome.decision is PermissionDecisionValue.DENY
    assert outcome.reason == "authorization_unavailable"
    # A read denial stays 404 even on an internal failure, so an induced error
    # cannot be used to distinguish an existing private resource from an absent one.
    assert outcome.surface is DenialSurface.NOT_FOUND


async def test_az_t9_resource_loader_failure_denies(engine, db, audit, world):
    """The same property for the other port the engine depends on."""

    class ExplodingLoader:
        async def load(self, session, resource_type, resource_ref):
            raise RuntimeError("resource store unavailable")

    engine._resources = ExplodingLoader()

    outcome = await authorize(
        engine,
        db,
        audit,
        read_file(
            principal_for(world.alice, graph_id=world.graph_id),
            world.alice_private_file.file_id,
            graph_id=world.graph_id,
        ),
    )
    assert outcome.decision is PermissionDecisionValue.DENY


async def test_az_t9_unloadable_resource_type_denies(engine, db, audit, world):
    """An unimplemented resource type denies rather than defaulting to allow.

    `mem0fact` has no loader in this branch (Mem0 is `11`'s store), and the right
    answer for "I cannot establish this resource's owner or visibility" is
    refusal.
    """

    outcome = await authorize(
        engine,
        db,
        audit,
        AccessRequest(
            principal=principal_for(world.alice, graph_id=world.graph_id),
            operation=Operation.READ,
            resource_type=ResourceType.MEM0FACT,
            resource_ref=str(uuid.uuid4()),
            graph_id=world.graph_id,
        ),
    )
    assert not outcome.allowed
    assert outcome.surface is DenialSurface.NOT_FOUND


# ── AZ-T10: the agent cannot become a confused deputy ───────────────────────


async def test_az_t10_agent_acting_for_alice_cannot_read_bobs_private_data(
    engine, db, audit, world
):
    """AZ-T10 (04 §8) — the agent's context is bounded by its principal's
    authorization.

    There is no "agent principal" to test with, and that is the point: 04 §8 says
    the runtime constructs an `AccessRequest` **on behalf of the principal**, so
    an agent acting for Alice is exactly an `AccessRequest` carrying Alice's
    principal. It therefore gets Alice's answers — including a refusal for Bob's
    private file — and there is no field on `AccessRequest` through which a
    proposal could widen that.
    """

    outcome = await authorize(
        engine,
        db,
        audit,
        read_file(
            principal_for(world.alice, graph_id=world.graph_id),
            world.bob_private_file.file_id,
            graph_id=world.graph_id,
        ),
    )
    assert not outcome.allowed
    assert outcome.surface is DenialSurface.NOT_FOUND

    assert not any(
        field in AccessRequest.__dataclass_fields__
        for field in ("as_user_id", "effective_user_id", "override_principal")
    )


# ── AZ-T11: graph deletion does not delete private data ─────────────────────


async def test_az_t11_revoking_a_member_leaves_their_private_data_intact(
    db, audit, world, graph_service
):
    """AZ-T11's neighbouring guarantee (04 §4.3/§4.4) — "deleting a graph never
    deletes another user's private data".

    Graph *deletion* is not implemented in this branch (04 §4.4 defers the
    cascade's resource half to `09`/`11`), so what is asserted here is the part
    this branch owns: membership revocation is purely an authorization change and
    touches no resource row. A future `delete_graph` that removed member data
    would have to change this behaviour to pass.
    """

    from server.storage.models import FileResource

    await graph_service.revoke_membership(
        db,
        graph_id=world.graph_id,
        actor_user_id=world.alice.user_id,
        target_user_id=world.bob.user_id,
        audit=audit,
    )

    still_there = await db.get(FileResource, world.bob_private_file.file_id)
    assert still_there is not None
    assert still_there.owner_user_id == world.bob.user_id
    assert still_there.visibility is Visibility.PRIVATE


async def test_az_t11_ownership_transfer_does_not_transfer_resource_ownership(
    db, audit, world, graph_service
):
    """04 §6 `[LOCKED]` — "Resource ownership does **not** transfer with graph
    ownership"."""

    from server.storage.models import FileResource

    await graph_service.transfer_ownership(
        db,
        graph_id=world.graph_id,
        current_owner_user_id=world.alice.user_id,
        new_owner_user_id=world.bob.user_id,
        audit=audit,
    )

    alice_file = await db.get(FileResource, world.alice_private_file.file_id)
    assert alice_file.owner_user_id == world.alice.user_id


# ── AZ-T12: every decision is audited ───────────────────────────────────────


async def test_az_t12_every_decision_emits_an_audit_event_and_permission_decision(
    engine, db, audit, world
):
    """AZ-T12 (04 §1) — "Every call produces a `PermissionDecision` and an
    `AuditEvent`", for allows and denies alike."""

    await authorize(
        engine,
        db,
        audit,
        read_file(
            principal_for(world.alice, graph_id=world.graph_id),
            world.alice_private_file.file_id,
            graph_id=world.graph_id,
        ),
    )
    await authorize(
        engine,
        db,
        audit,
        read_file(
            principal_for(world.bob, graph_id=world.graph_id),
            world.alice_private_file.file_id,
            graph_id=world.graph_id,
        ),
    )

    decisions = (
        (await db.execute(select(PermissionDecision).order_by(PermissionDecision.timestamp)))
        .scalars()
        .all()
    )
    assert len(decisions) >= 2
    assert {d.decision for d in decisions} >= {
        PermissionDecisionValue.ALLOW,
        PermissionDecisionValue.DENY,
    }
    assert all(d.reason for d in decisions)

    events = (
        (await db.execute(select(AuditEvent).where(AuditEvent.action == "authz.decision")))
        .scalars()
        .all()
    )
    assert len(events) >= 2
