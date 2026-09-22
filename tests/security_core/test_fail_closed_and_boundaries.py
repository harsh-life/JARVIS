"""Fail-closed behaviour (§16 of the scope, INV-15) and the security module
boundaries (16 §8: REPO-T1, REPO-T4, REPO-T6).

The rule under test, from §16 of the security-core scope: **never turn a
dependency failure into "allow because the security service was unavailable."**
"""

from __future__ import annotations

import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from server.capabilities.registry import UnknownCapability, UnknownOperation, lookup
from server.gateway.app import API_V1_PREFIX
from server.graph.authorization import AccessRequest
from shared.schemas.authorization import DenialSurface, Operation, Principal, ResourceType
from shared.schemas.enums import CapabilityScopeType, PermissionDecisionValue
from tests.security_core.helpers import code_only

pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parents[2]


def principal_for(user) -> Principal:
    return Principal(
        user_id=user.user_id, device_id=uuid.uuid4(), session_id=uuid.uuid4()
    )


# ── every dependency failure denies ────────────────────────────────────────


@pytest.mark.parametrize(
    "broken_port",
    ["_memberships", "_resources", "_capabilities", "_risk", "_floor", "_confirmations"],
)
async def test_any_failing_security_dependency_denies(engine, db, audit, world, broken_port):
    """INV-15 / FAIL-CORE-003 — a failure in *any* collaborator denies.

    Parametrized across every port the engine holds, because a fail-closed wrapper
    that only covers the ports someone thought to test is not fail-closed. Each
    substitute raises on use, and the engine must still refuse.
    """

    class Exploding:
        def __getattr__(self, name):
            def _raise(*args, **kwargs):
                raise RuntimeError(f"{name} unavailable")

            return _raise

    setattr(engine, broken_port, Exploding())

    outcome = await engine.authorize(
        db,
        AccessRequest(
            principal=principal_for(world.alice),
            operation=Operation.SHARE,
            resource_type=ResourceType.FILERESOURCE,
            resource_ref=str(world.alice_private_file.file_id),
            graph_id=world.graph_id,
            required_capability="file.write",
            capability_operation="write_file",
            task_id="t",
        ),
        audit=audit,
    )

    assert outcome.decision is PermissionDecisionValue.DENY, broken_port
    assert not outcome.allowed


async def test_a_malformed_resource_reference_denies(engine, db, audit, world):
    """Malformed security metadata → reject, never a wildcard match."""

    for bad_ref in ("", "not-a-uuid", "../../etc/passwd", "*", "None"):
        outcome = await engine.authorize(
            db,
            AccessRequest(
                principal=principal_for(world.alice),
                operation=Operation.READ,
                resource_type=ResourceType.FILERESOURCE,
                resource_ref=bad_ref,
                graph_id=world.graph_id,
            ),
            audit=audit,
        )
        assert not outcome.allowed, bad_ref


async def test_a_missing_resource_reference_on_a_non_create_operation_denies(
    engine, db, audit, world
):
    outcome = await engine.authorize(
        db,
        AccessRequest(
            principal=principal_for(world.alice),
            operation=Operation.READ,
            resource_type=ResourceType.FILERESOURCE,
            resource_ref=None,
            graph_id=world.graph_id,
        ),
        audit=audit,
    )
    assert not outcome.allowed
    assert outcome.reason == "missing_resource_ref"
    assert outcome.surface is DenialSurface.NOT_FOUND


async def test_an_unexpected_capability_or_operation_denies(db, grants, world):
    """"unexpected enum/operation → deny/reject" (§16 of the scope)."""

    from shared.schemas.authorization import CapabilityCheckContext

    context = CapabilityCheckContext(principal=principal_for(world.alice))

    for capability in ("", "nonexistent.capability", "FILE.READ", "file.read ", None):
        assert not await grants.has_capability(
            db, capability=capability, context=context
        ), capability

    await grants.grant(
        db,
        principal_id=world.alice.user_id,
        scope_type=CapabilityScopeType.USER,
        capability="file.read",
        granted_by=world.alice.user_id,
    )
    for operation in ("", "not_an_operation", "READ_FILE"):
        assert not await grants.has_capability(
            db, capability="file.read", context=context, capability_operation=operation
        ), operation


async def test_the_registry_raises_rather_than_defaulting(db):
    with pytest.raises(UnknownCapability):
        lookup("no.such.capability")
    with pytest.raises(UnknownOperation):
        lookup("file.read").risk_for("no_such_operation")


async def test_a_locked_secret_store_denies_the_operation_that_needs_it(api):
    """SS-T8 at the request level (FAIL-012) — a locked store makes the operation
    that needs a credential fail explicitly, never proceed without it.

    Device registration and token refresh both need the store; with it locked they
    return an error rather than registering a device with no credential or issuing
    a token without verifying one.
    """

    bootstrap = await api.oidc_login()
    api.core.secret_store.lock()

    registration = await api.client.post(
        f"{API_V1_PREFIX}/devices",
        json={"platform": "android"},
        headers={"Authorization": f"Bearer {bootstrap}"},
    )
    # 02 §1.7's `dependency_unavailable` — explicit, retryable, and naming the
    # class of backend rather than fabricating success (12 §8).
    assert registration.status_code == 503
    body = registration.json()["error"]
    assert body["code"] == "dependency_unavailable"
    assert body["retryable"] is True
    assert body["details"]["dependency"] == "secret_store"
    assert "device_credential" not in registration.text

    async with api.storage.session() as session:
        from sqlalchemy import select

        from server.storage.models import Device

        assert (await session.execute(select(Device))).scalars().all() == []


async def test_token_refresh_fails_closed_when_the_store_is_locked(api):
    """The credential verifier resolves the stored public key through the
    SecretStore; a locked store must fail authentication rather than skip
    signature verification."""

    onboarded = await api.onboard()
    api.core.secret_store.lock()

    refreshed = await api.issue_token(onboarded.device_id, onboarded.credential)
    assert refreshed.status_code == 401


async def test_an_unexpected_error_rolls_back_the_business_transaction(api, monkeypatch):
    """`server/gateway/deps.py`'s commit policy: an unexpected exception discards
    the partial mutation rather than committing it.

    A security *refusal* commits (so the audit trail survives, 02 §1.2); anything
    else rolls back.
    """

    from sqlalchemy import select

    from server.graph.service import GraphService
    from server.storage.models import Graph

    onboarded = await api.onboard()

    original = GraphService.create_graph

    async def boom(self, session, **kwargs):
        graph = await original(self, session, **kwargs)
        raise RuntimeError("something unexpected after the row was written")

    monkeypatch.setattr(GraphService, "create_graph", boom)

    with pytest.raises(RuntimeError):
        await api.client.post(
            f"{API_V1_PREFIX}/graphs",
            json={"name": "doomed", "type": "shared"},
            headers=onboarded.auth,
        )

    async with api.storage.session() as session:
        assert (await session.execute(select(Graph))).scalars().all() == []


# ── 16 §8: the module boundaries, mechanically ─────────────────────────────


def _lint_imports_cmd() -> list[str]:
    script = Path(sys.executable).parent / "lint-imports"
    assert script.exists(), f"lint-imports console script not found at {script}"
    return [str(script)]


async def test_repo_t1_t4_t6_the_boundary_contracts_all_hold():
    """REPO-T1/T4/T6/T7 (16 §8) — "a boundary violation is a CI failure, not a
    review nicety".

    The named contracts are asserted individually rather than only by the summary
    count, so a contract being silently *removed* from pyproject fails here.
    """

    result = subprocess.run(
        [*_lint_imports_cmd(), "--config", "pyproject.toml"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    for contract in (
        "Track B server layering (16 §2)",
        "Agent never imports raw secrets resolution (16 §3, REPO-T1)",
        "Dashboard cannot import secrets (16 §5, DASH-002, REPO-T6)",
        "shared/schemas never imports server (16 §1/§4, REPO-T3)",
        "Agent cannot import the capability/authz engine (16 §5, INV-8)",
        "Memory/vault never resolve secrets (12 §6, GRAPH-009)",
        "SecretStore never imports the identity layer (12 §4, SUPER-001)",
        "Tools/models cannot reach the grant, confirmation, or decision paths (16 §5)",
        "Model adapters never import the SecretStore (06 §1, 16 §3)",
        "Runtime never depends on Intelligence/Decision providers (INTEL-003, 26)",
    ):
        assert f"{contract} KEPT" in result.stdout, contract

    match = re.search(r"Contracts: (\d+) kept, (\d+) broken\.", result.stdout)
    assert match is not None and int(match.group(2)) == 0, result.stdout


async def test_repo_t7_the_boundary_check_actually_runs_in_ci():
    """REPO-T7 (16 §8) — "the dependency rules are enforced **in CI**, and a
    deliberate violation fails the build".

    The test above proves the contracts hold *when someone runs them*. This one
    proves something runs them, which is the part 16 §6 `[LOCKED]`s: "a boundary
    violation is a CI failure, not a review nicety — because under heavy code
    generation, 'the reviewer will catch it' is not a reliable control."

    Without this assertion, deleting the workflow would leave the whole suite
    green while silently downgrading every boundary contract to a convention.
    """

    import yaml

    workflow_dir = REPO_ROOT / ".github" / "workflows"
    workflows = sorted(workflow_dir.glob("*.yml")) + sorted(workflow_dir.glob("*.yaml"))
    assert workflows, "no CI workflow exists, so no boundary contract is enforced (16 §6)"

    def steps_of(path):
        parsed = yaml.safe_load(path.read_text())
        for job in (parsed.get("jobs") or {}).values():
            yield from (job.get("steps") or [])

    commands = [
        step.get("run", "")
        for path in workflows
        for step in steps_of(path)
    ]
    joined = "\n".join(commands)

    assert "lint-imports" in joined, "CI does not run lint-imports (REPO-T7)"
    assert "pytest" in joined, "CI does not run the test suite"

    # And it must trigger on pull requests, or a violation reaches main unchecked.
    triggers = set()
    for path in workflows:
        parsed = yaml.safe_load(path.read_text())
        # PyYAML parses the bare key `on:` as the boolean True.
        raw = parsed.get("on", parsed.get(True)) or {}
        triggers.update(raw if isinstance(raw, (dict, list)) else [raw])
    assert "pull_request" in triggers, f"CI does not run on pull requests: {triggers}"


async def test_repo_t1_the_agent_package_cannot_reach_secret_resolution():
    """REPO-T1 (SECRET-002, INV-6) — release-blocking.

    Asserted as an absence in the source as well as in the import graph: no module
    under `server/agent` mentions the secrets package at all, so there is nothing
    for a future edit to "just uncomment".
    """

    for path in Path("server/agent").rglob("*.py"):
        code = code_only(path)
        assert "server.secrets" not in code, path
        assert "server/secrets" not in code, path
        assert "SecretStore" not in code, path


async def test_no_module_outside_the_engine_reimplements_the_read_predicate():
    """§9 of the security-core scope — "Do not duplicate visibility logic across
    multiple modules. Create ONE authoritative authorization/visibility
    predicate."

    The predicate's shape is distinctive: it compares `visibility` against `graph`
    and `owner_user_id` against a user. Any *other* module doing both is a second
    implementation, which is how the two drift apart and one becomes wrong.
    """

    # The predicate's home, and the engine that applies it. Memory hydration
    # imports the same function from `server/graph/predicate.py` rather than
    # restating it, which is what this test exists to keep true.
    engine_modules = {Path("server/graph/authorization.py"), Path("server/graph/predicate.py")}
    offenders: list[str] = []
    for path in sorted(Path("server").rglob("*.py")):
        if path in engine_modules:
            continue
        code = code_only(path)
        mentions_graph_visibility = "Visibility.GRAPH" in code
        mentions_owner_comparison = re.search(r"owner_user_id\s*==", code) is not None
        if mentions_graph_visibility and mentions_owner_comparison:
            offenders.append(str(path))

    assert offenders == [], offenders


async def test_the_engine_is_the_only_producer_of_a_permission_decision():
    """04 §1 — "No resource operation happens without passing through this engine
    — there is no side door."

    `PermissionDecision` rows are written in exactly one place, so a module that
    decided access on its own would have no way to record it and would be visible
    as an unaudited path.
    """

    writers = [
        str(path)
        for path in sorted(Path("server").rglob("*.py"))
        if "record_permission_decision" in path.read_text()
    ]
    assert writers == ["server/graph/authorization.py", "server/security/audit.py"], writers


async def test_there_is_no_global_security_manager():
    """§18 of the security-core scope — "Do not use a giant global
    'SecurityManager' containing everything."

    The composition root assembles the pieces; it does not become a class that owns
    them all, which is how a boundary quietly disappears.
    """

    for path in sorted(Path("server").rglob("*.py")):
        code = code_only(path)
        for forbidden in ("SecurityManager", "SecurityService", "AuthManager"):
            assert forbidden not in code, f"{path}: {forbidden}"


async def test_no_decision_or_intelligence_provider_implementation_exists():
    """§19 of the security-core scope, and 26's own status: `26_DECISION_PROVIDER.md`
    is `[FUTURE][PROPOSED]` and "Consumed by: **no one, currently**".

    Nothing in this branch may have started building it, and `intelligence`
    likewise stays an empty stub (INV-18/INTEL-003).
    """

    for path in sorted(Path("server").rglob("*.py")):
        code = code_only(path)
        assert "DecisionProvider" not in code, path
        assert "IntelligenceProvider" not in code, path

    # `intelligence` remains a stub: only its __init__.py exists. (`agent` is now
    # the runtime branch's `05` implementation; it is held to INTEL-003 by the
    # "Runtime never depends on Intelligence/Decision providers" contract.)
    assert [p.name for p in Path("server/intelligence").glob("*.py")] == ["__init__.py"]


async def test_no_track_a_dependency_exists():
    """§3 of the security-core scope — Track A and its internals are out of scope."""

    for path in sorted(Path("server").rglob("*.py")):
        code = code_only(path)
        for forbidden in ("track_a", "trackA", "TrackA"):
            assert forbidden not in code, f"{path}: {forbidden}"


async def test_no_filesystem_network_or_device_execution_bypass_was_added():
    """§19 of the security-core scope, carried forward by the runtime branch — no
    branch so far implements a filesystem sandbox, network egress, or Android
    execution path, so none may have opened a bypass of boundaries that do not
    exist yet.

    Asserted as an absence: `fs`, `net`, `voice`, `scheduler` and `vault` remain
    stubs, and no server module reaches for a raw socket or subprocess. (`tools`,
    `modeltools` and `memory` now hold the runtime's registry, model-tools, and
    hydration boundary; `ToolRegistry.register` refuses any tool that declares a
    filesystem or network need, until `09`/`10` exist to enforce it.)
    """

    for package in ("fs", "net", "voice", "scheduler", "vault"):
        assert [p.name for p in Path(f"server/{package}").glob("*.py")] == [
            "__init__.py"
        ], package

    for path in sorted(Path("server").rglob("*.py")):
        code = code_only(path)
        for forbidden in ("socket", "subprocess", "os.system", "shutil"):
            assert forbidden not in code, f"{path}: {forbidden}"
