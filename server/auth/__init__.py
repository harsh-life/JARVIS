"""Authentication, identity, and sessions — 03_AUTH_IDENTITY_SESSION.md.

03 §0's distinction, which this package exists to hold:

> **Authentication ("who are you") and authorization ("what may you access") are
> separate systems** (AUTH-004). This document owns *authentication and session
> establishment only*. […] It makes **no** resource-access decisions.

So nothing in this package decides access. It produces a `Principal` (03 §8) and
stops. The authorization engine that consumes it is `server/graph`, which sits
*below* this package in the dependency order (16 §2) and re-checks every
authorization fact itself.
"""

from server.auth.bootstrap import (
    BOOTSTRAP_TOKEN_TTL,
    consume_bootstrap_token,
    issue_bootstrap_token,
)
from server.auth.device import (
    PROOF_FRESHNESS,
    DeviceService,
    RegisteredDevice,
    build_device_proof,
)
from server.auth.errors import (
    AccessTokenExpired,
    AuthError,
    InvalidAccessToken,
    InvalidBootstrapToken,
    InvalidDeviceProof,
    InvalidIdToken,
    InvalidLoginState,
    StepUpRequired,
)
from server.auth.login import LoginCompletion, LoginStart, OIDCLoginFlow
from server.auth.oidc import (
    SCOPES,
    GoogleOIDCProvider,
    OIDCIdentity,
    OIDCProvider,
    validate_id_token,
)
from server.auth.repository import AuthRepository
from server.auth.sessions import (
    ACCESS_TOKEN_TTL,
    STEP_UP_WINDOW,
    IssuedAccessToken,
    ResolvedSession,
    SessionService,
)

__all__ = [
    "ACCESS_TOKEN_TTL",
    "BOOTSTRAP_TOKEN_TTL",
    "PROOF_FRESHNESS",
    "SCOPES",
    "STEP_UP_WINDOW",
    "AccessTokenExpired",
    "AuthError",
    "AuthRepository",
    "DeviceService",
    "GoogleOIDCProvider",
    "InvalidAccessToken",
    "InvalidBootstrapToken",
    "InvalidDeviceProof",
    "InvalidIdToken",
    "InvalidLoginState",
    "IssuedAccessToken",
    "LoginCompletion",
    "LoginStart",
    "OIDCIdentity",
    "OIDCLoginFlow",
    "OIDCProvider",
    "RegisteredDevice",
    "ResolvedSession",
    "SessionService",
    "StepUpRequired",
    "build_device_proof",
    "consume_bootstrap_token",
    "issue_bootstrap_token",
    "validate_id_token",
]
