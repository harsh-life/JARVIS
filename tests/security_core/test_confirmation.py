"""Confirmation tokens — PERM-004, 05 §4, RT-T3/RT-T4/TL-T4.

The failure this file exists to rule out is a **reusable approval**. 05 §4 makes
confirmation a pause on one specific action; if a token minted for one action
validated for another, the model could get a human to approve something small and
then spend that approval on something else.

Each test below removes one field from the match and asserts the token stops
working — so the binding is verified field by field rather than as a single
happy-path "the token works".
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from server.capabilities.confirmation import (
    DEFAULT_CONFIRMATION_TTL,
    ConfirmationRefused,
)
from server.capabilities.floor import AbsoluteFloorViolation
from server.graph.authorization import AccessRequest
from shared.schemas.authorization import ActionBinding, Operation, Principal, ResourceType
from shared.schemas.enums import PermissionDecisionValue, RiskCategory, Visibility

pytestmark = pytest.mark.asyncio


def binding_for(user, *, task_id="task-1", operation=Operation.DELETE, resource_ref=None, **kw):
    return ActionBinding(
        principal_user_id=user.user_id,
        task_id=task_id,
        operation=operation,
        resource_type=kw.pop("resource_type", ResourceType.FILERESOURCE),
        session_id=kw.pop("session_id", None),
        capability=kw.pop("capability", "file.write"),
        resource_ref=resource_ref or "file-a",
        arguments=kw.pop("arguments", {"path": "/a.txt"}),
    )


async def issue(confirmations, db, binding, tier=RiskCategory.CONSEQUENTIAL, **kw):
    return await confirmations.issue(db, binding=binding, risk_category=tier, **kw)


# ── the happy path ─────────────────────────────────────────────────────────


async def test_a_matching_confirmation_validates_once(confirmations, db, world):
    binding = binding_for(world.alice)
    issued = await issue(confirmations, db, binding)

    assert await confirmations.consume(db, token=issued.token, binding=binding)


async def test_a_token_is_single_use(confirmations, db, world):
    """05 §4 — a confirmation authorizes one attempt.

    The second call must fail, or a captured token would be a standing permission
    for that action.
    """

    binding = binding_for(world.alice)
    issued = await issue(confirmations, db, binding)

    assert await confirmations.consume(db, token=issued.token, binding=binding)
    assert not await confirmations.consume(db, token=issued.token, binding=binding)


async def test_only_the_hash_of_the_token_is_stored(confirmations, db, world):
    """A database leak must not yield usable confirmation tokens."""

    from tests.security_core.helpers import database_contains

    binding = binding_for(world.alice)
    issued = await issue(confirmations, db, binding)
    await db.flush()

    assert not await database_contains(db, issued.token)


# ── the binding, field by field ────────────────────────────────────────────


async def test_a_token_for_one_resource_does_not_validate_for_another(
    confirmations, db, world
):
    """The canonical case from §13 of the security-core scope: a confirmation for
    `delete(file A)` must not become valid for `delete(file B)`."""

    issued = await issue(confirmations, db, binding_for(world.alice, resource_ref="file-a"))

    assert not await confirmations.consume(
        db, token=issued.token, binding=binding_for(world.alice, resource_ref="file-b")
    )


async def test_a_token_for_one_operation_does_not_validate_for_another(
    confirmations, db, world
):
    """`delete(file A)` must not become `read(file A)` — or anything else."""

    issued = await issue(
        confirmations, db, binding_for(world.alice, operation=Operation.DELETE)
    )

    for operation in (Operation.READ, Operation.WRITE, Operation.SHARE, Operation.ADMINISTER):
        assert not await confirmations.consume(
            db,
            token=issued.token,
            binding=binding_for(world.alice, operation=operation),
        ), operation


async def test_a_token_does_not_validate_for_a_different_principal(
    confirmations, db, world
):
    """Alice's approval is not Bob's."""

    issued = await issue(confirmations, db, binding_for(world.alice))

    assert not await confirmations.consume(
        db, token=issued.token, binding=binding_for(world.bob)
    )


async def test_a_token_does_not_validate_for_a_different_task(confirmations, db, world):
    """"The user confirmed this task" must not generalise across tasks."""

    issued = await issue(confirmations, db, binding_for(world.alice, task_id="task-1"))

    assert not await confirmations.consume(
        db, token=issued.token, binding=binding_for(world.alice, task_id="task-2")
    )


async def test_a_token_does_not_validate_with_different_arguments(
    confirmations, db, world
):
    """The argument hash: approving "send to X" must not send to Y."""

    issued = await issue(
        confirmations, db, binding_for(world.alice, arguments={"to": "alice@example.test"})
    )

    assert not await confirmations.consume(
        db,
        token=issued.token,
        binding=binding_for(world.alice, arguments={"to": "attacker@example.test"}),
    )


async def test_a_token_does_not_validate_for_a_different_capability(
    confirmations, db, world
):
    issued = await issue(confirmations, db, binding_for(world.alice, capability="file.write"))

    assert not await confirmations.consume(
        db,
        token=issued.token,
        binding=binding_for(world.alice, capability="system.restricted"),
    )


async def test_a_token_does_not_validate_for_a_different_resource_type(
    confirmations, db, world
):
    issued = await issue(
        confirmations, db, binding_for(world.alice, resource_type=ResourceType.FILERESOURCE)
    )

    assert not await confirmations.consume(
        db,
        token=issued.token,
        binding=binding_for(world.alice, resource_type=ResourceType.SCHEDULEDJOB),
    )


async def test_a_token_does_not_validate_for_a_different_session(
    confirmations, db, world
):
    """Binding the session means a token captured from one session cannot be
    replayed in another."""

    session_a, session_b = uuid.uuid4(), uuid.uuid4()
    issued = await issue(
        confirmations, db, binding_for(world.alice, session_id=session_a)
    )

    assert not await confirmations.consume(
        db, token=issued.token, binding=binding_for(world.alice, session_id=session_b)
    )
    assert await confirmations.consume(
        db, token=issued.token, binding=binding_for(world.alice, session_id=session_a)
    )


async def test_argument_hashing_is_order_independent(world):
    """A canonical encoding, so the same logical arguments always hash the same.

    Without this, a legitimate confirmation would fail to validate at random
    depending on mapping order — which would push someone to weaken the check.
    """

    first = binding_for(world.alice, arguments={"a": 1, "b": 2})
    second = binding_for(world.alice, arguments={"b": 2, "a": 1})
    assert first.arguments_hash() == second.arguments_hash()

    different = binding_for(world.alice, arguments={"a": 1, "b": 3})
    assert first.arguments_hash() != different.arguments_hash()


async def test_argument_hashing_handles_non_json_scalars(world):
    """UUIDs and datetimes appear in real tool arguments; hashing must not raise
    inside a security check."""

    import datetime

    binding = binding_for(
        world.alice,
        arguments={"id": uuid.uuid4(), "when": datetime.datetime.now(datetime.timezone.utc)},
    )
    assert len(binding.arguments_hash()) == 64


async def test_no_arguments_and_empty_arguments_hash_identically(world):
    assert (
        binding_for(world.alice, arguments=None).arguments_hash()
        == binding_for(world.alice, arguments={}).arguments_hash()
    )


# ── expiry fails closed; no timeout auto-approves ──────────────────────────


async def test_an_expired_token_does_not_validate(confirmations, db, world):
    """05 §4 `[LOCKED]` — "No timeout auto-approves."

    Expiry can only ever turn into a denial. This is the property that makes
    "walk away and the action happens anyway" impossible.
    """

    from server.storage.models import ConfirmationToken
    from server.auth.repository import utcnow

    binding = binding_for(world.alice)
    issued = await issue(confirmations, db, binding)

    from server.secrets.crypto import hash_token

    row = await db.get(ConfirmationToken, hash_token(issued.token))
    row.expires_at = utcnow() - timedelta(seconds=1)
    await db.flush()

    assert not await confirmations.consume(db, token=issued.token, binding=binding)


async def test_an_expired_token_stays_unused_rather_than_being_spent(
    confirmations, db, world
):
    """An expired token is refused *before* the spend, so nothing about it looks
    like it was approved in the audit trail."""

    from server.auth.repository import utcnow
    from server.secrets.crypto import hash_token
    from server.storage.models import ConfirmationToken

    binding = binding_for(world.alice)
    issued = await issue(confirmations, db, binding)
    row = await db.get(ConfirmationToken, hash_token(issued.token))
    row.expires_at = utcnow() - timedelta(seconds=1)
    await db.flush()

    await confirmations.consume(db, token=issued.token, binding=binding)

    refreshed = await db.get(ConfirmationToken, hash_token(issued.token))
    assert refreshed.used_at is None


async def test_unknown_and_empty_tokens_do_not_validate(confirmations, db, world):
    binding = binding_for(world.alice)
    for token in ("", "not-a-token", "x" * 64):
        assert not await confirmations.consume(db, token=token, binding=binding), token


async def test_the_default_ttl_is_bounded(confirmations, db, world):
    """A token is not a standing permission: it expires on a human timescale."""

    assert timedelta(minutes=1) <= DEFAULT_CONFIRMATION_TTL <= timedelta(minutes=30)

    issued = await issue(confirmations, db, binding_for(world.alice))
    from server.auth.repository import utcnow

    assert issued.expires_at <= utcnow() + DEFAULT_CONFIRMATION_TTL + timedelta(seconds=5)


# ── RT-T4 / TL-T5: a prohibited action is never confirmable ────────────────


async def test_rt_t4_a_floor_action_is_never_issued_a_confirmation(
    confirmations, db, world
):
    """RT-T4 (PERM-006, 05 §4 `[LOCKED]`) — "An **absolute-floor** proposal
    returns `prohibited` — it is never offered as confirmable."

    Refusing to mint the token is what makes that true mechanically: there is no
    prompt a human could be tricked into approving.
    """

    with pytest.raises(AbsoluteFloorViolation):
        await issue(
            confirmations,
            db,
            binding_for(world.alice, capability="capability.self_grant"),
            tier=RiskCategory.HIGH_IRREVERSIBLE,
        )

    with pytest.raises(AbsoluteFloorViolation):
        await issue(
            confirmations,
            db,
            binding_for(
                world.alice,
                capability=None,
                operation=Operation.SHARE,
                resource_type=ResourceType.SECRET_REFERENCE,
            ),
        )


async def test_no_token_row_is_written_for_a_refused_floor_action(
    confirmations, db, world
):
    from sqlalchemy import select

    from server.storage.models import ConfirmationToken

    with pytest.raises(AbsoluteFloorViolation):
        await issue(
            confirmations, db, binding_for(world.alice, capability="superuser.assume")
        )

    assert (await db.execute(select(ConfirmationToken))).scalars().all() == []


async def test_an_automatic_tier_action_gets_no_token(confirmations, db, world):
    """A token for something that needed no confirmation would be a spare
    credential lying around, and would suggest confirmation is advisory."""

    for tier in (RiskCategory.LOW_READ, RiskCategory.LOW_WRITE):
        with pytest.raises(ConfirmationRefused):
            await issue(confirmations, db, binding_for(world.alice), tier=tier)


# ── the engine's use of confirmation (RT-T3 / TL-T4) ──────────────────────


async def test_tl_t4_a_consequential_action_requires_confirmation_and_does_not_execute(
    engine, db, audit, world
):
    """TL-T4 / RT-T3 — a consequential operation returns `require_confirmation`,
    and nothing happens until a matching token arrives."""

    from tests.security_core.test_authorization import principal_for

    request = AccessRequest(
        principal=principal_for(world.alice, graph_id=world.graph_id),
        operation=Operation.SHARE,
        resource_type=ResourceType.FILERESOURCE,
        resource_ref=str(world.alice_private_file.file_id),
        graph_id=world.graph_id,
        task_id="task-share-1",
    )

    outcome = await engine.authorize(db, request, audit=audit)

    assert outcome.decision is PermissionDecisionValue.REQUIRE_CONFIRMATION
    assert outcome.risk_category is RiskCategory.CONSEQUENTIAL
    assert outcome.confirmation_required_for is not None
    # The resource was not touched.
    assert world.alice_private_file.visibility is Visibility.PRIVATE


async def test_a_matching_confirmation_lets_the_engine_allow(
    engine, db, audit, world, confirmations
):
    """The positive path: the engine issues a binding, the human confirms, and the
    next authorization with that token allows exactly that action."""

    from tests.security_core.test_authorization import principal_for

    principal = principal_for(world.alice, graph_id=world.graph_id)
    base = dict(
        principal=principal,
        operation=Operation.SHARE,
        resource_type=ResourceType.FILERESOURCE,
        resource_ref=str(world.alice_private_file.file_id),
        graph_id=world.graph_id,
        task_id="task-share-1",
    )

    first = await engine.authorize(db, AccessRequest(**base), audit=audit)
    assert first.decision is PermissionDecisionValue.REQUIRE_CONFIRMATION

    issued = await confirmations.issue(
        db,
        binding=first.confirmation_required_for,
        risk_category=first.risk_category,
    )

    confirmed = await engine.authorize(
        db, AccessRequest(**base, confirmation_token=issued.token), audit=audit
    )
    assert confirmed.allowed
    assert confirmed.reason == "allowed_with_confirmation"


async def test_a_confirmation_token_is_spent_by_the_authorization_that_uses_it(
    engine, db, audit, world, confirmations
):
    """Validating and spending cannot be separate steps that a retry repeats."""

    from tests.security_core.test_authorization import principal_for

    base = dict(
        principal=principal_for(world.alice, graph_id=world.graph_id),
        operation=Operation.SHARE,
        resource_type=ResourceType.FILERESOURCE,
        resource_ref=str(world.alice_private_file.file_id),
        graph_id=world.graph_id,
        task_id="task-share-1",
    )
    first = await engine.authorize(db, AccessRequest(**base), audit=audit)
    issued = await confirmations.issue(
        db, binding=first.confirmation_required_for, risk_category=first.risk_category
    )

    assert (
        await engine.authorize(
            db, AccessRequest(**base, confirmation_token=issued.token), audit=audit
        )
    ).allowed

    replayed = await engine.authorize(
        db, AccessRequest(**base, confirmation_token=issued.token), audit=audit
    )
    assert replayed.decision is PermissionDecisionValue.REQUIRE_CONFIRMATION


async def test_a_confirmation_for_one_file_does_not_authorize_another(
    engine, db, audit, world, confirmations
):
    """The engine-level version of the substitution attack."""

    from tests.security_core.test_authorization import principal_for

    principal = principal_for(world.alice, graph_id=world.graph_id)
    shared_base = dict(
        principal=principal,
        operation=Operation.SHARE,
        resource_type=ResourceType.FILERESOURCE,
        graph_id=world.graph_id,
        task_id="task-share-1",
    )

    first = await engine.authorize(
        db,
        AccessRequest(**shared_base, resource_ref=str(world.alice_private_file.file_id)),
        audit=audit,
    )
    issued = await confirmations.issue(
        db, binding=first.confirmation_required_for, risk_category=first.risk_category
    )

    # Same principal, same task, same operation — different file.
    other = await engine.authorize(
        db,
        AccessRequest(
            **shared_base,
            resource_ref=str(world.alice_shared_file.file_id),
            confirmation_token=issued.token,
        ),
        audit=audit,
    )
    assert other.decision is PermissionDecisionValue.REQUIRE_CONFIRMATION


async def test_a_denied_request_is_never_upgraded_by_a_confirmation_token(
    engine, db, audit, world, confirmations
):
    """Confirmation is the last gate, not a bypass of the earlier ones.

    Bob holds a genuine confirmation token for his own action; presenting it on a
    request that fails D3/D4 must still be denied. Otherwise a human's approval
    would substitute for authorization.
    """

    from tests.security_core.test_authorization import principal_for

    bob = principal_for(world.bob, graph_id=world.graph_id)

    own = await engine.authorize(
        db,
        AccessRequest(
            principal=bob,
            operation=Operation.SHARE,
            resource_type=ResourceType.FILERESOURCE,
            resource_ref=str(world.bob_private_file.file_id),
            graph_id=world.graph_id,
            task_id="t",
        ),
        audit=audit,
    )
    issued = await confirmations.issue(
        db, binding=own.confirmation_required_for, risk_category=own.risk_category
    )

    # Same token, aimed at Alice's file.
    outcome = await engine.authorize(
        db,
        AccessRequest(
            principal=bob,
            operation=Operation.SHARE,
            resource_type=ResourceType.FILERESOURCE,
            resource_ref=str(world.alice_shared_file.file_id),
            graph_id=world.graph_id,
            task_id="t",
            confirmation_token=issued.token,
        ),
        audit=audit,
    )
    assert outcome.decision is PermissionDecisionValue.DENY
    assert outcome.reason == "owner_only"


async def test_a_floor_request_never_reaches_the_confirmation_branch(
    engine, db, audit, world, store
):
    """The engine checks the floor *before* computing a tier, so a prohibited
    action cannot be weighed, tiered, or offered as confirmable.

    The secret is real and owned by the requesting principal, so the request
    passes D1–D5 and reaches the floor gate. Using a nonexistent handle would make
    this pass on a 404 and prove nothing about the ordering.
    """

    from server.secrets.requester import SecretRequester
    from shared.schemas.authorization import DenialSurface
    from shared.schemas.enums import SecretClass, SecretOwnerScopeType
    from tests.security_core.test_authorization import principal_for

    secret_ref = await store.set(
        db,
        owner_scope_type=SecretOwnerScopeType.USER,
        owner_scope_id=str(world.alice.user_id),
        secret_class=SecretClass.OAUTH_TOKEN,
        value="TEST-ONLY-value",
        requester=SecretRequester.server(),
        audit=audit,
    )

    outcome = await engine.authorize(
        db,
        AccessRequest(
            principal=principal_for(world.alice, graph_id=world.graph_id),
            operation=Operation.SHARE,
            resource_type=ResourceType.SECRET_REFERENCE,
            resource_ref=secret_ref,
            graph_id=world.graph_id,
        ),
        audit=audit,
    )
    assert outcome.decision is PermissionDecisionValue.DENY
    assert outcome.surface is DenialSurface.PROHIBITED
    assert outcome.reason.startswith("prohibited:")
    # Never offered as confirmable, even though `share` is a consequential tier.
    assert outcome.confirmation_required_for is None
