"""07's acceptance hooks — TL-T2, TL-T3, TL-T5, TL-T8, TL-T10 — plus the
capability-model guarantees from §11 of the security-core scope.

The through-line: **a model cannot manufacture a capability, modify a grant, or
grant itself permission**, and `AgentConfiguration` never becomes a privilege
escalation mechanism.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from server.capabilities.floor import (
    AbsoluteFloorCategory,
    AbsoluteFloorViolation,
    assert_not_absolute_floor,
    floor_category_for_capability,
    floor_category_for_request,
)
from server.capabilities.grants import CapabilityGrantRefused
from server.capabilities.policy import FloorPolicyAdapter, RiskPolicyAdapter
from server.capabilities.registry import (
    CAPABILITY_REGISTRY,
    UnknownCapability,
    UnknownOperation,
    lookup,
)
from server.capabilities.risk import Disposition, disposition, requires_confirmation, risk_tier
from server.gateway.app import API_V1_PREFIX
from shared.schemas.authorization import (
    CapabilityCheckContext,
    Operation,
    Principal,
    ResourceType,
)
from shared.schemas.enums import CapabilityScopeType, RiskCategory

pytestmark = pytest.mark.asyncio


def principal_for(user) -> Principal:
    return Principal(
        user_id=user.user_id, device_id=uuid.uuid4(), session_id=uuid.uuid4()
    )


def context_for(user, **kwargs) -> CapabilityCheckContext:
    return CapabilityCheckContext(principal=principal_for(user), **kwargs)


# ── TL-T2: an operation without its capability is denied ───────────────────


async def test_tl_t2_absent_grant_denies(db, grants, world):
    """TL-T2 (PERM-002) — no grant, no capability."""

    assert not await grants.has_capability(
        db, capability="file.read", context=context_for(world.alice)
    )


async def test_tl_t2_explicit_grant_allows(db, grants, world):
    await grants.grant(
        db,
        principal_id=world.alice.user_id,
        scope_type=CapabilityScopeType.USER,
        capability="file.read",
        granted_by=world.alice.user_id,
    )
    assert await grants.has_capability(
        db, capability="file.read", context=context_for(world.alice)
    )


async def test_revoked_grant_denies_immediately(db, grants, world):
    """PRD §13 — "Revocation is immediate: […] the next device operation for that
    app fails authorization"."""

    grant = await grants.grant(
        db,
        principal_id=world.alice.user_id,
        scope_type=CapabilityScopeType.USER,
        capability="file.read",
        granted_by=world.alice.user_id,
    )
    context = context_for(world.alice)
    assert await grants.has_capability(db, capability="file.read", context=context)

    await grants.revoke(db, grant_id=grant.grant_id, revoked_by=world.alice.user_id)

    assert not await grants.has_capability(db, capability="file.read", context=context)


async def test_expired_grant_denies(db, grants, world):
    """01 §7.1 — `expires_at` in the past is not authority. An already-expired
    grant cannot even be created, so this expires one after the fact."""

    from server.auth.repository import utcnow

    grant = await grants.grant(
        db,
        principal_id=world.alice.user_id,
        scope_type=CapabilityScopeType.USER,
        capability="file.read",
        granted_by=world.alice.user_id,
        expires_at=utcnow() + timedelta(hours=1),
    )
    context = context_for(world.alice)
    assert await grants.has_capability(db, capability="file.read", context=context)

    grant.expires_at = utcnow() - timedelta(seconds=1)
    await db.flush()

    assert not await grants.has_capability(db, capability="file.read", context=context)


async def test_an_already_expired_grant_cannot_be_created(db, grants, world):
    from server.auth.repository import utcnow

    with pytest.raises(CapabilityGrantRefused):
        await grants.grant(
            db,
            principal_id=world.alice.user_id,
            scope_type=CapabilityScopeType.USER,
            capability="file.read",
            granted_by=world.alice.user_id,
            expires_at=utcnow() - timedelta(seconds=1),
        )


# ── wrong scope ────────────────────────────────────────────────────────────


async def test_a_grant_for_another_principal_does_not_apply(db, grants, world):
    """A grant is keyed on a principal; another user's grant is not authority."""

    await grants.grant(
        db,
        principal_id=world.bob.user_id,
        scope_type=CapabilityScopeType.USER,
        capability="file.read",
        granted_by=world.bob.user_id,
    )
    assert not await grants.has_capability(
        db, capability="file.read", context=context_for(world.alice)
    )


async def test_a_graph_scoped_grant_does_not_apply_outside_that_graph(db, grants, world):
    """01 §7.1 — `principal_id` is the id of whatever `scope_type` names, so a
    graph-scoped grant applies only inside that graph. Using it elsewhere would
    widen what the user consented to."""

    await grants.grant(
        db,
        principal_id=world.graph_id,
        scope_type=CapabilityScopeType.GRAPH,
        capability="file.read",
        granted_by=world.alice.user_id,
    )

    assert await grants.has_capability(
        db,
        capability="file.read",
        context=context_for(world.alice, graph_id=world.graph_id),
    )
    # No graph context at all: the grant does not apply.
    assert not await grants.has_capability(
        db, capability="file.read", context=context_for(world.alice)
    )
    # A different graph: likewise.
    assert not await grants.has_capability(
        db, capability="file.read", context=context_for(world.alice, graph_id=uuid.uuid4())
    )


async def test_resource_scope_narrowing_binds(db, grants, world):
    """07 §2 — a grant's `resource_scope` narrows it, and an operation that
    cannot prove it stays inside is denied rather than assumed to be."""

    await grants.grant(
        db,
        principal_id=world.alice.user_id,
        scope_type=CapabilityScopeType.USER,
        capability="app.interact",
        granted_by=world.alice.user_id,
        resource_scope={"package_name": "com.example.allowed"},
    )

    assert await grants.has_capability(
        db,
        capability="app.interact",
        context=context_for(world.alice, resource_scope={"package_name": "com.example.allowed"}),
        capability_operation="tap",
    )
    # A different app.
    assert not await grants.has_capability(
        db,
        capability="app.interact",
        context=context_for(world.alice, resource_scope={"package_name": "com.example.other"}),
        capability_operation="tap",
    )
    # No narrowing declared: unprovable, therefore denied.
    assert not await grants.has_capability(
        db,
        capability="app.interact",
        context=context_for(world.alice),
        capability_operation="tap",
    )


async def test_a_grant_cannot_be_scoped_by_an_unknown_dimension(db, grants, world):
    """Silently ignoring an unrecognised scope key would grant *more* than the
    user asked for — a client meaning "only this app" that misspells the key
    would otherwise receive an unscoped grant."""

    with pytest.raises(CapabilityGrantRefused):
        await grants.grant(
            db,
            principal_id=world.alice.user_id,
            scope_type=CapabilityScopeType.USER,
            capability="app.interact",
            granted_by=world.alice.user_id,
            resource_scope={"packagename": "com.example"},  # typo
        )


# ── TL-T5: the absolute floor (PERM-006) ───────────────────────────────────


@pytest.mark.parametrize(
    ("capability", "expected"),
    [
        ("superuser", AbsoluteFloorCategory.OBTAIN_SUPERUSER_CREDENTIALS),
        ("superuser.assume", AbsoluteFloorCategory.OBTAIN_SUPERUSER_CREDENTIALS),
        ("superuser.anything.at.all", AbsoluteFloorCategory.OBTAIN_SUPERUSER_CREDENTIALS),
        ("secret.master_key", AbsoluteFloorCategory.OBTAIN_MASTER_KEYS),
        ("secret.master_key.read", AbsoluteFloorCategory.OBTAIN_MASTER_KEYS),
        ("secret.read_raw", AbsoluteFloorCategory.EXFILTRATE_CREDENTIALS),
        ("secret.export", AbsoluteFloorCategory.EXFILTRATE_CREDENTIALS),
        ("audit.disable", AbsoluteFloorCategory.DISABLE_AUTH_OR_AUDIT),
        ("auth.disable", AbsoluteFloorCategory.DISABLE_AUTH_OR_AUDIT),
        ("authz.bypass", AbsoluteFloorCategory.DISABLE_AUTH_OR_AUDIT),
        ("capability.self_grant", AbsoluteFloorCategory.SELF_ESCALATE),
        ("capability.escalate", AbsoluteFloorCategory.SELF_ESCALATE),
        ("sandbox.escape", AbsoluteFloorCategory.ESCAPE_SANDBOX),
        ("fs.host_root", AbsoluteFloorCategory.ESCAPE_SANDBOX),
        ("graph.read_private", AbsoluteFloorCategory.READ_ANOTHER_USERS_PRIVATE_DATA),
        ("user.impersonate", AbsoluteFloorCategory.READ_ANOTHER_USERS_PRIVATE_DATA),
        # Presentation must not defeat the classification.
        ("  SUPERUSER  ", AbsoluteFloorCategory.OBTAIN_SUPERUSER_CREDENTIALS),
        ("Secret.Master_Key", AbsoluteFloorCategory.OBTAIN_MASTER_KEYS),
    ],
)
async def test_tl_t5_floor_capabilities_are_classified(capability, expected):
    """PRD §16's seven categories, each recognised by name.

    Classification matters beyond refusal: an escalation attempt logged as an
    unknown-capability typo is an incident nobody reviews.
    """

    assert floor_category_for_capability(capability) is expected


async def test_tl_t5_no_grant_for_a_floor_capability_can_be_created(db, grants, world):
    """TL-T5 / DM-T9 (01 §7.1 `[LOCKED]`) — "attempting to create one is a hard
    error, not a stored-but-denied grant"."""

    from sqlalchemy import select

    from server.storage.models import CapabilityGrant

    for capability in ("superuser", "secret.master_key", "capability.self_grant"):
        with pytest.raises(AbsoluteFloorViolation):
            await grants.grant(
                db,
                principal_id=world.alice.user_id,
                scope_type=CapabilityScopeType.USER,
                capability=capability,
                granted_by=world.alice.user_id,
            )

    # Nothing was written on any of those paths.
    rows = (await db.execute(select(CapabilityGrant))).scalars().all()
    assert rows == []


async def test_tl_t5_a_floor_capability_is_never_active_even_if_a_row_exists(
    db, grants, world
):
    """Defense in depth: a row inserted by a direct database edit (bypassing the
    grant service entirely) still authorizes nothing."""

    import datetime

    from server.storage.models import CapabilityGrant

    db.add(
        CapabilityGrant(
            principal_id=world.alice.user_id,
            scope_type=CapabilityScopeType.USER,
            capability="capability.self_grant",
            granted_by=world.alice.user_id,
            created_at=datetime.datetime.now(datetime.timezone.utc),
        )
    )
    await db.flush()

    assert not await grants.has_capability(
        db, capability="capability.self_grant", context=context_for(world.alice)
    )


async def test_the_registry_contains_no_floor_capability(db):
    """The primary enforcement is the closed allow-list (PRD §16's "prohibited
    because no capability grants them"), so no registry entry may describe a
    floor action."""

    for name in CAPABILITY_REGISTRY:
        assert floor_category_for_capability(name) is None, name
        assert_not_absolute_floor(name)


async def test_an_unregistered_capability_grants_nothing(db, grants, world):
    """Fail-closed: an unrecognised capability is a denial, never a default."""

    with pytest.raises(CapabilityGrantRefused):
        await grants.grant(
            db,
            principal_id=world.alice.user_id,
            scope_type=CapabilityScopeType.USER,
            capability="file.invent_a_new_power",
            granted_by=world.alice.user_id,
        )

    assert not await grants.has_capability(
        db, capability="file.invent_a_new_power", context=context_for(world.alice)
    )
    with pytest.raises(UnknownCapability):
        lookup("file.invent_a_new_power")


async def test_sharing_a_secret_reference_is_an_absolute_floor_request(db):
    """GRAPH-009 / AZ-T5 — every mutating operation on a secret reference through
    the generic resource path is a floor request, because the SecretStore's own
    mediated interface is the only door onto credential material."""

    for operation in (
        Operation.SHARE,
        Operation.WRITE,
        Operation.DELETE,
        Operation.CREATE,
        Operation.ADMINISTER,
    ):
        assert (
            floor_category_for_request(
                capability=None,
                operation=operation,
                resource_type=ResourceType.SECRET_REFERENCE,
            )
            is AbsoluteFloorCategory.EXFILTRATE_CREDENTIALS
        ), operation

    # Reading one through the engine is not itself a floor action — it is simply
    # governed by ownership and the store's own mediation.
    assert (
        floor_category_for_request(
            capability=None,
            operation=Operation.READ,
            resource_type=ResourceType.SECRET_REFERENCE,
        )
        is None
    )


async def test_ordinary_capabilities_are_not_floor_requests():
    for name in CAPABILITY_REGISTRY:
        assert (
            floor_category_for_request(
                capability=name,
                operation=Operation.READ,
                resource_type=ResourceType.FILERESOURCE,
            )
            is None
        ), name


# ── TL-T8: an operation outside the mapping is not executable ──────────────


async def test_tl_t8_operation_outside_the_capability_mapping_is_not_executable(
    db, grants, world
):
    """TL-T8 (07 §3 `[LOCKED]`) — "An operation not in the mapping is not
    executable, even with the capability."

    `file.read` is granted; `delete_file` is not in its mapping. A name-based
    check would allow it.
    """

    await grants.grant(
        db,
        principal_id=world.alice.user_id,
        scope_type=CapabilityScopeType.USER,
        capability="file.read",
        granted_by=world.alice.user_id,
    )

    assert await grants.has_capability(
        db,
        capability="file.read",
        context=context_for(world.alice),
        capability_operation="read_file",
    )
    assert not await grants.has_capability(
        db,
        capability="file.read",
        context=context_for(world.alice),
        capability_operation="delete_file",
    )
    assert not await grants.has_capability(
        db,
        capability="file.read",
        context=context_for(world.alice),
        capability_operation="write_file",
    )


async def test_file_read_does_not_grant_universal_crud():
    """07 §3 / AND-006 — a capability that *sounds* like universal CRUD is not.

    Asserted as a property of the registry rather than of one call: every
    operation `file.read` exposes is a read.
    """

    definition = lookup("file.read")
    assert set(definition.operations) == {"read_file", "list_directory", "stat"}
    for operation in definition.operations:
        assert definition.risk_for(operation) is RiskCategory.LOW_READ

    with pytest.raises(UnknownOperation):
        definition.risk_for("write_file")


async def test_every_registered_operation_has_a_tier():
    """PERM-007 — "every operation is exactly one of the three"."""

    for name, definition in CAPABILITY_REGISTRY.items():
        assert definition.operations, f"{name} enumerates no operations"
        for operation, tier in definition.operations.items():
            assert isinstance(tier, RiskCategory), (name, operation)
            assert disposition(tier) in {
                Disposition.AUTOMATIC,
                Disposition.REQUIRE_CONFIRMATION,
            }


async def test_system_restricted_is_never_automatic():
    """07 §2 / 08 — the high-risk family is kept separate, and nothing in it is
    automatic."""

    definition = lookup("system.restricted")
    for operation, tier in definition.operations.items():
        assert tier is RiskCategory.HIGH_IRREVERSIBLE, operation
        assert requires_confirmation(tier)


# ── TL-T10: the tier is produced by policy, not by a model ─────────────────


async def test_tl_t10_the_risk_policy_takes_no_model_input():
    """TL-T10 (PERM-005) — "the confirmation decision is produced by the
    deterministic policy, not the model".

    Asserted structurally: the policy's signature has no parameter through which
    a score, a confidence, or free-form text could arrive. A test that only
    checked a returned tier would still pass if such a parameter were added.
    """

    import inspect

    parameters = set(inspect.signature(RiskPolicyAdapter().risk_tier).parameters)
    # `resource_scope` is the operation's own deterministic narrowing (which
    # app it acts in) — what the sensitive-app classification is keyed on
    # (docs/CAPABILITY_MATRIX.md §5.1: "keyed on resource_scope, deterministic,
    # and never model-judged").
    assert parameters == {
        "resource_type",
        "operation",
        "capability_name",
        "capability_operation",
        "resource_scope",
    }
    scope_parameters = set(inspect.signature(RiskPolicyAdapter().scope_denial).parameters)
    assert scope_parameters == {"capability_name", "capability_operation", "resource_scope"}
    for forbidden in ("confidence", "score", "model", "rationale", "llm", "suggestion"):
        assert forbidden not in parameters
        assert forbidden not in scope_parameters

    floor_parameters = set(
        inspect.signature(FloorPolicyAdapter().floor_category_for_request).parameters
    )
    assert floor_parameters == {"capability", "operation", "resource_type"}


async def test_the_tier_table_is_deterministic():
    """The same action always yields the same tier — no randomness, no clock, no
    hidden state."""

    calls = [
        risk_tier(
            resource_type=ResourceType.FILERESOURCE,
            operation=Operation.DELETE,
            capability=lookup("file.write"),
            capability_operation="delete_file",
        )
        for _ in range(5)
    ]
    assert len(set(calls)) == 1
    assert calls[0] is RiskCategory.CONSEQUENTIAL


async def test_the_more_restrictive_of_the_two_axes_wins():
    """A low-tier resource operation must not launder a high-tier concrete
    primitive, or vice versa."""

    # A `write` operation is low_write on the resource axis, but the concrete
    # primitive is irreversible — the composition must take the higher.
    assert (
        risk_tier(
            resource_type=ResourceType.FILERESOURCE,
            operation=Operation.WRITE,
            capability=lookup("system.restricted"),
            capability_operation="run_shell_command",
        )
        is RiskCategory.HIGH_IRREVERSIBLE
    )

    # And a low-tier primitive does not lower a consequential resource operation.
    assert (
        risk_tier(
            resource_type=ResourceType.FILERESOURCE,
            operation=Operation.SHARE,
            capability=lookup("file.read"),
            capability_operation="read_file",
        )
        is RiskCategory.CONSEQUENTIAL
    )


async def test_reads_are_automatic_and_consequential_actions_are_not():
    """PRD §15's dispositions."""

    assert disposition(RiskCategory.LOW_READ) is Disposition.AUTOMATIC
    assert disposition(RiskCategory.LOW_WRITE) is Disposition.AUTOMATIC
    assert disposition(RiskCategory.CONSEQUENTIAL) is Disposition.REQUIRE_CONFIRMATION
    assert disposition(RiskCategory.HIGH_IRREVERSIBLE) is Disposition.REQUIRE_CONFIRMATION


async def test_disposition_never_returns_never():
    """PERM-006's placement: a prohibited action is not a fourth tier that could
    be weighed. The floor is checked before a tier is ever computed, so
    `disposition` has no input that yields `NEVER`."""

    for tier in RiskCategory:
        assert disposition(tier) is not Disposition.NEVER


# ── AgentConfiguration is never authority (§4/§11 of the scope) ────────────


async def test_agent_configuration_cannot_grant_authority(db, grants, world):
    """`01` §4.1 calls `granted_capabilities` "the resolved set"; §7.1 names
    `CapabilityGrant` "the authoritative record".

    An `AgentConfiguration` listing a capability with no matching grant therefore
    authorizes nothing. This is the escalation path that would exist if the check
    read the config for convenience.
    """

    import datetime

    from server.storage.models import AgentConfiguration
    from shared.schemas.enums import AgentConfigScopeType

    db.add(
        AgentConfiguration(
            scope_type=AgentConfigScopeType.USER,
            scope_id=world.alice.user_id,
            primary_model={"provider": "ollama", "model": "test"},
            granted_capabilities=["file.read", "file.write", "system.restricted"],
            updated_at=datetime.datetime.now(datetime.timezone.utc),
        )
    )
    await db.flush()

    for capability in ("file.read", "file.write", "system.restricted"):
        assert not await grants.has_capability(
            db, capability=capability, context=context_for(world.alice)
        ), capability


async def test_the_grant_check_never_reads_agent_configuration():
    """The structural version of the rule above: `grants.py` does not import or
    reference `AgentConfiguration` at all, so a config cannot become authority by
    a later edit that "resolves" it."""

    from pathlib import Path

    source = Path("server/capabilities/grants.py").read_text()
    assert "AgentConfiguration" not in source.split('"""', 2)[-1]


# ── HTTP surface: a floor grant is refused, a model cannot self-escalate ───


async def test_posting_a_floor_capability_returns_prohibited_and_creates_nothing(api):
    """02 §6 `[LOCKED]` — "A `POST /capabilities` for an absolute-floor capability
    (PERM-006) returns `prohibited` and creates no grant"."""

    from sqlalchemy import select

    from server.storage.models import CapabilityGrant

    onboarded = await api.onboard()
    resp = await api.client.post(
        f"{API_V1_PREFIX}/capabilities",
        json={
            "capability": "capability.self_grant",
            "scope_type": "user",
            "scope_id": str(await _user_id_of(api, onboarded)),
        },
        headers=onboarded.auth,
    )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "prohibited"
    assert resp.json()["error"]["details"]["category"] == "self_escalate"

    async with api.storage.session() as session:
        assert (await session.execute(select(CapabilityGrant))).scalars().all() == []


async def test_a_user_cannot_grant_a_capability_to_another_users_device(api):
    """02 §1.1 — the `scope_id` in the body is a claim to validate. Without that
    check, any authenticated user could mint a grant on another user's device."""

    alice = await api.onboard(subject="alice-subject")
    bob = await api.onboard(subject="bob-subject")

    resp = await api.client.post(
        f"{API_V1_PREFIX}/capabilities",
        json={
            "capability": "file.read",
            "scope_type": "device",
            "scope_id": str(alice.device_id),
        },
        headers=bob.auth,
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "unauthorized"


async def test_capability_listing_shows_only_the_callers_own_grants(api):
    alice = await api.onboard(subject="alice-subject")
    bob = await api.onboard(subject="bob-subject")

    created = await api.client.post(
        f"{API_V1_PREFIX}/capabilities",
        json={
            "capability": "file.read",
            "scope_type": "user",
            "scope_id": str(await _user_id_of(api, alice)),
        },
        headers=alice.auth,
    )
    assert created.status_code == 201

    mine = await api.client.get(f"{API_V1_PREFIX}/capabilities", headers=alice.auth)
    assert len(mine.json()["items"]) == 1

    theirs = await api.client.get(f"{API_V1_PREFIX}/capabilities", headers=bob.auth)
    assert theirs.json()["items"] == []


async def test_revoking_another_users_grant_reports_not_found(api):
    """04 §7 — a caller must not confirm another principal's grant exists by
    trying to revoke it."""

    alice = await api.onboard(subject="alice-subject")
    bob = await api.onboard(subject="bob-subject")

    created = await api.client.post(
        f"{API_V1_PREFIX}/capabilities",
        json={
            "capability": "file.read",
            "scope_type": "user",
            "scope_id": str(await _user_id_of(api, alice)),
        },
        headers=alice.auth,
    )
    grant_id = created.json()["grant_id"]

    resp = await api.client.delete(
        f"{API_V1_PREFIX}/capabilities/{grant_id}", headers=bob.auth
    )
    assert resp.status_code == 404

    # Alice's grant is untouched.
    still = await api.client.get(f"{API_V1_PREFIX}/capabilities", headers=alice.auth)
    assert len(still.json()["items"]) == 1


async def _user_id_of(api, onboarded) -> uuid.UUID:
    from sqlalchemy import select

    from server.storage.models import AccessToken

    async with api.storage.session() as session:
        row = (
            await session.execute(
                select(AccessToken).where(AccessToken.device_id == onboarded.device_id)
            )
        ).scalars().first()
        return row.user_id
