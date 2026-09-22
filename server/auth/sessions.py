"""Access tokens, sessions, and principal resolution (03 §5, §6).

The three credentials 03 §1 says never to confuse stay separate here:

| Credential | Where it appears in this module |
|---|---|
| Google id_token | nowhere — it is validated and discarded in `login.py` |
| device credential | only as the input to `issue()`, already verified by `device.py` |
| access token | what this module issues, validates, and revokes |

`[LOCKED]` (03 §5.2, resolving OD-AUTH-2 with the doc's `[REC]`) access tokens
are **opaque, with a server-side lookup**, not self-contained JWTs. The trade-off
03 §5.2 describes lands on the side of instant revocation: at pilot scale the
per-call lookup is cheap, and it means a revoked device's live tokens die
immediately rather than at the end of their TTL.

`[LOCKED]` (03 §5.1, `01` §2.3, AUTH-T10) `Session.user_id` **must** equal
`Device.user_id`. This module never accepts a user id as an argument — it reads
it from the verified `Device` and builds the row through
`shared.schemas.identity.new_session_for_device`, foundation's
constructor whose entire purpose is making that mismatch unconstructible.

`[LOCKED]` (03 §5.3, PHONE-003, AUTH-T7) the principal is derived **from the
token**. `resolve_principal` takes a bearer token and nothing else; there is no
parameter through which a request body could contribute a `user_id`,
`device_id`, `session_id`, or `graph_id`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession as DbSession

from server.auth.errors import (
    AccessTokenExpired,
    InvalidAccessToken,
    StepUpRequired,
)
from server.auth.repository import AuthRepository, as_utc, utcnow
from server.secrets.crypto import generate_token, hash_token
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.storage.models import AccessToken, Device, Session, User
from shared.schemas.authorization import Principal
from shared.schemas.enums import AuditActor, AuditResult, UserStatus
from shared.schemas.identity import Device as DeviceContract
from shared.schemas.identity import new_session_for_device

# 03 §5.1's "short TTL". Short enough that a stolen token's window is small
# (03 §7's SEC-K row), long enough not to force a refresh on every interaction.
ACCESS_TOKEN_TTL = timedelta(minutes=15)

# 03 §5.5 / OD-AUTH-3 `[IMPL]`: how recently the device must have re-attested for
# a sensitive operation to proceed. Because a token is only ever issued against a
# freshly verified device-credential proof, a token's `issued_at` *is* the last
# re-attestation time — so freshness needs no separate mechanism to track.
STEP_UP_WINDOW = timedelta(minutes=5)


@dataclass(frozen=True)
class IssuedAccessToken:
    """`access_token` is the only copy of the raw value; only its hash is stored."""

    access_token: str
    expires_at: datetime
    session_id: uuid.UUID


@dataclass(frozen=True)
class ResolvedSession:
    """A validated bearer token, resolved to 03 §8's handoff contract.

    `token_issued_at` is carried so `require_step_up` can judge freshness without
    a second lookup — and so a caller cannot judge it from anything else.
    """

    principal: Principal
    token_issued_at: datetime
    token_hash: str


class SessionService:
    def __init__(self, *, repository: AuthRepository | None = None) -> None:
        self._repo = repository or AuthRepository()

    # ── 03 §5.1 issue / refresh ─────────────────────────────────────────

    async def issue(
        self,
        session: DbSession,
        *,
        device: Device,
        audit: AuditLogger,
        ttl: timedelta = ACCESS_TOKEN_TTL,
    ) -> IssuedAccessToken:
        """Create a Session and issue an access token bound to it.

        `device` must already have had its credential proof verified by
        `DeviceService.verify_proof`. This method does not re-verify, but it also
        cannot be reached with an unverified device from any endpoint: the router
        calls the two in sequence and the proof is the only source of a `Device`
        on that path.
        """

        record = new_session_for_device(
            device=DeviceContract.model_validate(device),
            expires_at=utcnow() + ttl,
        )

        db_session = Session(
            session_id=record.session_id,
            device_id=record.device_id,
            # Taken from the Device via the contract constructor above, never
            # from a caller (AUTH-T10).
            user_id=record.user_id,
            active_graph_id=None,
            issued_at=record.issued_at,
            expires_at=record.expires_at,
            scope=None,
        )
        session.add(db_session)

        raw = generate_token()
        now = utcnow()
        session.add(
            AccessToken(
                token_hash=hash_token(raw),
                session_id=db_session.session_id,
                user_id=db_session.user_id,
                device_id=db_session.device_id,
                issued_at=now,
                expires_at=now + ttl,
            )
        )
        await session.flush()

        await audit.record(
            actor=AuditActor.USER,
            action=AuditAction.ACCESS_TOKEN_ISSUED,
            resource=f"session:{db_session.session_id}",
            result=AuditResult.SUCCESS,
            user_id=db_session.user_id,
            device_id=db_session.device_id,
            session_id=db_session.session_id,
        )

        return IssuedAccessToken(
            access_token=raw,
            expires_at=now + ttl,
            session_id=db_session.session_id,
        )

    # ── 03 §5.3 validation ──────────────────────────────────────────────

    async def resolve_principal(self, session: DbSession, bearer_token: str) -> ResolvedSession:
        """Validate a bearer token and derive the principal from it alone.

        The chain 02 §1.2 requires, in order: valid token → device (not revoked)
        → user (active) → session (not expired). Each link is re-read live, so a
        device revoked or a user suspended mid-session fails the *next* request
        rather than at the end of the token's TTL.
        """

        if not bearer_token:
            raise InvalidAccessToken("absent")

        token_row = await self._repo.get_access_token(session, hash_token(bearer_token))
        if token_row is None:
            raise InvalidAccessToken("unknown")
        if token_row.revoked_at is not None:
            raise InvalidAccessToken("revoked")

        now = utcnow()
        if as_utc(token_row.expires_at) <= now:
            # The one authentication distinction safe to surface: it tells the
            # client to refresh rather than re-login (02 §1.7, AUTH-T8) and
            # discloses nothing about another principal.
            raise AccessTokenExpired("expired")

        device = await self._repo.get_device(session, token_row.device_id)
        if device is None or device.revoked:
            raise InvalidAccessToken("device_revoked")

        user = await session.get(User, token_row.user_id)
        if user is None or user.status is not UserStatus.ACTIVE:
            raise InvalidAccessToken("user_not_active")

        session_row = await self._repo.get_session(session, token_row.session_id)
        if session_row is None:
            raise InvalidAccessToken("session_missing")
        if as_utc(session_row.expires_at) <= now:
            raise AccessTokenExpired("session_expired")

        # Defense in depth for AUTH-T10: the invariant is enforced at
        # construction, and re-asserted here so a row written by any other means
        # cannot produce a cross-user principal.
        if session_row.user_id != device.user_id:
            raise InvalidAccessToken("session_device_user_mismatch")

        return ResolvedSession(
            principal=Principal(
                user_id=device.user_id,
                device_id=device.device_id,
                session_id=session_row.session_id,
                # Carried, but explicitly not trusted as a grant: 04 re-checks
                # membership against it on every request (04 §9).
                active_graph_id=session_row.active_graph_id,
            ),
            token_issued_at=as_utc(token_row.issued_at),
            token_hash=token_row.token_hash,
        )

    # ── 03 §5.5 step-up (SESSION-003) ───────────────────────────────────

    def require_step_up(
        self, resolved: ResolvedSession, *, window: timedelta = STEP_UP_WINDOW
    ) -> None:
        """Demand recent re-attestation for a sensitive operation.

        `[LOCKED]` (SESSION-003) some operations require fresh authentication
        even with a valid access token. Raising rather than returning a bool is
        deliberate: a bool invites a caller that forgets to check it.
        """

        if utcnow() - resolved.token_issued_at > window:
            raise StepUpRequired("stale_authentication")

    # ── 03 §6 logout ────────────────────────────────────────────────────

    async def logout(
        self, session: DbSession, *, resolved: ResolvedSession, audit: AuditLogger
    ) -> None:
        """End the current session (03 §6).

        `[LOCKED]` (03 §6) this does **not** revoke the device credential — the
        user can obtain a new token without re-login. Full sign-out is logout
        *plus* device revocation, which is a separate call by design: a shared or
        retired phone needs both, and conflating them would make every logout a
        re-pairing.
        """

        token_row = await self._repo.get_access_token(session, resolved.token_hash)
        if token_row is not None and token_row.revoked_at is None:
            token_row.revoked_at = utcnow()

        session_row = await self._repo.get_session(session, resolved.principal.session_id)
        if session_row is not None:
            # The session's own expiry is pulled back to now, so nothing can
            # re-attach to it even if another token referenced it.
            session_row.expires_at = utcnow()

        await session.flush()

        await audit.record(
            actor=AuditActor.USER,
            action=AuditAction.SESSION_LOGGED_OUT,
            resource=f"session:{resolved.principal.session_id}",
            result=AuditResult.SUCCESS,
            user_id=resolved.principal.user_id,
            device_id=resolved.principal.device_id,
            session_id=resolved.principal.session_id,
        )

    # ── 02 §4 active-graph switch (GRAPH-006) ───────────────────────────

    async def set_active_graph(
        self,
        session: DbSession,
        *,
        resolved: ResolvedSession,
        graph_id: uuid.UUID,
        audit: AuditLogger,
    ) -> Session | None:
        """Record the session's active graph.

        Membership is **not** checked here — the caller checks it through the
        authorization engine first, and this only records the result. Keeping the
        check out of this method avoids a second, divergent implementation of D1
        (§9 of the security-core scope: one authoritative predicate).

        Returns `None` if the session row has vanished, which the caller
        surfaces as an authentication failure rather than a silent success.
        """

        session_row = await self._repo.get_session(session, resolved.principal.session_id)
        if session_row is None:
            return None

        session_row.active_graph_id = graph_id
        await session.flush()

        await audit.record(
            actor=AuditActor.USER,
            action=AuditAction.ACTIVE_GRAPH_CHANGED,
            resource=f"session:{session_row.session_id}",
            result=AuditResult.SUCCESS,
            user_id=resolved.principal.user_id,
            device_id=resolved.principal.device_id,
            session_id=session_row.session_id,
            graph_id=graph_id,
        )
        return session_row
