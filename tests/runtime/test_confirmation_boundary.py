"""Runtime × confirmation (PERM-004/005/006/007, 05 §4, OD-F1).

Tier 1/2 run automatically inside an activated boundary; tier 3 pauses for an
explicit approval; tier 4 additionally needs a step-up-fresh session; the floor
is never offered. Every approval is a single-use token bound to one exact action,
and no timeout ever approves.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from server.secrets.crypto import hash_token
from server.storage.models import AccessToken, AgentTask, CapabilityGrant, ConfirmationToken
from shared.schemas.enums import CapabilityScopeType, Visibility
from tests.runtime.conftest import ask, call, failure_of, final, pending_of

pytestmark = pytest.mark.asyncio


async def _setup(h, *capabilities):
    alice = await h.user("alice")
    for capability in capabilities:
        await h.grant(alice, capability)
    return alice


async def test_low_risk_operations_proceed_without_any_confirmation(h):
    """Tier 1/2 (PRD §15): automatic once authorized — no token is even minted."""

    alice = await _setup(h, "file.read", "file.write")
    f = await h.file(alice, None, Visibility.PRIVATE, "a.txt")
    h.model.push(ask("file.read", "file.write"), call("files.read", "read_file", ref=f),
                 call("files.write", "write_file", ref=f, args={"x": 1}), final())

    resp = await h.submit(alice)

    assert resp.status_code == 200 and resp.json()["status"] == "completed"
    assert await h.rows(ConfirmationToken) == []


async def test_a_consequential_action_pauses_and_runs_only_on_approval(h):
    """RT-T3 / API-T4 / TL-T4: `delete_file` is tier 3. It returns
    `confirmation_required` with the explicit operation details, does not run,
    and runs exactly once after the user approves."""

    alice = await _setup(h, "file.write")
    f = await h.file(alice, None, Visibility.PRIVATE, "a.txt")
    h.model.push(ask("file.write"), call("files.write", "delete_file", ref=f))

    details = pending_of(await h.submit(alice))
    pending = details["pending"]
    assert pending["tool_id"] == "files.write" and pending["operation"] == "delete_file"
    assert pending["resource_ref"] == f and pending["risk_category"] == "consequential"
    assert pending["requires_step_up"] is False
    assert h.writes.calls == []

    h.model.push(final("deleted"))
    done = await h.confirm(alice, details["task_id"], details["confirmation_token"])

    assert done.status_code == 200, done.text
    assert h.writes.operations() == ["delete_file"]
    assert done.json()["response"] == "deleted"


async def test_declining_means_the_action_never_runs(h):
    alice = await _setup(h, "file.write")
    f = await h.file(alice, None, Visibility.PRIVATE, "a.txt")
    h.model.push(ask("file.write"), call("files.write", "delete_file", ref=f))
    details = pending_of(await h.submit(alice))

    h.model.push(final("ok, not deleting"))
    resp = await h.confirm(alice, details["task_id"], details["confirmation_token"], approve=False)

    assert resp.status_code == 200
    assert h.writes.calls == []
    assert "declined" in h.model.seen[-1][-1].content


async def test_a_high_irreversible_action_needs_strong_confirmation(h):
    """OD-F1 tier 4: confirmation **plus** step-up. A stale session's approval is
    refused with `step_up_required`, the action stays pending and unexecuted, and
    a freshly re-attested session can then approve it."""

    alice = await _setup(h, "file.write")
    h.model.push(ask("file.write"), call("files.write", "bulk_delete", args={"pattern": "*"}))
    details = pending_of(await h.submit(alice))
    assert details["pending"]["risk_category"] == "high_irreversible"
    assert details["pending"]["requires_step_up"] is True

    async with h.storage.session() as s:
        await s.execute(
            update(AccessToken)
            .where(AccessToken.token_hash == hash_token(alice.token))
            .values(issued_at=datetime.now(timezone.utc) - timedelta(minutes=30))
        )
        await s.commit()

    stale = await h.confirm(alice, details["task_id"], details["confirmation_token"])
    assert stale.status_code == 401
    assert stale.json()["error"]["details"].get("step_up_required") is True
    assert h.writes.calls == []
    assert (await h.get(alice, details["task_id"])).json()["status"] == "awaiting_confirmation"

    alice.token = await h.fresh_token(alice.device_id, alice.credential)
    h.model.push(final("bulk deleted"))
    fresh = await h.confirm(alice, details["task_id"], details["confirmation_token"])
    assert fresh.status_code == 200, fresh.text
    assert h.writes.operations() == ["bulk_delete"]


async def test_no_confirmation_timeout_ever_approves(h):
    """PERM-004 / RT-T3: an expired confirmation is a denial. The action is not
    performed — not on expiry, and not when approved late."""

    alice = await _setup(h, "file.write")
    f = await h.file(alice, None, Visibility.PRIVATE, "a.txt")
    h.model.push(ask("file.write"), call("files.write", "delete_file", ref=f))
    details = pending_of(await h.submit(alice))

    state = h.runtime.states.get(__import__("uuid").UUID(details["task_id"]))
    state.pending.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    late = await h.confirm(alice, details["task_id"], details["confirmation_token"])

    assert late.status_code == 409
    assert failure_of(late) == "confirmation_expired"
    assert h.writes.calls == []
    task = (await h.rows(AgentTask))[0]
    assert task.status == "failed"


async def test_a_wrong_token_changes_nothing(h):
    alice = await _setup(h, "file.write")
    f = await h.file(alice, None, Visibility.PRIVATE, "a.txt")
    h.model.push(ask("file.write"), call("files.write", "delete_file", ref=f))
    details = pending_of(await h.submit(alice))

    wrong = await h.confirm(alice, details["task_id"], "not-the-token")
    assert wrong.status_code == 409
    assert h.writes.calls == []

    h.model.push(final())
    right = await h.confirm(alice, details["task_id"], details["confirmation_token"])
    assert right.status_code == 200
    assert h.writes.operations() == ["delete_file"]

    replay = await h.confirm(alice, details["task_id"], details["confirmation_token"])
    assert replay.status_code == 409
    assert h.writes.operations() == ["delete_file"]


async def test_an_approval_covers_one_action_not_the_next(h):
    """PERM-004: "the user confirmed this task" never becomes a standing
    permission. Approving the deletion of A does not approve deleting B."""

    alice = await _setup(h, "file.write")
    a = await h.file(alice, None, Visibility.PRIVATE, "a.txt")
    b = await h.file(alice, None, Visibility.PRIVATE, "b.txt")
    h.model.push(ask("file.write"), call("files.write", "delete_file", ref=a))
    first = pending_of(await h.submit(alice))

    h.model.push(call("files.write", "delete_file", ref=b))
    second = pending_of(await h.confirm(alice, first["task_id"], first["confirmation_token"]))

    assert second["pending"]["resource_ref"] == b
    assert second["confirmation_token"] != first["confirmation_token"]
    assert [c.resource_ref for c in h.writes.calls] == [a]


async def test_the_same_user_may_approve_from_another_device(h):
    """Owner decision §4: same-user multi-device continuity. A task started on
    device 1 can be reviewed and approved on device 2 of the same user."""

    alice = await _setup(h, "file.write")
    f = await h.file(alice, None, Visibility.PRIVATE, "a.txt")
    h.model.push(ask("file.write"), call("files.write", "delete_file", ref=f))
    details = pending_of(await h.submit(alice))

    phone_two = await h.user("alice")
    assert phone_two.user_id == alice.user_id and phone_two.device_id != alice.device_id

    seen = (await h.get(phone_two, details["task_id"])).json()
    assert seen["status"] == "awaiting_confirmation"
    token = seen["pending"]["confirmation_token"]

    h.model.push(final())
    resp = await h.confirm(phone_two, details["task_id"], token)
    assert resp.status_code == 200, resp.text
    assert h.writes.operations() == ["delete_file"]


async def test_cancelling_a_paused_task_drops_the_action_and_its_grants(h):
    """05 §9: a cancelled task performs nothing further, and its task grants end."""

    alice = await h.user("alice")
    h.model.push(ask("file.write"))
    activation = pending_of(await h.submit(alice))
    f = await h.file(alice, None, Visibility.PRIVATE, "a.txt")
    h.model.push(call("files.write", "delete_file", ref=f))
    paused = pending_of(await h.confirm(alice, activation["task_id"], activation["confirmation_token"]))

    cancelled = await h.client.post(f"/api/v1/agent/tasks/{paused['task_id']}/cancel", headers=alice.auth)

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert h.writes.calls == []
    grants = await h.rows(CapabilityGrant, CapabilityGrant.scope_type == CapabilityScopeType.TASK)
    assert grants and all(g.revoked_at is not None for g in grants)
    late = await h.confirm(alice, paused["task_id"], paused["confirmation_token"])
    assert late.status_code == 409


async def test_a_floor_action_is_never_offered_as_confirmable(h):
    """RT-T4 / §39 #16: no confirmation token is ever minted for a floor action."""

    alice = await h.user("alice")
    h.model.push(ask("audit.disable"), ask("secret.export"), final("cannot"))

    resp = await h.submit(alice)

    assert resp.status_code == 200
    assert await h.rows(ConfirmationToken) == []
