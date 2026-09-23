"""Shared test support: configs, a local OIDC provider, and KEK material.

17 §6 `[LOCKED]`: "No fixture contains a real secret; test secrets are
clearly-marked test values." Every credential minted here is generated per-test
or carries an obvious test marker, and none is ever committed as a literal that
could be mistaken for a production value.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from server.config.schema import AppConfig
from server.execution import confinement
from server.secrets.kek import generate_kek_value

# Process tests exercise the executor's lifecycle (timeouts, output caps, env
# sanitization) on every host. Where the kernel can confine a process — Linux,
# including CI — they run confined, which also proves confinement does not break
# legitimate commands. Elsewhere (macOS) `landlock` mode refuses to run anything,
# so they opt into `unconfined` explicitly; the confinement guarantees are
# asserted separately (tests/execution/test_process_confinement.py).
TEST_PROCESS_CONFINEMENT = confinement.LANDLOCK if confinement.available() else confinement.UNCONFINED

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
