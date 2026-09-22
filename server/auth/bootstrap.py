"""Bootstrap tokens — authenticating the one call that has no device yet (03 §3.2).

The chicken-and-egg this solves: `POST /api/v1/devices` must be authenticated,
but the client has no device credential until that call returns one, and a
Google id_token is never a Hypermind API credential (AUTH-004).

`[IMPL, constrained]` (03 §3.2) the form is the engineer's choice; the
constraints are locked. This implementation is opaque-random + server record,
which gives each locked property a mechanism rather than a convention:

| Locked constraint | Mechanism |
|---|---|
| single-use | `used_at`, spent by a conditional UPDATE |
| short TTL | `expires_at`, checked on consume |
| bound to that user | `user_id` on the row, returned by consume |
| register-only scope | the token grants nothing else because nothing else accepts it — `consume` is called from exactly one place (`server/auth/device.py`) |

The last row is the important one: the scope is narrow because of an *absence*
of other consumers, not because of a scope claim that some future endpoint might
also choose to honour.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.errors import InvalidBootstrapToken
from server.secrets.crypto import generate_token, hash_token
from server.storage.models import BootstrapToken

# Long enough to walk back from the browser to the app, short enough that a
# leaked bootstrap token is not a standing device-registration permission.
BOOTSTRAP_TOKEN_TTL = timedelta(minutes=10)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def issue_bootstrap_token(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    ttl: timedelta = BOOTSTRAP_TOKEN_TTL,
) -> str:
    """Mint a token for the user who just completed OIDC login.

    Only the hash is stored; the returned string is the only copy, exactly as
    with the device credential itself (03 §3.1 step 5).
    """

    raw = generate_token()
    session.add(
        BootstrapToken(
            token_hash=hash_token(raw),
            user_id=user_id,
            created_at=_utcnow(),
            expires_at=_utcnow() + ttl,
        )
    )
    await session.flush()
    return raw


async def consume_bootstrap_token(session: AsyncSession, token: str) -> uuid.UUID:
    """Validate and spend a bootstrap token; return the bound user id.

    `[LOCKED]` (03 §3.1 step 6, AUTH-T2) single-use. The spend is a conditional
    UPDATE on `used_at IS NULL`, so two concurrent registrations cannot both
    succeed — checking then writing would let both pass the check before either
    wrote.

    Every failure is `InvalidBootstrapToken`: a client cannot tell an expired
    token from an unknown one from someone else's.
    """

    if not token:
        raise InvalidBootstrapToken("absent")

    row = await session.get(BootstrapToken, hash_token(token))
    if row is None:
        raise InvalidBootstrapToken("unknown")
    if row.used_at is not None:
        raise InvalidBootstrapToken("replayed")

    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= _utcnow():
        raise InvalidBootstrapToken("expired")

    result = await session.execute(
        update(BootstrapToken)
        .where(
            BootstrapToken.token_hash == row.token_hash,
            BootstrapToken.used_at.is_(None),
        )
        .values(used_at=_utcnow())
    )
    await session.flush()
    if result.rowcount != 1:
        raise InvalidBootstrapToken("replayed")

    return row.user_id
