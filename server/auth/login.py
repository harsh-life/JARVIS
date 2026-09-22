"""The OIDC login flow — state, nonce, PKCE, and subject→user mapping (03 §2).

This module owns 03 §2.3's checks 6 and 7, the two that are properties of the
*login attempt* rather than of the token:

6. **State** existed, was unused, not expired — and is marked used
   **immediately** (single-use). This is both the CSRF defense and the reason a
   captured callback cannot be replayed (AUTH-T2).
7. **PKCE** — the `code_verifier` stored under this state is what the token
   exchange presents, so an intercepted authorization code is useless without
   the verifier that never left this server.

The token-intrinsic checks (1–5) are in `server/auth/oidc.py`.

`[LOCKED]` (03 §2.3) any failure → `401`, an AuditEvent, **no session and no
user mutation**. `complete()` is written so the mutation (user creation) happens
strictly after every check has passed — there is no path that creates a user and
then rejects the login (AUTH-T1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.bootstrap import issue_bootstrap_token
from server.auth.errors import AuthError, InvalidLoginState
from server.auth.oidc import (
    OIDCProvider,
    code_challenge_for,
    generate_code_verifier,
    validate_id_token,
)
from server.auth.repository import AuthRepository
from server.secrets.crypto import generate_token
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.storage.models import OIDCLoginState, User
from shared.schemas.enums import AuditActor, AuditResult

# Long enough for a human to complete Google's consent screen, short enough that
# an abandoned login is not a standing CSRF window.
LOGIN_STATE_TTL = timedelta(minutes=10)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LoginStart:
    redirect_url: str
    state: str


@dataclass(frozen=True)
class LoginCompletion:
    user: User
    created: bool
    needs_device_registration: bool
    bootstrap_token: str


class OIDCLoginFlow:
    def __init__(
        self,
        provider: OIDCProvider,
        *,
        redirect_uri: str,
        repository: AuthRepository | None = None,
        state_ttl: timedelta = LOGIN_STATE_TTL,
    ) -> None:
        self._provider = provider
        self._redirect_uri = redirect_uri
        self._repo = repository or AuthRepository()
        self._state_ttl = state_ttl

    async def start(self, session: AsyncSession, *, audit: AuditLogger) -> LoginStart:
        """03 §2.1: generate state, nonce, and a PKCE verifier/challenge pair,
        store `{state → (nonce, code_verifier)}` with a short TTL, and return the
        provider redirect.

        All three values come from the same CSPRNG the rest of the branch uses.
        The verifier is stored server-side and **never** sent to the client, so
        the client cannot complete an exchange it did not start.
        """

        state = generate_token()
        nonce = generate_token()
        code_verifier = generate_code_verifier()

        session.add(
            OIDCLoginState(
                state=state,
                nonce=nonce,
                code_verifier=code_verifier,
                redirect_uri=self._redirect_uri,
                created_at=_utcnow(),
                expires_at=_utcnow() + self._state_ttl,
            )
        )
        await session.flush()

        await audit.record(
            actor=AuditActor.SYSTEM,
            action=AuditAction.OIDC_LOGIN_STARTED,
            resource="auth:oidc",
            result=AuditResult.SUCCESS,
        )

        return LoginStart(
            redirect_url=self._provider.authorization_url(
                state=state,
                nonce=nonce,
                code_challenge=code_challenge_for(code_verifier),
                redirect_uri=self._redirect_uri,
            ),
            state=state,
        )

    async def complete(
        self, session: AsyncSession, *, code: str, state: str, audit: AuditLogger
    ) -> LoginCompletion:
        """03 §2.1's callback half.

        Order is load-and-spend-state → exchange → validate → *then* map to a
        user. The state is spent before the network call so a retry of the same
        callback cannot start a second exchange.
        """

        try:
            state_row = await self._spend_state(session, state)
            identity = await self._exchange_and_validate(
                code=code, state_row=state_row
            )
        except AuthError as exc:
            await audit.record(
                actor=AuditActor.SYSTEM,
                action=AuditAction.OIDC_LOGIN_REJECTED,
                resource="auth:oidc",
                result=AuditResult.BLOCKED,
            )
            raise exc

        # Every check has passed; only now is any state mutated (03 §2.4).
        user, created = await self._repo.find_or_create_user(session, identity)

        if created:
            await audit.record(
                actor=AuditActor.SYSTEM,
                action=AuditAction.USER_CREATED,
                resource=f"user:{user.user_id}",
                result=AuditResult.SUCCESS,
                user_id=user.user_id,
            )

        devices = await self._repo.active_devices_for_user(session, user_id=user.user_id)
        bootstrap_token = await issue_bootstrap_token(session, user_id=user.user_id)

        await audit.record(
            actor=AuditActor.SYSTEM,
            action=AuditAction.OIDC_LOGIN_SUCCEEDED,
            resource=f"user:{user.user_id}",
            result=AuditResult.SUCCESS,
            user_id=user.user_id,
        )
        await audit.record(
            actor=AuditActor.SYSTEM,
            action=AuditAction.BOOTSTRAP_TOKEN_ISSUED,
            resource=f"user:{user.user_id}",
            result=AuditResult.SUCCESS,
            user_id=user.user_id,
        )

        return LoginCompletion(
            user=user,
            created=created,
            needs_device_registration=not devices,
            bootstrap_token=bootstrap_token,
        )

    async def _spend_state(self, session: AsyncSession, state: str) -> OIDCLoginState:
        """Check 6: existed, unused, unexpired — then mark used immediately."""

        if not state:
            raise InvalidLoginState("absent")

        row = await session.get(OIDCLoginState, state)
        if row is None:
            raise InvalidLoginState("unknown")
        if row.used_at is not None:
            raise InvalidLoginState("replayed")

        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= _utcnow():
            raise InvalidLoginState("expired")

        # Conditional UPDATE, same reasoning as the bootstrap token: a
        # check-then-write would let two concurrent callbacks both proceed.
        result = await session.execute(
            update(OIDCLoginState)
            .where(
                OIDCLoginState.state == row.state,
                OIDCLoginState.used_at.is_(None),
            )
            .values(used_at=_utcnow())
        )
        await session.flush()
        if result.rowcount != 1:
            raise InvalidLoginState("replayed")

        return row

    async def _exchange_and_validate(self, *, code: str, state_row: OIDCLoginState):
        """Checks 7 then 1–5.

        The Google tokens obtained here are used for exactly one thing —
        deriving the identity — and are never returned, stored, or logged
        (03 §1: "never persisted by Hypermind").
        """

        raw_id_token = await self._provider.exchange_code(
            code=code,
            code_verifier=state_row.code_verifier,
            redirect_uri=state_row.redirect_uri,
        )

        return validate_id_token(
            raw_id_token,
            issuer=self._provider.issuer,
            audience=self._provider.client_id,
            expected_nonce=state_row.nonce,
            jwks=await self._provider.jwks(),
        )
