"""Superuser HTTP authentication — build unit U2.

`Authorization: Superuser <token>`, verified by the existing out-of-band
mechanism (`server.security.superuser`), exposed as the reusable dependency
`server.gateway.superuser_auth.get_superuser`. No production route uses it yet;
these tests mount a probe route on the real application for the purpose.

| Requirement | Test |
|---|---|
| valid credential accepted | `test_the_configured_credential_authenticates` |
| invalid credential rejected | `test_a_wrong_credential_is_rejected` |
| malformed scheme rejected | `test_malformed_superuser_headers_are_rejected` |
| missing credential rejected | `test_a_missing_credential_is_rejected` |
| Bearer never authenticates as superuser | `test_a_user_bearer_token_never_authenticates_as_superuser` |
| superuser credential never authenticates as a user | `test_the_superuser_credential_never_authenticates_as_a_user` |
| constant-time comparison | `test_the_credential_comparison_is_constant_time` |
| no user/session/capability path mints it | `test_no_user_session_or_capability_path_mints_superuser_authority`, `test_only_the_superuser_module_mints_a_grant` |
| import boundaries | `test_the_superuser_import_contract_holds`, `test_the_superuser_import_contract_catches_a_violation` |
| ordinary auth unchanged | `test_ordinary_user_authentication_is_unchanged` |
"""

from __future__ import annotations

import ast
import dataclasses
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends

from server.gateway.superuser_auth import SUPERUSER_SCHEME, SuperuserPrincipal, get_superuser
from server.secrets.requester import SuperuserGrant, fingerprint
from server.security import superuser as superuser_module
from server.security.events import AuditAction
from server.security.superuser import SUPERUSER_TOKEN_ENV, authenticate_superuser
from server.storage.models import AuditEvent, CapabilityGrant
from shared.schemas.authorization import Principal
from shared.schemas.enums import AuditActor, AuditResult
from tests.runtime.conftest import make_harness  # noqa: F401 — the real-app harness

pytestmark = pytest.mark.asyncio

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "TEST-ONLY-superuser-credential-0123456789abcdef"
PROBE = "/api/v1/__test__/superuser"
CAPABILITIES = "/api/v1/capabilities"


@pytest_asyncio.fixture
async def su(make_harness, monkeypatch):
    """The real application, a configured superuser credential, and a probe
    route that depends on `get_superuser` — the shape later operator routes
    will take."""

    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, TOKEN)
    h = await make_harness()

    @h.app.get(PROBE)
    async def probe(principal: SuperuserPrincipal = Depends(get_superuser)) -> dict:
        return {"fingerprint": principal.token_fingerprint, "valid": principal.grant.is_valid()}

    return h


async def _probe(h, headers: dict | list | None = None) -> httpx.Response:
    return await h.client.get(PROBE, headers=headers)


def _unauthenticated(resp: httpx.Response) -> None:
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "unauthenticated"
    assert resp.json()["error"]["message"] == "superuser authentication required"


async def _superuser_audit(h) -> list[AuditEvent]:
    return [r for r in await h.rows(AuditEvent)
            if r.action in (AuditAction.SUPERUSER_AUTHENTICATED.value, AuditAction.SUPERUSER_REJECTED.value)]


async def _assert_token_never_recorded(h) -> None:
    for row in await h.rows(AuditEvent):
        assert TOKEN not in row.resource


# ── acceptance and refusal ─────────────────────────────────────────────────


async def test_the_configured_credential_authenticates(su):
    resp = await _probe(su, {"Authorization": f"Superuser {TOKEN}"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"fingerprint": fingerprint(TOKEN), "valid": True}
    [row] = await _superuser_audit(su)
    assert row.action == AuditAction.SUPERUSER_AUTHENTICATED.value
    assert row.actor == AuditActor.SUPERUSER.value
    assert row.result == AuditResult.SUCCESS.value
    assert row.resource == f"superuser:{fingerprint(TOKEN)}"
    assert row.user_id is None and row.session_id is None and row.device_id is None
    await _assert_token_never_recorded(su)


@pytest.mark.parametrize("wrong", [
    TOKEN[:-1] + ("x" if TOKEN[-1] != "x" else "y"),  # same length, last char differs
    TOKEN[:-1],                                       # a prefix
    TOKEN + "0",                                      # an extension
    "TEST-ONLY-an-entirely-different-credential-of-length",
])
async def test_a_wrong_credential_is_rejected(su, wrong):
    _unauthenticated(await _probe(su, {"Authorization": f"Superuser {wrong}"}))

    [row] = await _superuser_audit(su)
    assert (row.action, row.actor, row.result) == (
        AuditAction.SUPERUSER_REJECTED.value, AuditActor.SYSTEM.value, AuditResult.BLOCKED.value)
    assert row.resource == "superuser:rejected"
    await _assert_token_never_recorded(su)


@pytest.mark.parametrize("header", [
    f"superuser {TOKEN}",          # scheme is matched exactly
    f"SUPERUSER {TOKEN}",
    f"Superuser  {TOKEN}",         # two spaces: the token would start with whitespace
    f"Superuser {TOKEN} extra",    # whitespace inside the credential
    f"Superuser\t{TOKEN}",
    f"Superuser{TOKEN}",
    "Superuser",
    "Superuser ",
    f"Basic {TOKEN}",
    f"Token {TOKEN}",
    f" Superuser {TOKEN}",
    f"Superuser {'a' * 2000}",     # oversized
])
async def test_malformed_superuser_headers_are_rejected(su, header):
    _unauthenticated(await _probe(su, {"Authorization": header}))
    assert [r.resource for r in await _superuser_audit(su)] == ["superuser:rejected"]


async def test_two_authorization_headers_are_rejected_even_if_one_is_valid(su):
    resp = await su.client.get(PROBE, headers=[
        ("Authorization", f"Superuser {TOKEN}"),
        ("Authorization", f"Superuser {TOKEN}"),
    ])
    _unauthenticated(resp)


async def test_a_missing_credential_is_rejected(su):
    _unauthenticated(await _probe(su))
    assert [r.resource for r in await _superuser_audit(su)] == ["superuser:rejected"]


async def test_an_unconfigured_or_weak_deployment_has_no_superuser(make_harness, monkeypatch):
    """No credential configured → no superuser exists, and the response is the
    same as for a wrong credential (it reveals nothing about configuration)."""

    monkeypatch.delenv(SUPERUSER_TOKEN_ENV, raising=False)
    h = await make_harness()

    @h.app.get(PROBE)
    async def probe(principal: SuperuserPrincipal = Depends(get_superuser)) -> dict:
        return {}

    unconfigured = await _probe(h, {"Authorization": f"Superuser {TOKEN}"})
    _unauthenticated(unconfigured)
    assert [r.resource for r in await _superuser_audit(h)] == ["superuser:not_configured"]

    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, "short")  # below the 32-character minimum
    weak = await _probe(h, {"Authorization": "Superuser short"})
    _unauthenticated(weak)
    assert weak.json()["error"]["message"] == unconfigured.json()["error"]["message"]


# ── the two schemes never cross ────────────────────────────────────────────


async def test_a_user_bearer_token_never_authenticates_as_superuser(su, monkeypatch):
    alice = await su.user("alice")

    # The superuser path must not even look a Bearer token up.
    async def must_not_be_called(*args, **kwargs):
        raise AssertionError("the superuser dependency consulted the user session store")

    monkeypatch.setattr(su.core.sessions, "resolve_principal", must_not_be_called)

    _unauthenticated(await _probe(su, {"Authorization": f"Bearer {alice.token}"}))
    # A user's valid access token in the Superuser scheme is just a wrong credential.
    _unauthenticated(await _probe(su, {"Authorization": f"Superuser {alice.token}"}))
    # And the superuser credential itself, sent as Bearer, is refused here too.
    _unauthenticated(await _probe(su, {"Authorization": f"Bearer {TOKEN}"}))


async def test_the_superuser_credential_never_authenticates_as_a_user(su):
    for header in (f"Superuser {TOKEN}", f"Bearer {TOKEN}"):
        resp = await su.client.get(CAPABILITIES, headers={"Authorization": header})
        assert resp.status_code == 401, (header, resp.text)
    resp = await su.client.post("/api/v1/agent/tasks", json={"input": "x"},
                                headers={"Authorization": f"Superuser {TOKEN}", "Idempotency-Key": "k"})
    assert resp.status_code == 401
    # Nothing about those requests authenticated a superuser either.
    assert [r for r in await _superuser_audit(su)
            if r.action == AuditAction.SUPERUSER_AUTHENTICATED.value] == []


async def test_ordinary_user_authentication_is_unchanged(su):
    alice = await su.user("alice")
    ok = await su.client.get(CAPABILITIES, headers=alice.auth)
    assert ok.status_code == 200, ok.text
    for header in (None, "Bearer ", "Bearer not-a-token", f"bearer {alice.token}"):
        resp = await su.client.get(CAPABILITIES, headers={"Authorization": header} if header else None)
        assert resp.status_code == 401, (header, resp.text)
    assert await _superuser_audit(su) == []


# ── constant time ──────────────────────────────────────────────────────────


async def test_the_credential_comparison_is_constant_time(su, monkeypatch):
    """Every attempt is exactly one `hmac.compare_digest` over two 32-byte
    digests, whatever was presented: a length mismatch no longer returns early,
    and an empty credential takes the same path."""

    calls: list[tuple[int, int]] = []
    real = superuser_module.hmac.compare_digest

    def spy(a, b):
        calls.append((len(a), len(b)))
        return real(a, b)

    monkeypatch.setattr(superuser_module.hmac, "compare_digest", spy)

    for presented in ("", "x", TOKEN[:-1], TOKEN, TOKEN + "0", "y" * 1000):
        calls.clear()
        try:
            authenticate_superuser(presented)
        except Exception:
            pass
        assert calls == [(32, 32)], presented

    calls.clear()
    await _probe(su, {"Authorization": f"Superuser {TOKEN[:5]}"})
    await _probe(su, {"Authorization": f"Superuser {TOKEN}"})
    assert calls == [(32, 32), (32, 32)]


# ── no other origin ────────────────────────────────────────────────────────


async def test_no_user_session_or_capability_path_mints_superuser_authority(su):
    alice = await su.user("alice")

    # A user principal carries no superuser anything, and the two types share nothing.
    user_fields = {f.name for f in dataclasses.fields(Principal)} if dataclasses.is_dataclass(Principal) \
        else set(Principal.model_fields)
    assert not {f for f in user_fields if "superuser" in f.lower() or "grant" in f.lower()}
    assert not issubclass(SuperuserPrincipal, Principal)
    assert not {"user_id", "session_id", "device_id"} & {f.name for f in dataclasses.fields(SuperuserPrincipal)}

    # A capability named for superuser authority is refused at the floor.
    for capability in ("superuser", "superuser.assume", "superuser.anything"):
        resp = await su.client.post(CAPABILITIES, headers=alice.auth, json={
            "capability": capability, "scope_type": "user", "scope_id": str(alice.user_id)})
        assert resp.status_code in (403, 422), (capability, resp.text)
    assert [g for g in await su.rows(CapabilityGrant) if "superuser" in g.capability] == []

    # And a hand-built grant is not a verified one.
    assert SuperuserGrant(token_fingerprint="forged").is_valid() is False


async def test_only_the_superuser_module_mints_a_grant():
    """Source-level: `SuperuserGrant._issue` is called only by
    `server.security.superuser`, and `authenticate_superuser` only by the HTTP
    dependency. A new caller anywhere else fails here and needs review."""

    def called(node: ast.AST) -> str | None:
        if isinstance(node, ast.Call):
            func = node.func
            return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        return None

    issue_callers, auth_callers = set(), set()
    for path in (REPO_ROOT / "server").rglob("*.py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(ast.parse(path.read_text())):  # real calls, not docstrings
            name = called(node)
            if name == "_issue":
                issue_callers.add(rel)
            elif name == "authenticate_superuser":
                auth_callers.add(rel)
    assert issue_callers == {"server/security/superuser.py"}
    assert auth_callers == {"server/gateway/superuser_auth.py"}


# ── import boundaries ──────────────────────────────────────────────────────

CONTRACT = "Only the gateway reaches superuser authority (12 §4, SUPER-001)"


def _lint_imports() -> str:
    script = Path(sys.executable).parent / "lint-imports"
    assert script.exists(), f"lint-imports console script not found at {script}"
    return str(script)


async def test_the_superuser_import_contract_holds():
    result = subprocess.run([_lint_imports(), "--config", "pyproject.toml"],
                            cwd=REPO_ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{CONTRACT} KEPT" in result.stdout


@pytest.mark.parametrize("module, line", [
    ("server/agent/state.py", "import server.security.superuser\n"),
    ("server/tools/registry.py", "from server.gateway.superuser_auth import get_superuser\n"),
    ("server/auth/sessions.py", "from server.security.superuser import authenticate_superuser\n"),
])
async def test_the_superuser_import_contract_catches_a_violation(tmp_path, module, line):
    """The contract is live, not decorative: plant a violation in a scratch copy
    of the tree and import-linter must report it broken."""

    copy = tmp_path / "repo"
    shutil.copytree(REPO_ROOT / "server", copy / "server", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO_ROOT / "shared", copy / "shared", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(REPO_ROOT / "pyproject.toml", copy / "pyproject.toml")
    target = copy / module
    target.write_text(line + target.read_text())

    result = subprocess.run([_lint_imports(), "--config", "pyproject.toml", "--no-cache"],
                            cwd=copy, capture_output=True, text=True)
    assert result.returncode != 0, result.stdout
    assert f"{CONTRACT} BROKEN" in result.stdout
