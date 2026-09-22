"""User / device / session reads and writes (03 §2.4, §3, §5).

The one rule worth naming here: `find_or_create_user` keys on **`(iss, sub)`**
and never on email.

> 03 §2.4 `[LOCKED]`: The stable key is the `(iss, sub)` pair […] **Never
> email** (email is mutable; a user can change their Google email and must
> remain the same Hypermind user).

`email`/`display_name` are written as non-authoritative profile labels, and
`update_profile_labels` exists precisely so a changed email updates the label
without touching identity (AUTH-T3).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.oidc import OIDCIdentity
from server.storage.models import AccessToken, Device, Session, User
from shared.schemas.enums import UserStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    """Normalize a stored timestamp to UTC-aware.

    SQLite returns naive datetimes even for `DateTime(timezone=True)` columns,
    so every expiry comparison has to go through this. A naive/aware comparison
    raises `TypeError`, which the engine's fail-closed wrapper would turn into a
    denial — correct, but it would make every expiry check look like an outage.
    """

    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class AuthRepository:
    # ── users (03 §2.4) ─────────────────────────────────────────────────

    async def find_user_by_subject(
        self, session: AsyncSession, *, issuer: str, subject: str
    ) -> User | None:
        result = await session.execute(
            select(User).where(User.oidc_issuer == issuer, User.oidc_subject == subject)
        )
        return result.scalars().first()

    async def find_or_create_user(
        self, session: AsyncSession, identity: OIDCIdentity
    ) -> tuple[User, bool]:
        """Returns `(user, created)`.

        Lookup is by `(oidc_issuer, oidc_subject)` — the unique index foundation
        created for exactly this. A returning user whose Google email changed
        matches on subject and keeps their `user_id`.
        """

        existing = await self.find_user_by_subject(
            session, issuer=identity.issuer, subject=identity.subject
        )
        if existing is not None:
            self.update_profile_labels(existing, identity)
            await session.flush()
            return existing, False

        user = User(
            oidc_subject=identity.subject,
            oidc_issuer=identity.issuer,
            display_name=identity.display_name,
            status=UserStatus.ACTIVE,
            created_at=utcnow(),
        )
        session.add(user)
        await session.flush()
        return user, True

    @staticmethod
    def update_profile_labels(user: User, identity: OIDCIdentity) -> None:
        """Refresh the non-authoritative labels. Identity fields are untouched:
        nothing here writes `oidc_subject` or `oidc_issuer`."""

        if identity.display_name:
            user.display_name = identity.display_name

    # ── devices (03 §3, §4) ─────────────────────────────────────────────

    async def get_device(self, session: AsyncSession, device_id: uuid.UUID) -> Device | None:
        return await session.get(Device, device_id)

    async def active_devices_for_user(
        self, session: AsyncSession, *, user_id: uuid.UUID
    ) -> list[Device]:
        result = await session.execute(
            select(Device).where(Device.user_id == user_id, Device.revoked.is_(False))
        )
        return list(result.scalars().all())

    # ── sessions & access tokens (03 §5) ────────────────────────────────

    async def get_session(
        self, session: AsyncSession, session_id: uuid.UUID
    ) -> Session | None:
        return await session.get(Session, session_id)

    async def get_access_token(
        self, session: AsyncSession, token_hash: str
    ) -> AccessToken | None:
        return await session.get(AccessToken, token_hash)

    async def revoke_tokens_for_device(
        self, session: AsyncSession, *, device_id: uuid.UUID
    ) -> int:
        """Kill every live token for a device (03 §4.4 revocation).

        03 §4.4 says existing short access tokens "expire on their own short
        timer", bounding exposure to the token TTL. Revoking them outright is
        strictly stronger and costs nothing given the opaque-token design
        (03 §5.2's `[REC]`), so revocation is immediate rather than
        TTL-bounded — the documented residual risk stays only where it is
        unavoidable (the window *before* the owner knows, 03 §7).
        """

        result = await session.execute(
            select(AccessToken).where(
                AccessToken.device_id == device_id,
                AccessToken.revoked_at.is_(None),
            )
        )
        rows = list(result.scalars().all())
        now = utcnow()
        for row in rows:
            row.revoked_at = now
        if rows:
            await session.flush()
        return len(rows)
