"""Run tokens and request nonces (docs/29 §11.3) — pure rules, no store.

`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]` (docs/29). A run token is the only
credential an agent runtime ever holds, and it is deliberately weak:

* 32 random bytes, base64url on the wire, shown once (in the run's context)
  and stored only as its SHA-256;
* bound to one run, one agent, one spec hash and one gateway (`model` or
  `tool`) — useless for anything else;
* short-lived: it expires 30 s after the run's own deadline, never later;
* revocable: a stop, a cancel, a pause, a change or a delete revokes it, and
  the next request fails.

It carries no identity, no session, no provider key and no secret: whatever a
request with it asks is still decided by JARVIS, as the run's owner, under the
envelope, the mode and risk ceilings and the engine (docs/29 §10, §13.3).

A request nonce is at least 128 bits, base64url, and is never reused for a
different request on the same token (§11.3 replay protection).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from shared.schemas.agent_factory import RunTokenPurpose

TOKEN_BYTES = 32
NONCE_BYTES = 16
# |now - sent_at| beyond this is a stale request (docs/29 §11.3).
FRESHNESS = timedelta(seconds=60)
# A token outlives its run's deadline by this much, never more.
EXPIRY_GRACE = timedelta(seconds=30)
# Tokens and their nonces are kept this long after expiry, then pruned.
RETENTION = timedelta(hours=24)

_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{22,64}$")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def new_token() -> str:
    return _b64url(os.urandom(TOKEN_BYTES))


def new_nonce() -> str:
    return _b64url(os.urandom(NONCE_BYTES))


def token_digest(token: str) -> str:
    """What is stored and looked up: the token itself never is."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def well_formed_token(token: str) -> bool:
    return isinstance(token, str) and bool(_TOKEN.fullmatch(token))


def well_formed_nonce(nonce: str) -> bool:
    return isinstance(nonce, str) and bool(_NONCE.fullmatch(nonce))


def token_expiry(deadline: datetime) -> datetime:
    return deadline + EXPIRY_GRACE


def is_fresh(sent_at: datetime, now: datetime) -> bool:
    return abs(now - sent_at) <= FRESHNESS


@dataclass(frozen=True)
class TokenFacts:
    """A stored token, as the rules below need it."""

    token_hash: str
    run_id: object
    agent_id: object
    spec_hash: str
    purpose: str
    expires_at: datetime
    revoked_at: datetime | None


def binding_refusal(facts: TokenFacts | None, *, presented: str, purpose: RunTokenPurpose, run_id: object,
                    agent_id: object, run_spec_hash: str | None) -> str | None:
    """Is this the token for exactly this run, agent, spec and gateway?
    `None` if so, else why not (`unknown`, `purpose`, `binding`)."""

    if facts is None or not hmac.compare_digest(facts.token_hash, token_digest(presented)):
        return "unknown"
    if facts.purpose != purpose.value:
        return "purpose"
    if facts.run_id != run_id or facts.agent_id != agent_id:
        return "binding"
    if run_spec_hash is None or facts.spec_hash != run_spec_hash:
        return "binding"
    return None


def liveness_refusal(facts: TokenFacts, *, now: datetime) -> str | None:
    """Still usable now? `None`, `expired` or `revoked`."""

    if now >= facts.expires_at:
        return "expired"
    if facts.revoked_at is not None:
        return "revoked"
    return None


__all__ = [
    "EXPIRY_GRACE",
    "FRESHNESS",
    "NONCE_BYTES",
    "RETENTION",
    "TOKEN_BYTES",
    "TokenFacts",
    "binding_refusal",
    "is_fresh",
    "liveness_refusal",
    "new_nonce",
    "new_token",
    "token_digest",
    "token_expiry",
    "well_formed_nonce",
    "well_formed_token",
]
