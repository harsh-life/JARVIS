"""Shared test support: configs, a local OIDC provider, and KEK material.

17 §6 `[LOCKED]`: "No fixture contains a real secret; test secrets are
clearly-marked test values." Every credential minted here is generated per-test
or carries an obvious test marker, and none is ever committed as a literal that
could be mistaken for a production value.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from server.config.schema import AppConfig
from server.execution import confinement
from server.execution.break_glass import BreakGlassClaim
from server.execution.process import ConstrainedProcessExecutor, posix_process_controls_missing
from server.fs.paths import descriptor_containment_missing
from server.net.listen import unix_listener_missing
from server.secrets.kek import generate_kek_value

if TYPE_CHECKING:
    from server.gateway.superuser_auth import SuperuserPrincipal

# ── system.restricted on hosts with and without Landlock (OD-BG-2) ─────────
#
# There is no global unconfined mode any more (20 §2.5). Where the kernel can
# confine a process — Linux, including CI — `system.restricted` runs confined,
# and every process test runs that way. Where it cannot (macOS, old kernels),
# normal `system.restricted` is unsupported: it fails closed, and the tests that
# need a child to run are skipped with `NO_LANDLOCK_REASON` rather than quietly
# run without confinement. Their fail-closed behaviour is asserted by
# tests that run everywhere.
#
# The one exception is executor *mechanics* — timeouts, output caps, env
# sanitization, process-group kill — which break-glass must keep (20 §2.1,
# BG-T8). Those also run on the break-glass path, through an explicit
# `DisposableHostBreakGlass` record store: a test double for a disposable,
# data-free test environment, never a production object and never a switch.
HAS_LANDLOCK = confinement.available()
NO_LANDLOCK_REASON = (
    "normal system.restricted needs Landlock (OD-EXEC-1) and fails closed on this host; "
    "its refusal is asserted by the fail-closed tests (OD-BG-2: no global unconfined mode)"
)
requires_landlock = pytest.mark.skipif(not HAS_LANDLOCK, reason=NO_LANDLOCK_REASON)

# ── hosts without the POSIX facilities the execution layer is built on ──────
#
# Linux is the server host; Windows is a development/test/client environment
# only. On a host missing one of these facilities, the production code refuses
# the operation (PLATFORM_UNSUPPORTED / UnixSocketsUnavailable / ContainerError)
# instead of running it with weaker guarantees, and that refusal is asserted by
# tests/execution/test_platform_support.py, which runs everywhere. The tests
# below exercise the facility itself, so on such a host they are skipped with
# exactly what is missing.
POSIX_PROCESS_MISSING = posix_process_controls_missing()
requires_posix_process_controls = pytest.mark.skipif(
    POSIX_PROCESS_MISSING is not None,
    reason=f"needs POSIX process controls ({POSIX_PROCESS_MISSING}); process execution refuses with "
           "platform_unsupported on this host (asserted in test_platform_support.py)",
)
UNIX_LISTENER_MISSING = unix_listener_missing()
requires_unix_listener = pytest.mark.skipif(
    UNIX_LISTENER_MISSING is not None,
    reason=f"needs Unix-domain sockets ({UNIX_LISTENER_MISSING}); the egress proxy and unix: listeners "
           "refuse to start on this host (asserted in test_platform_support.py)",
)


# ── subprocesses: the same bytes on every host ──────────────────────────────


def utf8_child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """`base` (default: this process's environment) with the child's Python
    stdio set to UTF-8, for a parent that decodes its output with
    `encoding="utf-8"`. Without it, a Windows child writes cp1252 and the
    parent's default decoder (also cp1252) fails on a byte cp1252 leaves
    undefined: `subprocess.run(text=True)` then returns `stdout=None`.
    Only stdio is set (PYTHONIOENCODING), never PYTHONUTF8, so the file-I/O
    defaults the encoding guard checks are not masked."""

    env = dict(os.environ if base is None else base)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def lint_imports_script() -> Path:
    """The `lint-imports` console script installed next to this interpreter —
    not whichever one PATH finds — with the platform's executable suffix
    (`lint-imports.exe` on Windows; shutil.which applies PATHEXT)."""

    scripts = Path(sys.executable).parent
    found = shutil.which("lint-imports", path=str(scripts))
    assert found is not None, f"lint-imports console script not found in {scripts}"
    return Path(found)


DESCRIPTOR_CONTAINMENT_MISSING = descriptor_containment_missing()
requires_descriptor_containment = pytest.mark.skipif(
    DESCRIPTOR_CONTAINMENT_MISSING is not None,
    reason=f"needs descriptor-relative containment ({DESCRIPTOR_CONTAINMENT_MISSING}); the filesystem "
           "sandbox refuses every operation on this host (asserted in test_platform_support.py)",
)

DISPOSABLE_TASK_ID = uuid.UUID("00000000-0000-4000-8000-00000000d15b")
DISPOSABLE_USER_ID = uuid.UUID("00000000-0000-4000-8000-00000000d15c")


class DisposableHostBreakGlass:
    """OD-BG-2's disposable/test-environment break-glass record store. It
    grants every claim — the whole point of a record is that a superuser
    issued it, and here the test is that superuser — so it exists only in
    tests, for a host with no data worth protecting. Every claim is kept, so a
    test can assert the unconfined path was really taken."""

    def __init__(self) -> None:
        self.claims: list[BreakGlassClaim] = []
        self.invocations: list[tuple[BreakGlassClaim, dict]] = []

    def claim(self, *, task_id: uuid.UUID, user_id: uuid.UUID, executable: str) -> BreakGlassClaim:
        claim = BreakGlassClaim(record_id="disposable-test", task_id=task_id, user_id=user_id,
                                executable=executable, remaining=1)
        self.claims.append(claim)
        return claim

    def record_invocation(self, claim: BreakGlassClaim, **details: Any) -> None:
        self.invocations.append((claim, details))


class DisposableHostExecutor(ConstrainedProcessExecutor):
    """Every allow-listed executable, run through the break-glass path as the
    disposable test task. Named for what it is: unconfined."""

    def __init__(self, **kwargs: Any) -> None:
        allowed = tuple(kwargs.pop("allowed_executables", ()))
        self.break_glass = DisposableHostBreakGlass()
        super().__init__(allowed_executables=allowed, break_glass_executables=allowed,
                         break_glass=self.break_glass, **kwargs)

    async def run(self, argv, *, cwd, task_id=None, user_id=None, **kwargs):  # type: ignore[override]
        return await super().run(argv, cwd=cwd, task_id=task_id or DISPOSABLE_TASK_ID,
                                 user_id=user_id or DISPOSABLE_USER_ID, **kwargs)


def landlock_or_disposable_executor(**kwargs: Any) -> ConstrainedProcessExecutor:
    """For adapter/lifecycle tests: confined where the host can confine,
    otherwise explicitly the disposable-host break-glass executor."""

    return ConstrainedProcessExecutor(**kwargs) if HAS_LANDLOCK else DisposableHostExecutor(**kwargs)


TEST_CLIENT_ID = "test-oidc-client-id.apps.googleusercontent.example"
TEST_ISSUER = "https://accounts.google.test"
TEST_KEK_ENV_VAR = "HYPERMIND_TEST_KEK"


def make_test_config(**overrides: Any) -> AppConfig:
    """A valid AppConfig for tests, with a distinct SQLite file per caller."""

    payload: dict[str, Any] = {
        "server": {"base_url": "http://test.invalid"},
        "security": {"oidc": {"client_id": TEST_CLIENT_ID, "issuer": TEST_ISSUER}},
        "secrets": {"store": "encrypted_local", "kek_source": f"env:{TEST_KEK_ENV_VAR}"},
        "database_url": f"sqlite+aiosqlite:///./test_{uuid.uuid4().hex}.db",
    }
    payload.update(overrides)
    return AppConfig.model_validate(payload)


def make_test_kek() -> str:
    """A fresh, correctly sized KEK for one test. Never a fixed literal — a
    hard-coded key in a test file is the kind of value that later gets copied
    into a deployment."""

    return generate_kek_value()


@dataclass
class LocalOIDCProvider:
    """An OIDC provider backed by a locally generated RSA key.

    Satisfies `server.auth.oidc.OIDCProvider` structurally. This is not a stub
    that skips validation: it mints **real** RS256 id_tokens which the production
    `validate_id_token` verifies against a real JWKS. That is what makes the
    negative tests meaningful — a tampered signature, a wrong audience, or a
    stale nonce fails for the same reason it would against Google.

    `next_claims` lets a test control exactly what the provider returns, so each
    of 03 §2.3's checks can be defeated one at a time.
    """

    client_id: str = TEST_CLIENT_ID
    issuer_value: str = TEST_ISSUER
    subject: str = "google-subject-000001"
    email: str | None = "person@example.test"
    display_name: str | None = "Test Person"
    kid: str = "test-key-1"
    # Overrides applied to the next minted id_token's claims. A value of
    # `_OMIT` removes the claim entirely.
    claim_overrides: dict[str, Any] = field(default_factory=dict)
    # Set to sign with a *different* key than the one published in the JWKS,
    # which is how the "forged id_token" case is exercised.
    sign_with_foreign_key: bool = False
    last_nonce: str | None = None
    last_code_verifier: str | None = None

    def __post_init__(self) -> None:
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._foreign_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    @property
    def issuer(self) -> str:
        return self.issuer_value

    def authorization_url(
        self, *, state: str, nonce: str, code_challenge: str, redirect_uri: str
    ) -> str:
        self.last_nonce = nonce
        return (
            f"https://accounts.google.test/authorize?state={state}"
            f"&nonce={nonce}&code_challenge={code_challenge}&redirect_uri={redirect_uri}"
        )

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> str:
        self.last_code_verifier = code_verifier
        return self.mint_id_token(nonce=self.last_nonce or "")

    async def jwks(self) -> dict[str, Any]:
        from jwt.algorithms import RSAAlgorithm

        published = RSAAlgorithm.to_jwk(self._key.public_key(), as_dict=True)
        published["kid"] = self.kid
        published["alg"] = "RS256"
        published["use"] = "sig"
        return {"keys": [published]}

    def mint_id_token(self, *, nonce: str, **overrides: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self.issuer_value,
            "sub": self.subject,
            "aud": self.client_id,
            "iat": now,
            "nbf": now,
            "exp": now + 300,
            "nonce": nonce,
            "email": self.email,
            "name": self.display_name,
        }
        claims.update(self.claim_overrides)
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not _OMIT}

        key = self._foreign_key if self.sign_with_foreign_key else self._key
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": self.kid})


class _Omit:
    """Sentinel: remove a claim rather than set it to None."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<omit>"


_OMIT = _Omit()
OMIT = _OMIT


TEST_SUPERUSER_TOKEN = "TEST-ONLY-superuser-token-of-sufficient-length"


def make_superuser(monkeypatch) -> "SuperuserPrincipal":
    """A verified superuser principal, minted the only way there is: the
    out-of-band credential checked by `authenticate_superuser`."""

    from server.gateway.superuser_auth import SuperuserPrincipal
    from server.security.superuser import SUPERUSER_TOKEN_ENV, authenticate_superuser

    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, TEST_SUPERUSER_TOKEN)
    return SuperuserPrincipal(grant=authenticate_superuser(TEST_SUPERUSER_TOKEN), request_id=uuid.uuid4())
