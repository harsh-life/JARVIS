"""Authentication failures (03 §2.3, §5.4).

`[LOCKED]` (03 §2.3) any validation failure → `401 unauthenticated`, an
AuditEvent, **no session, no user mutation**. These types exist so that
outcome cannot be reached by accident: there is no "authenticated with
warnings" state and no partial principal.

The messages are deliberately generic. 02 §1.6 requires a security failure not
to leak *why* in a way that aids probing, so "the audience claim did not match"
never reaches a client — it goes to the audit trail and the server log instead.
"""

from __future__ import annotations


class AuthError(Exception):
    """Base for every authentication failure.

    `reason` is the internal, auditable detail; `str(exc)` is the client-safe
    message. Keeping them as separate fields is what stops a handler from
    rendering the diagnostic by default.
    """

    client_message = "authentication failed"

    def __init__(self, reason: str) -> None:
        super().__init__(self.client_message)
        self.reason = reason


class InvalidIdToken(AuthError):
    """An id_token failed one of 03 §2.3's seven checks."""


class InvalidLoginState(AuthError):
    """The `state` was absent, already used, or expired (03 §2.3 check 6).

    Covers both the CSRF case and the replay case (AUTH-T2).
    """


class InvalidBootstrapToken(AuthError):
    """The bootstrap token was absent, already used, expired, or bound to a
    different user (03 §3.2)."""


class InvalidDeviceProof(AuthError):
    """The device-credential proof did not verify (03 §4).

    Raised for a malformed proof, a bad signature, a stale timestamp, a replayed
    nonce, a revoked device, and an inactive user alike — the client learns only
    that it must sign in again (03 §5.4).
    """


class InvalidAccessToken(AuthError):
    """The bearer token was absent, unknown, or revoked."""


class AccessTokenExpired(AuthError):
    """The bearer token is past its expiry.

    Distinct from `InvalidAccessToken` because 02 §1.7 gives it its own code
    (`token_expired`) so the client knows to refresh rather than to re-login
    (AUTH-T8). This is the one authentication distinction that is safe to
    surface: it discloses nothing about another principal.
    """

    client_message = "access token expired"


class StepUpRequired(AuthError):
    """A sensitive operation needs fresh authentication (03 §5.5, SESSION-003)."""

    client_message = "this operation requires re-authentication"
