"""Security Core extensions made by the runtime branch.

* TASK-scoped grants were unusable: `_candidate_principal_ids` never included
  the task id, so a task grant could never match. The runtime's on-demand
  activation needs them, so the fix binds a task grant to its task **and** to
  the user who consented to it.
* The usage limiter (13) fails closed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from server.capabilities.grants import CapabilityGrantService
from server.security.usage import LimitExceeded, UsageLimits, UsagePolicy
from shared.schemas.authorization import CapabilityCheckContext, Principal
from shared.schemas.enums import CapabilityScopeType

pytestmark = pytest.mark.asyncio


# ── task-scoped grants ─────────────────────────────────────────────────────


async def test_a_task_grant_matches_only_its_task_and_its_consenting_user(db, world):
    """The Security Core fix this branch needed: a TASK grant is a candidate
    (keyed on the task id) and binds to the user who consented to it."""

    grants = CapabilityGrantService()
    task_id = uuid.uuid4()
    await grants.grant(
        db, principal_id=task_id, scope_type=CapabilityScopeType.TASK, capability="file.read",
        granted_by=world.alice.user_id, expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    def ctx(user, task):
        principal = Principal(user_id=user.user_id, device_id=uuid.uuid4(), session_id=uuid.uuid4())
        return CapabilityCheckContext(principal=principal, task_id=task)

    assert await grants.has_capability(db, capability="file.read", context=ctx(world.alice, str(task_id)))
    assert not await grants.has_capability(db, capability="file.read", context=ctx(world.alice, str(uuid.uuid4())))
    assert not await grants.has_capability(db, capability="file.read", context=ctx(world.bob, str(task_id)))
    assert not await grants.has_capability(db, capability="file.read", context=ctx(world.alice, None))
    assert not await grants.has_capability(db, capability="file.read", context=ctx(world.alice, "not-a-uuid"))


# ── usage limiter ──────────────────────────────────────────────────────────


async def test_an_unreadable_ledger_is_treated_as_at_limit(db, monkeypatch):
    """13 §4: never "allow unlimited because the limiter errored"."""

    policy = UsagePolicy(limits=UsageLimits(100, 100, 100, 10.0, 10.0))

    async def broken(*args, **kwargs):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(policy.ledger, "calls_since", broken)
    with pytest.raises(LimitExceeded) as exc:
        await policy.precheck(db, user_id=uuid.uuid4(), device_id=None, projected_cost=0.0)
    assert exc.value.limit == "limiter_unavailable"
