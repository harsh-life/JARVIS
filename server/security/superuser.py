"""Superuser / server-administrator authority (12 §4, SUPER-001).

The distinction this module exists to hold:

> **normal user  ≠  agent  ≠  superuser / server administrator**

`[LOCKED]` (12 §4) a Hypermind user who authenticated through Google is
**not** the server superuser, no matter what graphs they own or what
capabilities they hold. There is deliberately:

- **no endpoint** that grants superuser authority;
- **no `CapabilityGrant`** that confers it (the absolute-floor registry
  refuses to create one — `server/capabilities/floor.py`);
- **no path from a `User`, `Session`, `Device`, or `Principal`** to a
  `SuperuserGrant` anywhere in this codebase.

Superuser authority is established only by presenting a credential the
application never issues, never stores, and never returns: an operator-set
environment variable, supplied out-of-band exactly like the KEK. It is a
**separate** variable from the KEK, so holding one does not yield the other
(12 §4's "application compromise does not automatically yield the master
key / superuser creds").

Honest bound, stated the way 14 §0 requires: this separation is enforced by
*application code and process environment*. An attacker with arbitrary code
execution inside this process can read both env vars. That is not something
this module can fix on a single-process pilot — it is exactly OD-A1, and it
is measured in `docs/OD_A1_BR_T2.md` rather than claimed closed.
"""

from __future__ import annotations

import hashlib
import hmac
import os

from server.secrets.requester import (
    SuperuserGrant,
    fingerprint,
)

SUPERUSER_TOKEN_ENV = "HYPERMIND_SUPERUSER_TOKEN"

# 12 §3's entropy discipline applies here too: a short operator-chosen string
# would make the one credential separating the application from its master
# keys guessable.
MIN_SUPERUSER_TOKEN_LENGTH = 32


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


class SuperuserAuthenticationFailed(Exception):
    """Presented credential did not authenticate.

    Carries no detail about *why* — a caller probing this should learn
    nothing about the configured credential.
    """


class SuperuserNotConfigured(Exception):
    """No superuser credential is configured on this deployment.

    Fail-closed: with no credential configured, superuser authority simply
    does not exist and `class=master_key` references are unresolvable by
    anyone. That is the correct default — a deployment that never needs
    superuser operations should not have a latent path to them.
    """


def authenticate_superuser(
    presented_token: str, *, environ: dict[str, str] | None = None
) -> SuperuserGrant:
    """Verify an out-of-band superuser credential and mint a grant.

    This is the **only** mint point for `SuperuserGrant` in the codebase.
    Comparison is constant-time so a timing oracle cannot recover the
    configured value byte by byte.
    """

    env = os.environ if environ is None else environ
    expected = env.get(SUPERUSER_TOKEN_ENV)

    if not expected:
        raise SuperuserNotConfigured(
            f"{SUPERUSER_TOKEN_ENV} is not set; this deployment has no superuser "
            f"authority and master-key references are unresolvable"
        )

    if len(expected) < MIN_SUPERUSER_TOKEN_LENGTH:
        # A misconfiguration, not an authentication failure — and refused
        # rather than accepted, because a weak superuser credential is worse
        # than none (12 §4).
        raise SuperuserNotConfigured(
            f"{SUPERUSER_TOKEN_ENV} must be at least {MIN_SUPERUSER_TOKEN_LENGTH} "
            f"characters of high-entropy material"
        )

    # Compared as fixed-length digests: `hmac.compare_digest` is constant-time
    # only for equal-length inputs, and returns early on a length mismatch —
    # which would let a timing probe learn the configured credential's length.
    # Hashing both sides first makes every comparison the same 32 bytes, and
    # it runs for an empty credential too, so no input takes a shorter path.
    matched = hmac.compare_digest(_digest(presented_token or ""), _digest(expected))
    if not presented_token or not matched:
        raise SuperuserAuthenticationFailed("superuser authentication failed")

    return SuperuserGrant._issue(fingerprint(expected))
