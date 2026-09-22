"""SecretStore failure types.

`[LOCKED]` (12 §8) every one of these is a *failure*, never a fallback.
There is deliberately no exception here that a caller could catch and
proceed "without the credential" — the operation needing the secret is
denied instead (FAIL-012). That is why `SecretDenied` and `SecretStoreLocked`
are separate types from a generic lookup miss: a caller cannot accidentally
treat "you may not have this" as "this does not exist".
"""

from __future__ import annotations


class SecretStoreError(Exception):
    """Base for every SecretStore failure."""


class SecretStoreLocked(SecretStoreError):
    """The store has not been unlocked with the KEK (12 §3/§8).

    Surfaced as an explicit locked state — never a degraded, silently
    secretless mode that fabricates success.
    """


class SecretDenied(SecretStoreError):
    """Access mediation (12 §2) refused this requester for this handle.

    The message never names *why* in a way that would let a caller probe
    another scope's handles, and never contains any secret material.
    """


class SecretNotFound(SecretStoreError):
    """No such handle. Distinct from `SecretDenied` only inside the store —
    callers above the boundary must not surface the difference to a client
    (04 §7 anti-enumeration)."""


class SecretIntegrityError(SecretStoreError):
    """AEAD authentication failed (12 §3/§8): the ciphertext, its nonce, or
    its bound handle was tampered with, or the wrong key was supplied.

    `[LOCKED]` hard fail — never proceed with suspect material.
    """


class KEKUnavailable(SecretStoreError):
    """The KEK could not be obtained from its configured source (12 §3).

    Fail-closed: the server surfaces a locked state rather than starting in
    a mode where secrets silently cannot be resolved.
    """
