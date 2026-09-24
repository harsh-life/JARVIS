"""Confirmation tokens — a security mechanism, not a UI boolean (PERM-004).

05 §4 `[LOCKED]`:

> When a proposal's `04` decision is `require_confirmation`, the runtime
> **pauses the task**, returns `confirmation_required` with a
> `confirmation_token`, and does nothing further on that branch until
> `/confirm` arrives. […] No timeout auto-approves. […] An **absolute-floor**
> proposal returns `prohibited` — it is never offered as confirmable.

The failure this module exists to prevent is a *reusable* approval. "The user
confirmed this task" must never become a generic permission, because the model
proposes the next step after the human approved the previous one. So a token
is bound to one exact action:

    principal + session + task + capability + operation
            + resource type + resource ref + argument hash

A token minted for `delete(file A)` therefore does not validate for
`delete(file B)`, for `read(file A)`, for the same call with different
arguments, for a different task, or for a different principal — and it
validates exactly once.

`[IMPL]`, documented: 05 §4 and 02 §5 lock that a `confirmation_token` exists
and gates the pending action, but do not enumerate its binding fields. The list
above is this branch's binding; each field is there because omitting it would
make a specific substitution attack work, and the tests name them one by one
(`tests/security_core/test_confirmation.py`).

Storage is opaque-token-plus-server-record rather than a signed self-contained
token, for the same reason 03 §5.2 `[REC]`s opaque access tokens: single-use
semantics and immediate invalidation are properties of a row, and a signed
token cannot be un-signed before its expiry.
"""

from __future__ import annotations

import uuid

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from server.capabilities.floor import AbsoluteFloorViolation, floor_category_for_request
from server.capabilities.risk import requires_confirmation
from server.secrets.crypto import generate_token, hash_token
from server.storage.models import ConfirmationToken
from shared.schemas.authorization import ActionBinding
from shared.schemas.enums import RiskCategory

__all__ = [
    "DEFAULT_CONFIRMATION_TTL",
    "ActionBinding",
    "ConfirmationRefused",
    "ConfirmationService",
    "IssuedConfirmation",
]

# `[IMPL]` (OD-AUTH-3 neighbourhood): long enough for a human to read a prompt
# and decide, short enough that a stolen token is not a standing permission.
DEFAULT_CONFIRMATION_TTL = timedelta(minutes=5)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ConfirmationRefused(Exception):
    """A confirmation token could not be issued.

    Distinct from a *validation* failure: this is "this action is not
    confirmable at all", which is what a floor action gets instead of a
    prompt.
    """


@dataclass(frozen=True)
class IssuedConfirmation:
    """`token` is the only copy of the raw value; only its hash is stored."""

    token: str
    expires_at: datetime
    risk_category: RiskCategory


class ConfirmationService:
    async def issue(
        self,
        session: AsyncSession,
        *,
        binding: ActionBinding,
        risk_category: RiskCategory,
        ttl: timedelta = DEFAULT_CONFIRMATION_TTL,
    ) -> IssuedConfirmation:
        """Mint a token for one action.

        Two refusals, both `[LOCKED]`:

        * **A floor action is never confirmable** (PERM-006, 05 §4, RT-T4). It
          does not get a prompt, because offering one would imply the human
          could authorize it.
        * **An automatic-tier action gets no token.** A token for something that
          needed no confirmation is a spare credential lying around, and its
          existence would suggest confirmation is advisory.
        """

        floor = floor_category_for_request(
            capability=binding.capability,
            operation=binding.operation,
            resource_type=binding.resource_type,
        )
        if floor is not None:
            raise AbsoluteFloorViolation(floor, binding.capability)

        if not requires_confirmation(risk_category):
            raise ConfirmationRefused(
                f"risk tier {risk_category.value} is automatic; no confirmation "
                f"token is issued for it (PERM-007)"
            )

        raw = generate_token()
        now = _utcnow()
        expires_at = now + ttl

        session.add(
            ConfirmationToken(
                token_hash=hash_token(raw),
                principal_user_id=binding.principal_user_id,
                session_id=binding.session_id,
                task_id=binding.task_id,
                capability=binding.capability,
                operation=binding.operation.value,
                resource_type=binding.resource_type.value,
                resource_ref=binding.resource_ref,
                arguments_hash=binding.arguments_hash(),
                risk_category=risk_category,
                issued_at=now,
                expires_at=expires_at,
            )
        )
        await session.flush()

        return IssuedConfirmation(token=raw, expires_at=expires_at, risk_category=risk_category)

    async def consume(
        self, session: AsyncSession, *, token: str, binding: ActionBinding
    ) -> bool:
        """Validate a token against the action being attempted, and spend it.

        Returns `True` only if every bound field matches, the token is
        unexpired, and this call is the one that marked it used. Every other
        outcome is `False` — there is no third result a caller could
        misinterpret as "probably fine".

        Expiry is a denial, never an approval: 05 §4's "no timeout
        auto-approves" is the reason this function has no branch that treats an
        elapsed deadline as consent.
        """

        if not token:
            return False

        row = await session.get(ConfirmationToken, hash_token(token))
        if row is None:
            return False

        if row.used_at is not None:
            return False

        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= _utcnow():
            return False

        if not _binding_matches(row, binding):
            return False

        # Single-use, enforced by the database rather than by the read above:
        # two concurrent confirmations both pass the `used_at is None` check,
        # and only the one whose conditional UPDATE matches a row wins.
        result = await session.execute(
            update(ConfirmationToken)
            .where(
                ConfirmationToken.token_hash == row.token_hash,
                ConfirmationToken.used_at.is_(None),
            )
            .values(used_at=_utcnow())
        )
        await session.flush()
        return result.rowcount == 1


    async def invalidate_for_task(
        self, session: AsyncSession, *, principal_user_id: uuid.UUID, task_id: str
    ) -> int:
        """Spend every unused token bound to `task_id` (18 §5.3 step 3).

        Called whenever a task ends — by completion, failure, cancellation or a
        breaker trip — so no token outlives the task it was issued for. It
        reuses the single-use mark rather than a separate revocation column:
        `consume` already refuses a spent token, so a token invalidated here can
        never authorize anything, and the same conditional UPDATE keeps it
        race-free against a concurrent `consume`. Returns how many were spent.
        """

        result = await session.execute(
            update(ConfirmationToken)
            .where(
                ConfirmationToken.principal_user_id == principal_user_id,
                ConfirmationToken.task_id == task_id,
                ConfirmationToken.used_at.is_(None),
            )
            .values(used_at=_utcnow())
        )
        await session.flush()
        return result.rowcount or 0


def _binding_matches(row: ConfirmationToken, binding: ActionBinding) -> bool:
    """Exact equality on every bound field.

    Written as one conjunction on purpose: a per-field early return invites a
    later edit that "temporarily" skips one comparison, and the field skipped
    would be the substitution that works.
    """

    return (
        row.principal_user_id == binding.principal_user_id
        and row.session_id == binding.session_id
        and row.task_id == binding.task_id
        and row.capability == binding.capability
        and row.operation == binding.operation.value
        and row.resource_type == binding.resource_type.value
        and row.resource_ref == binding.resource_ref
        and row.arguments_hash == binding.arguments_hash()
    )
