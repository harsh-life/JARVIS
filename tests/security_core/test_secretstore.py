"""12's acceptance hooks — SS-T1..SS-T10.

17 §5 puts SS-T1, SS-T2 and SS-T3 in the release-blocking "secret containment"
set. 12 §6 is blunt about the standard: "A single leak of a secret into any of
these is a release-blocking defect."
"""

from __future__ import annotations

import uuid

import pytest

from server.secrets.crypto import aead_decrypt, hash_token
from server.secrets.errors import (
    SecretDenied,
    SecretIntegrityError,
    SecretNotFound,
    SecretStoreLocked,
)
from server.secrets.kek import resolve_kek
from server.secrets.requester import RequesterKind, SecretRequester, SuperuserGrant
from server.secrets.store import EncryptedLocalSecretStore
from server.security.superuser import (
    SUPERUSER_TOKEN_ENV,
    SuperuserAuthenticationFailed,
    SuperuserNotConfigured,
    authenticate_superuser,
)
from server.storage.models import SecretMaterial, SecretReference
from shared.schemas.enums import SecretClass, SecretOwnerScopeType
from tests.security_core.helpers import database_contains
from tests.support import TEST_KEK_ENV_VAR

pytestmark = pytest.mark.asyncio

# A clearly-marked test value (17 §6: "test secrets are clearly-marked test
# values"). Never a plausible-looking real credential.
TEST_SECRET_VALUE = "TEST-ONLY-not-a-real-credential-9f2c"


async def set_user_secret(store, db, audit, *, user_id, value=TEST_SECRET_VALUE, cls=None):
    return await store.set(
        db,
        owner_scope_type=SecretOwnerScopeType.USER,
        owner_scope_id=str(user_id),
        secret_class=cls or SecretClass.OAUTH_TOKEN,
        value=value,
        requester=SecretRequester.server(),
        audit=audit,
    )


# ── SS-T1: the leak test (release-blocking) ────────────────────────────────


async def test_ss_t1_secret_value_never_appears_anywhere_in_the_database(
    store, db, audit, world
):
    """SS-T1 (SECRET-004) — the value appears in no table, no audit record, and
    no metadata row.

    The sweep is exhaustive rather than targeted (see `helpers.database_contains`):
    the failure SECRET-004 guards against is a leak into a column nobody thought
    to check.
    """

    secret_ref = await set_user_secret(store, db, audit, user_id=world.alice.user_id)
    await db.flush()

    assert not await database_contains(db, TEST_SECRET_VALUE)

    # The handle itself is of course present — that is the whole design.
    assert await database_contains(db, secret_ref)


async def test_ss_t1_secret_value_is_absent_from_audit_records(store, db, audit, world):
    """12 §5 — "with the secret **value** never in the audit record"."""

    from server.storage.models import AuditEvent

    secret_ref = await set_user_secret(store, db, audit, user_id=world.alice.user_id)
    await store.get(db, secret_ref, SecretRequester.user(world.alice.user_id), audit)
    await db.flush()

    from sqlalchemy import select

    events = (await db.execute(select(AuditEvent))).scalars().all()
    secret_events = [e for e in events if e.action.startswith("secret.")]
    assert secret_events, "secret lifecycle produced no audit events (12 §5)"
    for event in secret_events:
        assert TEST_SECRET_VALUE not in event.resource
        assert TEST_SECRET_VALUE not in event.action


async def test_ss_t1_metadata_row_has_no_column_able_to_hold_a_value(store, db, audit, world):
    """The structural half of SS-T1: `secret_references` cannot hold a value even
    in principle, so the separation is not a convention that could drift.

    Foundation asserts this too; it is re-asserted here because security-core is
    the branch that started writing to the table, and a new "cached_value" column
    added for convenience is exactly how this guarantee would be lost.
    """

    columns = set(SecretReference.__table__.columns.keys())
    assert columns == {
        "secret_ref",
        "owner_scope_type",
        "owner_scope_id",
        "class",
        "created_at",
        "rotated_at",
        "revoked_at",
    }


# ── SS-T2: the agent can only hold a handle (release-blocking) ─────────────


async def test_ss_t2_agent_requester_is_refused_unconditionally(store, db, audit, world):
    """SS-T2 (SECRET-002, INV-6) — "the agent can reference a secret only by
    handle and cannot call `get`".

    Refused for every scope and class, including a secret that would otherwise
    resolve fine — the denial is about *who is asking*, not about the handle.
    """

    secret_ref = await set_user_secret(store, db, audit, user_id=world.alice.user_id)

    # It resolves for a legitimate requester...
    assert (
        await store.get(db, secret_ref, SecretRequester.user(world.alice.user_id), audit)
        == TEST_SECRET_VALUE
    )

    # ...and never for the agent.
    with pytest.raises(SecretDenied):
        await store.get(db, secret_ref, SecretRequester.agent(), audit)


async def test_ss_t2_agent_cannot_store_rotate_or_delete_either(store, db, audit, world):
    """The same absence in the other direction: an agent that could *write* a
    secret could seed material a boundary resolver would later use."""

    secret_ref = await set_user_secret(store, db, audit, user_id=world.alice.user_id)

    with pytest.raises(SecretDenied):
        await store.set(
            db,
            owner_scope_type=SecretOwnerScopeType.USER,
            owner_scope_id=str(world.alice.user_id),
            secret_class=SecretClass.OTHER,
            value="TEST-ONLY-agent-authored",
            requester=SecretRequester.agent(),
            audit=audit,
        )

    with pytest.raises(SecretDenied):
        await store.rotate(db, secret_ref, SecretRequester.agent(), audit)

    with pytest.raises(SecretDenied):
        await store.delete(db, secret_ref, SecretRequester.agent(), audit)


async def test_ss_t2_the_secrets_package_exposes_no_unmediated_resolver(store):
    """P2, "absence over restriction" (16 §3).

    16 §3 requires `server/secrets` to expose no "give me the raw value" function
    to general callers. The guarantee is an absence, so the test is an absence
    check: every public resolution path takes a `SecretRequester`, and there is no
    module-level shortcut that skips mediation.
    """

    import inspect

    import server.secrets as secrets_package

    for name in dir(secrets_package):
        if name.startswith("_"):
            continue
        attr = getattr(secrets_package, name)
        if inspect.isfunction(attr):
            params = set(inspect.signature(attr).parameters)
            # The only module-level functions are handle minting and KEK
            # plumbing; none of them returns secret material.
            assert name in {"new_handle", "resolve_kek", "generate_kek_value"}, name
            assert "requester" not in params

    # And `get` cannot be called without a requester.
    signature = inspect.signature(EncryptedLocalSecretStore.get)
    assert "requester" in signature.parameters
    assert signature.parameters["requester"].default is inspect.Parameter.empty


# ── SS-T3: a DB/backup leak without the KEK is useless (release-blocking) ──


async def test_ss_t3_stolen_database_without_the_kek_yields_no_plaintext(
    store, db, audit, world, monkeypatch
):
    """SS-T3 (12 §3) — "a DB/backup leak without the KEK yields no usable
    plaintext".

    Simulated as the real thing: a *fresh* store object (the attacker's process)
    over the *same* database rows, with no KEK. It cannot unlock, and the raw
    ciphertext it can read does not decrypt under a KEK it does not have.
    """

    secret_ref = await set_user_secret(store, db, audit, user_id=world.alice.user_id)
    await db.flush()

    # The attacker has the database file. They do not have the KEK.
    thief = EncryptedLocalSecretStore()
    assert not thief.is_unlocked

    with pytest.raises(SecretStoreLocked):
        await thief.get(db, secret_ref, SecretRequester.user(world.alice.user_id), audit)

    # Reading the ciphertext directly gets them nothing either.
    material = await db.get(SecretMaterial, secret_ref)
    assert material is not None
    assert TEST_SECRET_VALUE.encode() not in material.ciphertext

    # And a *wrong* KEK fails authentication rather than yielding garbage that
    # could be mistaken for a value.
    from server.secrets.kek import generate_kek_value

    monkeypatch.setenv("WRONG_KEK", generate_kek_value())
    with pytest.raises(SecretIntegrityError):
        await thief.unlock(db, resolve_kek("env:WRONG_KEK"))


async def test_ss_t3_the_kek_is_never_stored_in_the_database(store, db, audit, world, kek_value):
    """12 §3 — "The KEK is **not** stored in the application DB alongside the
    ciphertext (that would make a DB leak == plaintext)"."""

    await set_user_secret(store, db, audit, user_id=world.alice.user_id)
    await db.flush()

    assert not await database_contains(db, kek_value)

    raw_kek = resolve_kek(f"env:{TEST_KEK_ENV_VAR}")
    from server.storage.models import SecretStoreKey
    from sqlalchemy import select

    keys = (await db.execute(select(SecretStoreKey))).scalars().all()
    assert keys, "the store bootstrapped without a wrapped DEK"
    for row in keys:
        # The stored key is the DEK *wrapped* under the KEK — never the KEK.
        assert raw_kek not in row.wrapped_dek


async def test_ss_t9_tampered_ciphertext_fails_authentication(store, db, audit, world):
    """SS-T9 (12 §3/§8) — "a tampered ciphertext fails integrity, never silently
    decrypts"."""

    secret_ref = await set_user_secret(store, db, audit, user_id=world.alice.user_id)
    await db.flush()

    material = await db.get(SecretMaterial, secret_ref)
    material.ciphertext = material.ciphertext[:-1] + bytes([material.ciphertext[-1] ^ 0xFF])
    await db.flush()

    with pytest.raises(SecretIntegrityError):
        await store.get(db, secret_ref, SecretRequester.user(world.alice.user_id), audit)


async def test_ciphertext_cannot_be_moved_between_handles(store, db, audit, world):
    """The AAD binding (12 §3): a ciphertext is bound to its own handle.

    This is the attack available to someone who can write the database but not
    read the KEK — swap Bob's ciphertext onto Alice's handle and resolve it as
    Alice. Binding the handle as AEAD associated data makes that fail
    authentication.
    """

    alice_ref = await set_user_secret(
        store, db, audit, user_id=world.alice.user_id, value="TEST-ONLY-alice-value"
    )
    bob_ref = await set_user_secret(
        store, db, audit, user_id=world.bob.user_id, value="TEST-ONLY-bob-value"
    )
    await db.flush()

    alice_material = await db.get(SecretMaterial, alice_ref)
    bob_material = await db.get(SecretMaterial, bob_ref)
    alice_material.nonce, alice_material.ciphertext = bob_material.nonce, bob_material.ciphertext
    await db.flush()

    with pytest.raises(SecretIntegrityError):
        await store.get(db, alice_ref, SecretRequester.user(world.alice.user_id), audit)


# ── SS-T4: master keys are superuser-only (SUPER-001) ──────────────────────


async def test_ss_t4_master_key_is_unresolvable_by_user_tool_or_agent(
    store, db, audit, world
):
    """SS-T4 (SUPER-001) — a `class=master_key` reference is unresolvable by the
    agent or a user tool."""

    master_ref = await store.set(
        db,
        owner_scope_type=SecretOwnerScopeType.SERVER,
        owner_scope_id=None,
        secret_class=SecretClass.MASTER_KEY,
        value="TEST-ONLY-master-key-material",
        requester=SecretRequester.server(),
        audit=audit,
    )

    for requester in (
        SecretRequester.agent(),
        SecretRequester.user(world.alice.user_id),
        SecretRequester.tool("some.tool", declared_secret_refs={master_ref}),
        # Even server-owned deterministic code cannot resolve a master key: 12 §4
        # makes resolution superuser/bootstrap-only.
        SecretRequester.server(),
    ):
        with pytest.raises(SecretDenied):
            await store.get(db, master_ref, requester, audit)


async def test_ss_t4_master_key_resolves_only_for_a_verified_superuser(
    store, db, audit, monkeypatch
):
    """The positive half: superuser authority exists, and it is the only thing
    that resolves a master key."""

    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, "TEST-ONLY-superuser-token-of-sufficient-length")

    master_ref = await store.set(
        db,
        owner_scope_type=SecretOwnerScopeType.SERVER,
        owner_scope_id=None,
        secret_class=SecretClass.MASTER_KEY,
        value="TEST-ONLY-master-key-material",
        requester=SecretRequester.server(),
        audit=audit,
    )

    grant = authenticate_superuser("TEST-ONLY-superuser-token-of-sufficient-length")
    resolved = await store.get(db, master_ref, SecretRequester.superuser(grant), audit)
    assert resolved == "TEST-ONLY-master-key-material"


async def test_a_superuser_requester_cannot_be_forged_from_an_unverified_grant():
    """`SecretRequester.superuser` refuses an unverified grant, so a bare
    dataclass construction does not produce authority."""

    forged = SuperuserGrant(token_fingerprint="whatever")
    assert not forged.is_valid()
    with pytest.raises(ValueError):
        SecretRequester.superuser(forged)


async def test_superuser_authentication_rejects_a_wrong_or_absent_credential(monkeypatch):
    monkeypatch.delenv(SUPERUSER_TOKEN_ENV, raising=False)
    with pytest.raises(SuperuserNotConfigured):
        authenticate_superuser("anything")

    # A weak configured credential is refused rather than accepted (12 §4).
    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, "short")
    with pytest.raises(SuperuserNotConfigured):
        authenticate_superuser("short")

    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, "TEST-ONLY-superuser-token-of-sufficient-length")
    with pytest.raises(SuperuserAuthenticationFailed):
        authenticate_superuser("TEST-ONLY-wrong-token-of-sufficient-length!!")
    with pytest.raises(SuperuserAuthenticationFailed):
        authenticate_superuser("")


# ── SS-T5: graph sharing never exposes a secret (GRAPH-009) ───────────────


async def test_ss_t5_graph_scoped_secret_does_not_resolve_for_a_graph_member(
    store, db, audit, world
):
    """SS-T5 (GRAPH-009, 12 §2) — "graph sharing never exposes a
    `SecretReference`".

    Bob is an active member of the graph the secret is scoped to. Membership is
    deliberately absent from the resolution check, so no amount of it becomes a
    resolution.
    """

    graph_ref = await store.set(
        db,
        owner_scope_type=SecretOwnerScopeType.GRAPH,
        owner_scope_id=str(world.graph_id),
        secret_class=SecretClass.OAUTH_TOKEN,
        value=TEST_SECRET_VALUE,
        requester=SecretRequester.server(),
        audit=audit,
    )

    with pytest.raises(SecretDenied):
        await store.get(
            db, graph_ref, SecretRequester.user(world.bob.user_id, graph_id=world.graph_id), audit
        )
    with pytest.raises(SecretDenied):
        await store.get(
            db, graph_ref, SecretRequester.user(world.alice.user_id, graph_id=world.graph_id), audit
        )

    # Only deterministic server-owned work in that graph resolves it.
    assert (
        await store.get(db, graph_ref, SecretRequester.server(graph_id=world.graph_id), audit)
        == TEST_SECRET_VALUE
    )


# ── SS-T6 / SS-T7: rotation and revocation ────────────────────────────────


async def test_ss_t6_rotation_invalidates_the_old_value_atomically(store, db, audit, world):
    """SS-T6 (12 §5) — "rotation invalidates the old value atomically (no
    dual-valid window)".

    There is exactly one ciphertext row per handle, overwritten in place, so no
    moment exists at which both the old and new value verify.
    """

    requester = SecretRequester.user(world.alice.user_id)
    secret_ref = await set_user_secret(store, db, audit, user_id=world.alice.user_id)
    assert await store.get(db, secret_ref, requester, audit) == TEST_SECRET_VALUE

    returned_ref = await store.rotate(
        db, secret_ref, requester, audit, new_value="TEST-ONLY-rotated-value"
    )
    assert returned_ref == secret_ref  # callers keep their handle (12 §5)

    assert await store.get(db, secret_ref, requester, audit) == "TEST-ONLY-rotated-value"

    # The old plaintext is gone from storage entirely, not merely superseded.
    await db.flush()
    assert not await database_contains(db, TEST_SECRET_VALUE)

    reference = await db.get(SecretReference, secret_ref)
    assert reference.rotated_at is not None


async def test_ss_t7_revocation_makes_the_next_resolution_fail_immediately(
    store, db, audit, world
):
    """SS-T7 (12 §5) — revocation is immediate."""

    requester = SecretRequester.user(world.alice.user_id)
    secret_ref = await set_user_secret(store, db, audit, user_id=world.alice.user_id)
    assert await store.get(db, secret_ref, requester, audit) == TEST_SECRET_VALUE

    await store.delete(db, secret_ref, requester, audit)

    with pytest.raises((SecretDenied, SecretNotFound)):
        await store.get(db, secret_ref, requester, audit)

    # The reference survives so the revocation stays auditable and a replayed
    # handle is denied rather than merely looking absent.
    reference = await db.get(SecretReference, secret_ref)
    assert reference is not None and reference.revoked_at is not None

    await db.flush()
    assert not await database_contains(db, TEST_SECRET_VALUE)


# ── SS-T8: locked store fails closed ──────────────────────────────────────


async def test_ss_t8_locked_store_fails_closed_with_no_raw_fallback(store, db, audit, world):
    """SS-T8 (12 §8, FAIL-012) — "store locked → fail closed, no raw-secret
    fallback".

    The failure is an exception, not a `None` or an empty string: a caller cannot
    accidentally proceed "without the credential", because there is no return
    value that would let them.
    """

    secret_ref = await set_user_secret(store, db, audit, user_id=world.alice.user_id)
    store.lock()

    for call in (
        lambda: store.get(db, secret_ref, SecretRequester.user(world.alice.user_id), audit),
        lambda: store.rotate(db, secret_ref, SecretRequester.user(world.alice.user_id), audit),
        lambda: store.set(
            db,
            owner_scope_type=SecretOwnerScopeType.USER,
            owner_scope_id=str(world.alice.user_id),
            secret_class=SecretClass.OTHER,
            value="TEST-ONLY-while-locked",
            requester=SecretRequester.server(),
            audit=audit,
        ),
    ):
        with pytest.raises(SecretStoreLocked):
            await call()


async def test_a_never_bootstrapped_store_cannot_be_unlocked(db, kek_value):
    """12 §3 — unlocking a store that was never bootstrapped is an explicit
    locked state, not an implicit bootstrap that would create a second DEK."""

    fresh = EncryptedLocalSecretStore()
    with pytest.raises(SecretStoreLocked):
        await fresh.unlock(db, resolve_kek(f"env:{TEST_KEK_ENV_VAR}"))


async def test_bootstrap_is_idempotent_and_never_replaces_an_existing_dek(
    store, db, audit, world, kek_value
):
    """12 §3's crash-recovery discipline: re-running the documented bootstrap step
    must not orphan existing ciphertext by minting a new DEK."""

    requester = SecretRequester.user(world.alice.user_id)
    secret_ref = await set_user_secret(store, db, audit, user_id=world.alice.user_id)

    # A restart: a brand new store object, bootstrapped again with the same KEK.
    restarted = EncryptedLocalSecretStore()
    await restarted.bootstrap(db, resolve_kek(f"env:{TEST_KEK_ENV_VAR}"))

    assert await restarted.get(db, secret_ref, requester, audit) == TEST_SECRET_VALUE


async def test_kek_resolution_fails_closed_on_every_bad_source(monkeypatch):
    """12 §3/§8 — an unavailable or malformed KEK leaves the store locked rather
    than falling back to a derived-from-nothing key."""

    from server.secrets.errors import KEKUnavailable

    monkeypatch.delenv("MISSING_KEK", raising=False)
    for source in (
        "env:MISSING_KEK",
        "env:",
        "file:/etc/kek",
        "HYPERMIND_KEK",
    ):
        with pytest.raises(KEKUnavailable):
            resolve_kek(source)

    monkeypatch.setenv("BAD_KEK", "not-base64-!!!")
    with pytest.raises(KEKUnavailable):
        resolve_kek("env:BAD_KEK")

    # Correctly encoded but the wrong length is refused too.
    import base64

    monkeypatch.setenv("SHORT_KEK", base64.urlsafe_b64encode(b"tooshort").decode())
    with pytest.raises(KEKUnavailable):
        resolve_kek("env:SHORT_KEK")


# ── SS-T10: a tool gets only its declared secret ──────────────────────────


async def test_ss_t10_tool_receives_only_its_declared_secret(store, db, audit, world):
    """SS-T10 (12 §2) — "a tool receives only its declared secret, never another
    scope's"."""

    declared = await set_user_secret(
        store, db, audit, user_id=world.alice.user_id, value="TEST-ONLY-declared"
    )
    undeclared = await set_user_secret(
        store, db, audit, user_id=world.alice.user_id, value="TEST-ONLY-undeclared"
    )
    other_users = await set_user_secret(
        store, db, audit, user_id=world.bob.user_id, value="TEST-ONLY-bobs"
    )

    tool = SecretRequester.tool(
        "weather.lookup",
        declared_secret_refs={declared},
        user_id=world.alice.user_id,
    )

    assert await store.get(db, declared, tool, audit) == "TEST-ONLY-declared"

    with pytest.raises(SecretDenied):
        await store.get(db, undeclared, tool, audit)

    with pytest.raises(SecretDenied):
        await store.get(db, other_users, tool, audit)


async def test_cross_user_secret_resolution_is_denied(store, db, audit, world):
    """The user-scope half of 12 §2: "a user-scoped secret resolves only for that
    user's authorized operations"."""

    alice_ref = await set_user_secret(store, db, audit, user_id=world.alice.user_id)

    with pytest.raises(SecretDenied):
        await store.get(db, alice_ref, SecretRequester.user(world.bob.user_id), audit)


async def test_server_scoped_pilot_credentials_never_resolve_for_a_user(
    store, db, audit, world
):
    """SECRET-003 / 12 §7 — the pilot's server-owned model credentials resolve
    "only for server-owned operations, never exposed to a user, a user's tool, or
    the agent's context"."""

    server_ref = await store.set(
        db,
        owner_scope_type=SecretOwnerScopeType.SERVER,
        owner_scope_id=None,
        secret_class=SecretClass.MODEL_API_KEY,
        value="TEST-ONLY-shared-pilot-api-key",
        requester=SecretRequester.server(),
        audit=audit,
    )

    for requester in (
        SecretRequester.user(world.alice.user_id),
        SecretRequester.agent(),
        SecretRequester.tool("t", declared_secret_refs={server_ref}, user_id=world.alice.user_id),
    ):
        with pytest.raises(SecretDenied):
            await store.get(db, server_ref, requester, audit)

    assert (
        await store.get(db, server_ref, SecretRequester.server(), audit)
        == "TEST-ONLY-shared-pilot-api-key"
    )


async def test_unknown_handle_is_not_found_and_leaks_nothing(store, db, audit, world):
    with pytest.raises(SecretNotFound):
        await store.get(
            db, f"secretstore:{uuid.uuid4().hex}", SecretRequester.user(world.alice.user_id), audit
        )


async def test_handles_carry_no_information_about_what_they_point_at(store, db, audit, world):
    """A leaked handle must tell an attacker nothing about scope, class, or owner."""

    user_ref = await set_user_secret(store, db, audit, user_id=world.alice.user_id)
    master_ref = await store.set(
        db,
        owner_scope_type=SecretOwnerScopeType.SERVER,
        owner_scope_id=None,
        secret_class=SecretClass.MASTER_KEY,
        value="TEST-ONLY-master",
        requester=SecretRequester.server(),
        audit=audit,
    )

    for handle in (user_ref, master_ref):
        assert handle.startswith("secretstore:")
        body = handle.removeprefix("secretstore:")
        assert len(body) == 32
        assert "master" not in handle
        assert str(world.alice.user_id) not in handle
        assert "user" not in body and "server" not in body


async def test_requester_descriptions_carry_no_credential_material():
    """12 §5's audit records name the requester; that name must not be a secret."""

    from server.secrets.requester import fingerprint

    assert SecretRequester.agent().describe() == "agent"
    assert SecretRequester.server().describe() == "server"
    tool = SecretRequester.tool("my.tool", declared_secret_refs={"secretstore:abc"})
    assert tool.describe() == "tool:my.tool"
    assert "secretstore:abc" not in tool.describe()

    # A superuser fingerprint is short and non-reversible.
    marker = fingerprint("TEST-ONLY-superuser-token-of-sufficient-length")
    assert len(marker) == 12
    assert "TEST-ONLY" not in marker


async def test_hash_token_is_not_reversible_and_is_stable():
    """Credential-shaped values are stored as hashes, so a DB leak yields nothing
    usable (the property behind `access_tokens`, `bootstrap_tokens`, and
    `confirmation_tokens`)."""

    value = "TEST-ONLY-opaque-token"
    digest = hash_token(value)
    assert digest == hash_token(value)
    assert value not in digest
    assert len(digest) == 64


async def test_aead_roundtrip_requires_the_matching_aad():
    """The primitive behind the handle binding, asserted directly."""

    from server.secrets.crypto import aead_encrypt, generate_key

    key = generate_key()
    nonce, ciphertext = aead_encrypt(key, b"plaintext", aad=b"handle-a")

    assert aead_decrypt(key, nonce, ciphertext, aad=b"handle-a") == b"plaintext"
    with pytest.raises(SecretIntegrityError):
        aead_decrypt(key, nonce, ciphertext, aad=b"handle-b")


async def test_requester_kinds_are_exactly_the_documented_set():
    """12 §2's vocabulary. A new kind added without a mediation rule in
    `_authorize` would fall through to the final `SecretDenied`, but naming the
    set here makes the omission visible rather than silently fail-closed."""

    assert {k.value for k in RequesterKind} == {
        "agent",
        "tool",
        "user",
        "server",
        "superuser",
    }
