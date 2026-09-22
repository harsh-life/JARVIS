"""BR-T2 — the OD-A1 blast-radius measurement (14 §4, 17 §4).

14 §4 specifies this experiment and, unusually, specifies that it is **not** a
pass/fail test:

> **Setup:** a test harness that executes attacker-controlled code inside the
> application process (simulated app-RCE), acting as user A's compromised
> process.
> **Attempt:** read user B's memory, files, and (in-memory/at-rest) secrets.
> **Deliverable:** the **measured blast radius** — exactly what was and wasn't
> reachable — not a pass/fail.

So this module measures, records, and asserts the measurement *matches what the
package claims* — in both directions. It asserts the containments that are real,
and it also asserts that the **uncontained** cases really are uncontained, so the
documented residual cannot quietly become a false "isolated" claim (INV-20, the
honesty invariant).

If a future branch genuinely closes one of the uncontained rows, the
corresponding assertion here will fail. That failure is the intended signal: it
means BR-T2 must be re-run and `docs/OD_A1_BR_T2.md` updated, not that the
assertion should be deleted.

The attacker model is precise: code running **inside** the FastAPI process, with
the ordinary Python access that implies — the live `AsyncSession`, the
constructed `SecurityCore`, and module globals. It is not a remote client, and it
is not root on the host.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import select

from server.secrets.crypto import aead_decrypt
from server.secrets.errors import SecretDenied, SecretStoreLocked
from server.secrets.kek import resolve_kek
from server.secrets.requester import SecretRequester, SuperuserGrant
from server.secrets.store import EncryptedLocalSecretStore
from server.graph.authorization import AccessRequest
from server.storage.models import CapabilityGrant, Device, FileResource, SecretMaterial, SecretStoreKey
from shared.schemas.authorization import (
    CapabilityCheckContext,
    Operation,
    Principal,
    ResourceType,
)
from shared.schemas.enums import CapabilityScopeType, SecretClass, SecretOwnerScopeType
from tests.support import TEST_KEK_ENV_VAR

pytestmark = pytest.mark.asyncio

BOB_SECRET = "TEST-ONLY-bobs-private-credential-value"
REPORT = Path("docs/OD_A1_BR_T2.md")


@dataclass
class Finding:
    """One measured attempt."""

    attempt: str
    reachable: bool
    detail: str

    def row(self) -> str:
        mark = "REACHABLE" if self.reachable else "contained"
        return f"  [{mark:>9}] {self.attempt} — {self.detail}"


async def _seed(db, store, audit, world) -> str:
    """Give Bob a private secret for the attacker (acting as Alice) to go after."""

    return await store.set(
        db,
        owner_scope_type=SecretOwnerScopeType.USER,
        owner_scope_id=str(world.bob.user_id),
        secret_class=SecretClass.OAUTH_TOKEN,
        value=BOB_SECRET,
        requester=SecretRequester.server(),
        audit=audit,
    )


async def test_br_t2_measured_blast_radius_under_simulated_app_rce(
    db, store, audit, world, grants, engine, kek_value, capsys
):
    """The experiment. Emits the measured table and asserts it in both directions."""

    bob_secret_ref = await _seed(db, store, audit, world)
    await db.flush()

    findings: list[Finding] = []

    # ── 1. Application-level authorization checks ───────────────────────────
    #
    # The engine's answers are correct for a caller that *uses* the engine. An
    # attacker who is the application process is not obliged to.

    outcome = await engine.authorize(
        db,
        AccessRequest(
            principal=Principal(
                user_id=world.alice.user_id,
                device_id=uuid.uuid4(),
                session_id=uuid.uuid4(),
                active_graph_id=world.graph_id,
            ),
            operation=Operation.READ,
            resource_type=ResourceType.FILERESOURCE,
            resource_ref=str(world.bob_private_file.file_id),
            graph_id=world.graph_id,
        ),
        audit=audit,
    )
    findings.append(
        Finding(
            "read B's private file *through the authorization engine*",
            reachable=outcome.allowed,
            detail="engine denies with 404 (AZ-T1) — the logical boundary holds for callers that use it",
        )
    )

    # ── 2. Bypassing the engine entirely ───────────────────────────────────

    bobs_rows = (
        await db.execute(
            select(FileResource).where(FileResource.owner_user_id == world.bob.user_id)
        )
    ).scalars().all()
    findings.append(
        Finding(
            "read B's private rows by querying the shared store directly",
            reachable=bool(bobs_rows),
            detail=(
                "the visibility filter is application code; an attacker who *is* the "
                "application does not have to call it (14 §4's core admission)"
            ),
        )
    )

    # ── 3. Secret resolution with a forged requester ────────────────────────
    #
    # The attacker can construct any `SecretRequester` they like, because
    # constructing one is ordinary Python.

    forged = SecretRequester.user(world.bob.user_id)
    stolen = await store.get(db, bob_secret_ref, forged, audit)
    findings.append(
        Finding(
            "resolve B's secret by constructing a requester claiming to be B",
            reachable=stolen == BOB_SECRET,
            detail=(
                "the requester is a value the caller supplies; mediation binds the "
                "*claim*, and in-process code chooses its own claims"
            ),
        )
    )

    # ── 4. What still refuses, even in-process ──────────────────────────────

    agent_denied = False
    try:
        await store.get(db, bob_secret_ref, SecretRequester.agent(), audit)
    except SecretDenied:
        agent_denied = True
    findings.append(
        Finding(
            "resolve any secret through the agent requester",
            reachable=not agent_denied,
            detail="unconditional denial (SECRET-002/INV-6) — no scope or class overrides it",
        )
    )

    master_ref = await store.set(
        db,
        owner_scope_type=SecretOwnerScopeType.SERVER,
        owner_scope_id=None,
        secret_class=SecretClass.MASTER_KEY,
        value="TEST-ONLY-master-key",
        requester=SecretRequester.server(),
        audit=audit,
    )
    master_denied = False
    try:
        await store.get(db, master_ref, SecretRequester.server(), audit)
    except SecretDenied:
        master_denied = True
    findings.append(
        Finding(
            "resolve a class=master_key reference without superuser authority",
            reachable=not master_denied,
            detail="denied for every non-superuser requester (SS-T4, SUPER-001)",
        )
    )

    # ── 5. In-memory key material ──────────────────────────────────────────

    dek = getattr(store, "_dek", None)
    material = await db.get(SecretMaterial, bob_secret_ref)
    decrypted_from_memory = None
    if dek is not None and material is not None:
        decrypted_from_memory = aead_decrypt(
            dek, material.nonce, material.ciphertext, aad=bob_secret_ref.encode()
        ).decode()
    findings.append(
        Finding(
            "extract the unlocked DEK from the live store object and decrypt any secret",
            reachable=decrypted_from_memory == BOB_SECRET,
            detail=(
                "12 §4's stated residual, verbatim: 'a compromised *running* process "
                "reading in-memory unlocked secrets is a real limit'"
            ),
        )
    )

    # ── 6. The at-rest boundary (this is what holds) ───────────────────────

    thief = EncryptedLocalSecretStore()
    at_rest_locked = False
    try:
        await thief.get(db, bob_secret_ref, SecretRequester.user(world.bob.user_id), audit)
    except SecretStoreLocked:
        at_rest_locked = True
    findings.append(
        Finding(
            "decrypt the store from database contents alone, without process memory",
            reachable=not at_rest_locked,
            detail="the KEK is external; a stolen DB or backup yields no plaintext (SS-T3, BR-T3)",
        )
    )

    kek_bytes = resolve_kek(f"env:{TEST_KEK_ENV_VAR}")
    wrapped_rows = (await db.execute(select(SecretStoreKey))).scalars().all()
    kek_in_db = any(kek_bytes in row.wrapped_dek for row in wrapped_rows)
    findings.append(
        Finding(
            "recover the KEK from the database",
            reachable=kek_in_db,
            detail="only the wrapped DEK is stored; the KEK is never written (12 §3)",
        )
    )

    # ── 7. Device impersonation ────────────────────────────────────────────
    #
    # The asymmetric credential choice (03 §4.2's [REC]) pays off precisely here:
    # even total server compromise yields no material that can impersonate a
    # device, because the server never held it.

    # A real device has to exist for this row to measure anything, so one is
    # registered here rather than asserted about an empty table.
    from server.auth.bootstrap import issue_bootstrap_token
    from server.auth.device import DeviceService, build_device_proof
    from shared.schemas.enums import DevicePlatform

    devices = DeviceService(secret_store=store)
    bootstrap = await issue_bootstrap_token(db, user_id=world.bob.user_id)
    registered = await devices.register(
        db, bootstrap_token=bootstrap, platform=DevicePlatform.ANDROID, audit=audit
    )
    await db.flush()

    device_rows = (await db.execute(select(Device))).scalars().all()
    assert device_rows, "the measurement needs at least one registered device"

    # Everything the server holds for that device, resolved with full in-process
    # authority. If any of it could sign a proof, impersonation is reachable.
    forgeable_with_server_material = False
    examined = 0
    for row in device_rows:
        # Resolved with a forged owner requester — i.e. granting the attacker the
        # access finding 3 already showed is reachable. This row asks a narrower
        # question: given *everything* the server holds, can a device be
        # impersonated?
        stored_verifier = await store.get(
            db, row.credential_ref, SecretRequester.user(row.user_id), audit
        )
        examined += 1
        try:
            build_device_proof(
                device_id=row.device_id, device_credential=stored_verifier
            )
            # A proof was produced from server-held material — but it only matters
            # if it actually verifies, so check that rather than assuming.
            forged = build_device_proof(
                device_id=row.device_id, device_credential=stored_verifier
            )
            await devices.verify_proof(db, forged, audit=audit)
            forgeable_with_server_material = True
        except Exception:  # noqa: BLE001 - measurement, not enforcement
            pass

    findings.append(
        Finding(
            "forge a device-credential proof using server-side material",
            reachable=forgeable_with_server_material,
            detail=(
                f"the server stores only public verifiers ({examined} examined); a proof "
                "signed with one does not verify, because the private half never existed "
                "server-side (03 §4.2 [REC])"
            ),
        )
    )

    # The counterpart, to keep the row honest: the credential the *device* holds
    # does verify. So the containment is about where the key lives, not about the
    # proof mechanism being unusable.
    genuine = build_device_proof(
        device_id=registered.device.device_id,
        device_credential=registered.device_credential,
    )
    verified = await devices.verify_proof(db, genuine, audit=audit)
    assert verified.device_id == registered.device.device_id

    # ── 8. Self-escalation ─────────────────────────────────────────────────

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
    floor_active = await grants.has_capability(
        db,
        capability="capability.self_grant",
        context=CapabilityCheckContext(
            principal=Principal(
                user_id=world.alice.user_id, device_id=uuid.uuid4(), session_id=uuid.uuid4()
            )
        ),
    )
    findings.append(
        Finding(
            "self-grant an absolute-floor capability by writing the grant row directly",
            reachable=floor_active,
            detail=(
                "the row can be written, but authorizes nothing: no registry entry and no "
                "tool exposes a floor operation (PERM-006, prohibition by absence)"
            ),
        )
    )

    superuser_forgeable = SuperuserGrant._issue("in-process").is_valid()
    findings.append(
        Finding(
            "mint superuser authority from inside the process",
            reachable=superuser_forgeable,
            detail=(
                "SuperuserGrant is a process-local object; in-process code can construct "
                "one, as server/secrets/requester.py states outright"
            ),
        )
    )

    # ── the measured table ─────────────────────────────────────────────────

    print("\nBR-T2 measured blast radius (simulated app-level RCE):")
    for finding in findings:
        print(finding.row())

    measured = {f.attempt: f.reachable for f in findings}

    # Containments the package *does* claim. A regression here is a real defect.
    assert measured["read B's private file *through the authorization engine*"] is False
    assert measured["resolve any secret through the agent requester"] is False
    assert (
        measured["resolve a class=master_key reference without superuser authority"] is False
    )
    assert (
        measured["decrypt the store from database contents alone, without process memory"]
        is False
    )
    assert measured["recover the KEK from the database"] is False
    assert measured["forge a device-credential proof using server-side material"] is False
    assert (
        measured[
            "self-grant an absolute-floor capability by writing the grant row directly"
        ]
        is False
    )

    # Residuals the package explicitly does **not** claim to have closed
    # (INV-20). These assertions exist so the documented limit cannot silently
    # become a false "isolated" claim — and so that genuinely closing one forces
    # a re-run of BR-T2 and an update to docs/OD_A1_BR_T2.md.
    assert measured["read B's private rows by querying the shared store directly"] is True
    assert (
        measured["resolve B's secret by constructing a requester claiming to be B"] is True
    )
    assert (
        measured[
            "extract the unlocked DEK from the live store object and decrypt any secret"
        ]
        is True
    )
    assert measured["mint superuser authority from inside the process"] is True


async def test_inv_20_the_package_does_not_claim_isolation_it_has_not_earned():
    """INV-20 (14 §2) — "cross-user isolation under single-laptop RCE **not**
    claimed proven".

    The honesty invariant is a property of what the repository *says*, so it is
    tested that way: the BR-T2 report must exist, must record the measurement, and
    must not contain a claim of full isolation.
    """

    assert REPORT.exists(), (
        "BR-T2's deliverable is the measured blast radius, recorded at "
        f"{REPORT} (14 §4, 17 §4)"
    )
    text = REPORT.read_text()

    # It must be honest about status.
    assert "OD-A1" in text
    assert "[OPEN — OWNER]" in text
    for option in ("(a)", "(b)", "(c)"):
        assert option in text, f"the owner's decision options must be stated: {option}"

    # And it must not claim what §4 says is not claimed.
    lowered = text.lower()
    for false_claim in (
        "fully isolated",
        "cross-user isolation is proven",
        "rce-proof",
        "no residual",
    ):
        assert false_claim not in lowered, false_claim


async def test_the_residual_table_is_present_with_owner_actions():
    """BR-T4 (14 §7) — "residual table present, each has owner action"."""

    text = REPORT.read_text()
    assert "Residual" in text or "residual" in text
    assert "Owner action" in text or "owner action" in text
    # The four residuals 14 §5 enumerates that are in this branch's scope.
    assert "in-memory" in text.lower()
    assert "stolen device credential" in text.lower()


async def test_real_user_data_remains_gated():
    """PILOT-004 / 14 §4's `[LOCKED]` gate — "no real, non-disposable user data is
    entrusted to the pilot until OD-A1 is reviewed".

    The gate has to be written down to be a gate.
    """

    text = REPORT.read_text().lower()
    assert "disposable" in text
    assert "real user data" in text or "real, non-disposable" in text
