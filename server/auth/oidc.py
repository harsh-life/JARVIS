"""Google OIDC — the provider interface and id_token validation (03 §2).

Two things this module holds apart, because conflating them is AUTH-004's whole
point:

> **Google is the identity provider. Google is NOT the authorization system for
> Track B resources.**

So this module ends at "which Hypermind user is this". It issues no access
token, grants no capability, and touches no resource. The Google tokens it
validates are discarded immediately afterwards and never persisted (03 §1).

`[LOCKED]` (03 §2.2, AUTH-002/003) the requested scopes are exactly `openid`,
`email`, `profile`. No Gmail/Drive/Calendar/Contacts/Photos scope is requested,
so a Hypermind login grants zero access to any Google API. Future Google-data
access would be a *separate* consent flow, never bundled into login — which is
why `SCOPES` is a module constant rather than a parameter a caller could widen.

`[LOCKED]` (03 §2.3) all seven checks run on every login, or the token is
rejected. They are enumerated explicitly in `validate_id_token` rather than
delegated wholesale to a library default, so that each one is visible, testable,
and cannot silently disappear behind a dependency upgrade.
"""

from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import jwt
from jwt import PyJWK

from server.auth.errors import InvalidIdToken
from server.secrets.crypto import generate_token

# 03 §2.2 [LOCKED] — the minimal identity set, and nothing else.
SCOPES = ("openid", "email", "profile")

GOOGLE_ISSUER = "https://accounts.google.com"
GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"

# Only asymmetric RS256 is accepted. Naming the algorithm explicitly is what
# prevents algorithm-confusion attacks: an attacker-supplied `alg: none` or a
# symmetric `HS256` signed with the (public) JWKS modulus is rejected because it
# is not in this list, not because a library default happened to catch it.
ALLOWED_ALGORITHMS = ("RS256",)

# Tolerance for clock skew between Google and this server (seconds).
CLOCK_SKEW_LEEWAY = 60


@dataclass(frozen=True)
class OIDCIdentity:
    """What a validated id_token yields — 03 §2.4.

    `subject` + `issuer` are the stable identity key. `email` and `display_name`
    are carried as **non-authoritative profile labels only**: they key nothing
    and may change freely (AUTH-T3).
    """

    issuer: str
    subject: str
    email: str | None = None
    display_name: str | None = None


class OIDCProvider(Protocol):
    """The replaceable identity-provider interface (AUTH-001).

    MVP is Google-only, but the seam is here so a second provider is an adapter
    rather than a runtime change. Cross-provider account *linking* remains
    OD-AUTH-1 `[FUTURE]` — one `(iss, sub)` is one user (03 §2.5).
    """

    @property
    def issuer(self) -> str: ...

    @property
    def client_id(self) -> str: ...

    def authorization_url(
        self, *, state: str, nonce: str, code_challenge: str, redirect_uri: str
    ) -> str: ...

    async def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> str: ...

    async def jwks(self) -> dict[str, Any]: ...


# ── PKCE (03 §2.3 check 7) ──────────────────────────────────────────────────


def generate_code_verifier() -> str:
    """A PKCE code verifier: 43–128 chars of unreserved characters (RFC 7636)."""

    return generate_token(64)


def code_challenge_for(verifier: str) -> str:
    """The S256 challenge. Plain `code_challenge_method=plain` is never used —
    it would leave the verifier interceptable, which is the attack PKCE exists
    to close."""

    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ── id_token validation (03 §2.3) ───────────────────────────────────────────


def validate_id_token(
    raw_id_token: str,
    *,
    issuer: str,
    audience: str,
    expected_nonce: str,
    jwks: dict[str, Any],
    now: int | None = None,
) -> OIDCIdentity:
    """Run every one of 03 §2.3's checks, or reject.

    Checks 6 (state existed/unused/unexpired) and 7 (PKCE) are enforced by the
    login flow, which owns the state record — see `server/auth/login.py`. The
    five token-intrinsic ones are here:

    1. **Signature** against the provider's published JWKS.
    2. **Issuer** equals the expected issuer exactly.
    3. **Audience** equals this deployment's client_id exactly — this is what
       rejects a token minted for a different app.
    4. **Expiry / iat / nbf** sane.
    5. **Nonce** equals the nonce bound to this login's `state` — so an
       id_token cannot be replayed across logins.
    """

    now = int(time.time()) if now is None else now

    try:
        header = jwt.get_unverified_header(raw_id_token)
    except jwt.PyJWTError as exc:
        raise InvalidIdToken(f"malformed id_token: {exc}") from exc

    if header.get("alg") not in ALLOWED_ALGORITHMS:
        raise InvalidIdToken(f"unacceptable signing algorithm: {header.get('alg')!r}")

    key = _select_key(jwks, header.get("kid"))

    try:
        claims = jwt.decode(
            raw_id_token,
            key=key.key,
            algorithms=list(ALLOWED_ALGORITHMS),
            audience=audience,
            issuer=issuer,
            leeway=CLOCK_SKEW_LEEWAY,
            options={
                "require": ["exp", "iat", "iss", "aud", "sub"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidIdToken("id_token expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise InvalidIdToken("id_token audience is not this client") from exc
    except jwt.InvalidIssuerError as exc:
        raise InvalidIdToken("id_token issuer mismatch") from exc
    except jwt.PyJWTError as exc:
        raise InvalidIdToken(f"id_token rejected: {exc}") from exc

    # PyJWT tolerates `iat` in the future; 03 §2.3 check 4 asks for a sane one.
    issued_at = claims.get("iat")
    if not isinstance(issued_at, (int, float)) or issued_at > now + CLOCK_SKEW_LEEWAY:
        raise InvalidIdToken("id_token iat is not sane")

    # Check 5 — nonce. Compared here rather than left to the library because no
    # JWT library treats it as mandatory, and a missing nonce check is exactly
    # the replay hole 03 §7 lists as "effectively closed".
    presented_nonce = claims.get("nonce")
    if not presented_nonce or presented_nonce != expected_nonce:
        raise InvalidIdToken("id_token nonce does not match this login")

    subject = claims.get("sub")
    if not subject:
        raise InvalidIdToken("id_token carries no subject")

    return OIDCIdentity(
        issuer=str(claims["iss"]),
        subject=str(subject),
        email=claims.get("email"),
        display_name=claims.get("name"),
    )


def _select_key(jwks: dict[str, Any], kid: str | None) -> PyJWK:
    keys = jwks.get("keys") or []
    if not keys:
        raise InvalidIdToken("provider JWKS is empty")

    if kid is not None:
        for entry in keys:
            if entry.get("kid") == kid:
                return PyJWK.from_dict(entry)
        # A `kid` naming no published key means the token was not signed by the
        # provider's current key material. Falling back to "try every key" would
        # weaken the check to "any key the provider ever published".
        raise InvalidIdToken("id_token kid matches no published provider key")

    if len(keys) != 1:
        raise InvalidIdToken("id_token has no kid and the provider publishes several keys")
    return PyJWK.from_dict(keys[0])


# ── the Google adapter ──────────────────────────────────────────────────────


class GoogleOIDCProvider:
    """03 §2's concrete provider. Satisfies `OIDCProvider` structurally.

    `[LOCKED]` (03 §2) Track B's OIDC client is a **public client using PKCE**:
    there is no `client_secret` parameter here and no way to configure one, which
    is why `config.example.yaml` documents that a secret-shaped value in config
    is a load-time failure rather than a supported option.
    """

    def __init__(
        self,
        *,
        client_id: str,
        issuer: str = GOOGLE_ISSUER,
        http_client: Any | None = None,
        jwks_cache_seconds: int = 3600,
    ) -> None:
        self._client_id = client_id
        self._issuer = issuer
        self._http = http_client
        self._jwks_cache: tuple[float, dict[str, Any]] | None = None
        self._jwks_cache_seconds = jwks_cache_seconds

    @property
    def issuer(self) -> str:
        return self._issuer

    @property
    def client_id(self) -> str:
        return self._client_id

    def authorization_url(
        self, *, state: str, nonce: str, code_challenge: str, redirect_uri: str
    ) -> str:
        params = {
            "client_id": self._client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(SCOPES),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{urlencode(params)}"

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> str:
        client = self._client()
        response = await client.post(
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "client_id": self._client_id,
                "code": code,
                "code_verifier": code_verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )
        if response.status_code >= 400:
            # The provider's body may echo request parameters; it is never
            # surfaced to the client (02 §1.6) and never logged verbatim.
            raise InvalidIdToken(f"token exchange failed with status {response.status_code}")

        payload = response.json()
        raw = payload.get("id_token")
        if not raw:
            raise InvalidIdToken("token exchange returned no id_token")
        return str(raw)

    async def jwks(self) -> dict[str, Any]:
        """Fetch and cache the provider's keys (03 §2.3 check 1, "rotated")."""

        now = time.monotonic()
        if self._jwks_cache is not None:
            fetched_at, cached = self._jwks_cache
            if now - fetched_at < self._jwks_cache_seconds:
                return cached

        client = self._client()
        response = await client.get(GOOGLE_JWKS_URI)
        if response.status_code >= 400:
            raise InvalidIdToken("could not fetch provider JWKS")
        payload = response.json()
        self._jwks_cache = (now, payload)
        return payload

    def _client(self) -> Any:
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(timeout=10.0)
        return self._http
