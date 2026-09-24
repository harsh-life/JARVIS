"""Task modes and the mode ceiling — build unit U4
(`docs/18_SUPERVISORY_RUNTIME_RECOVERY_CONTROL.md` §3, OD-F1).

`app.interact` is the probe capability because its operations span the tiers:
`read_screen_element` (low_read) · `tap` (low_write) · `input_text`
(consequential). `files.write.bulk_delete` is high_irreversible.

| Requirement | Test |
|---|---|
| default mode is execute | `test_the_default_mode_is_execute` |
| explicit execute behaves normally | `test_execute_mode_chains_low_risk_operations_automatically` |
| draft / suggest / observe cannot execute | `test_non_execute_modes_run_reads_and_refuse_everything_else` |
| worker cannot alter the mode | `test_the_worker_cannot_set_or_promote_the_mode` |
| ceiling enforced before execution (and before the engine) | `test_the_ceiling_refuses_instead_of_offering_a_confirmation` |
| prohibited stays prohibited | `test_prohibited_stays_prohibited_in_every_mode` |
| consequential needs confirmation | `test_execute_mode_still_confirms_consequential_operations` |
| high-risk needs confirmation + step-up | `test_execute_mode_still_needs_step_up_for_high_impact` |
| draft cannot become execute | `test_the_worker_cannot_set_or_promote_the_mode`, `test_an_approval_is_re_checked_against_the_mode` |
| activation is not blocked for usable capabilities | `test_activation_follows_whether_any_operation_fits_the_mode` |
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from server.agent.modes import MODE_CEILING, capability_usable, within_ceiling
from server.secrets.crypto import hash_token
from server.storage.models import AccessToken, AgentTask, ConfirmationToken
from shared.schemas.agent import TaskMode
from shared.schemas.enums import RiskCategory
from tests.runtime.conftest import ask, call, failure_of, final, pending_of

pytestmark = pytest.mark.asyncio

PKG = {"package_name": "com.example"}
NON_EXECUTE = ["draft", "suggest", "observe"]


def ui(operation: str, **args) -> str:
    return call("ui.app", operation, args=args or {"id": "x"}, platform="android")


async def submit(h, actor, mode: str | None = None, text: str = "please help"):
    return await h.submit(actor, text, extra_body={"mode": mode} if mode is not None else None)


def completed(resp) -> dict:
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed", body
    return body


# ── the mode itself ────────────────────────────────────────────────────────


async def test_the_default_mode_is_execute(h):
    alice = await h.user("alice")
    h.model.push(final("done"))
    body = completed(await submit(h, alice))
    assert body["mode"] == "execute"
    [row] = await h.rows(AgentTask)
    assert row.mode == "execute"
    fetched = (await h.get(alice, body["task_id"])).json()
    assert fetched["mode"] == "execute"


async def test_an_unknown_mode_is_rejected_at_submission(h):
    alice = await h.user("alice")
    for bad in ("admin", "EXECUTE", "", "execute; drop"):
        resp = await submit(h, alice, bad)
        assert resp.status_code == 422, (bad, resp.text)
    assert await h.rows(AgentTask) == []


async def test_the_mode_is_recorded_and_reported(h):
    alice = await h.user("alice")
    for mode in NON_EXECUTE:
        h.model.push(final(f"{mode} result"))
        body = completed(await submit(h, alice, mode))
        assert body["mode"] == mode
    assert sorted(r.mode for r in await h.rows(AgentTask)) == sorted(NON_EXECUTE)


# ── execute: unchanged ─────────────────────────────────────────────────────


async def test_execute_mode_chains_low_risk_operations_automatically(h):
    """The user's explicit instruction authorizes low-risk chaining: no prompt
    per primitive (OD-F1)."""

    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    h.model.push(ask("app.interact", scope=PKG), ui("read_screen_element"), ui("tap"), ui("tap"), final("done"))

    completed(await submit(h, alice, "execute"))
    assert [c.operation for c in h.ui.calls] == ["read_screen_element", "tap", "tap"]


async def test_execute_mode_still_confirms_consequential_operations(h):
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    h.model.push(ask("app.interact", scope=PKG), ui("input_text", text="hi"))

    paused = pending_of(await submit(h, alice, "execute"))
    assert paused["pending"]["risk_category"] == "consequential"
    assert h.ui.calls == []


async def test_execute_mode_still_needs_step_up_for_high_impact(h):
    alice = await h.user("alice")
    await h.grant(alice, "file.write")
    h.model.push(ask("file.write"), call("files.write", "bulk_delete", args={"glob": "*"}))

    paused = pending_of(await submit(h, alice, "execute"))
    assert paused["pending"]["risk_category"] == "high_irreversible"
    assert paused["pending"]["requires_step_up"] is True
    async with h.storage.session() as s:  # age the session past step-up freshness
        await s.execute(update(AccessToken).where(AccessToken.token_hash == hash_token(alice.token))
                        .values(issued_at=datetime.now(timezone.utc) - timedelta(minutes=30)))
        await s.commit()
    stale = await h.confirm(alice, paused["task_id"], paused["confirmation_token"])
    assert stale.status_code == 401 and stale.json()["error"]["details"].get("step_up_required") is True
    assert h.writes.calls == []


# ── draft / suggest / observe: nothing executes ────────────────────────────


@pytest.mark.parametrize("mode", NON_EXECUTE)
async def test_non_execute_modes_run_reads_and_refuse_everything_else(h, mode):
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    h.model.push(
        ask("app.interact", scope=PKG),
        ui("read_screen_element"),      # low_read — runs
        ui("tap"),                      # low_write — refused
        ui("input_text", text="hi"),    # consequential — refused, not confirmed
        final(f"here is the {mode}"),
    )

    body = completed(await submit(h, alice, mode))

    assert body["mode"] == mode
    assert [c.operation for c in h.ui.calls] == ["read_screen_element"]
    assert await h.rows(ConfirmationToken) == []
    seen = h.model.all_text()
    assert f"not permitted in {mode} mode" in seen


@pytest.mark.parametrize("mode", NON_EXECUTE)
async def test_the_ceiling_refuses_instead_of_offering_a_confirmation(h, mode):
    """An over-ceiling consequential action is refused before the engine is
    asked: the user is never shown a confirmation for it, because approving it
    would make a draft execute."""

    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    h.model.push(ask("app.interact", scope=PKG), ui("input_text", text="send"), final("ok"))

    resp = await submit(h, alice, mode)

    assert resp.status_code == 200 and resp.json()["pending"] is None
    assert await h.rows(ConfirmationToken) == []
    assert h.ui.calls == []


@pytest.mark.parametrize("mode", NON_EXECUTE)
async def test_activation_follows_whether_any_operation_fits_the_mode(h, mode):
    """Activation is bookkeeping, not execution: a capability with a readable
    operation activates in any mode. One with nothing at or under the ceiling
    (`file.write`: low_write and above) is refused — not turned into a
    confirmation that could never lead anywhere."""

    alice = await h.user("alice")
    h.model.push(ask("file.write"), final("ok"))

    completed(await submit(h, alice, mode))

    seen = h.model.all_text()
    assert f"file.write: not activated: none of its operations can run in a {mode} task" in seen
    assert await h.rows(ConfirmationToken) == []  # no prompt was ever offered for file.write


@pytest.mark.parametrize("mode", NON_EXECUTE)
async def test_activation_of_a_readable_capability_still_works(h, mode):
    alice = await h.user("alice")
    h.model.push(ask("file.read"))

    paused = pending_of(await submit(h, alice, mode))
    assert paused["pending"]["kind"] == "capability_activation"
    assert paused["pending"]["capability"] == "file.read"


async def test_prohibited_stays_prohibited_in_every_mode(h):
    alice = await h.user("alice")
    for mode in ["execute", *NON_EXECUTE]:
        h.model.push(ask("superuser"), final("ok"))
        completed(await submit(h, alice, mode))
    assert await h.rows(ConfirmationToken) == []
    assert h.model.all_text().count("superuser: prohibited") == 4


# ── the worker cannot change the mode ──────────────────────────────────────


async def test_the_worker_cannot_set_or_promote_the_mode(h):
    """No proposal can carry a mode (proposals are `extra="forbid"`), a mode in
    a tool's arguments means nothing to the supervisor, and a draft stays a
    draft to the end."""

    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    h.model.push(
        ask("app.interact", scope=PKG),
        call("ui.app", "tap", args={"id": "x"}, platform="android", mode="execute"),   # extra field
        ui("tap", mode="execute", id="x"),                                             # in arguments
        '{"type": "final_answer", "content": "done", "mode": "execute"}',             # extra field
        final("done"),
    )

    body = completed(await submit(h, alice, "draft"))

    assert body["mode"] == "draft"
    assert h.ui.calls == []
    [row] = await h.rows(AgentTask)
    assert row.mode == "draft"
    assert "not a valid proposal" in h.model.all_text()


async def test_an_approval_is_re_checked_against_the_mode(h):
    """Defence in depth: an action paused in an execute task is re-checked
    against the task's mode when approved. Modes never change — this forces
    one to prove the check is there."""

    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    h.model.push(ask("app.interact", scope=PKG), ui("input_text", text="hi"), final("ok"))
    paused = pending_of(await submit(h, alice, "execute"))
    h.runtime.states.get(uuid.UUID(paused["task_id"])).mode = TaskMode.DRAFT

    resp = await h.confirm(alice, paused["task_id"], paused["confirmation_token"])

    assert resp.status_code == 200, resp.text
    assert h.ui.calls == []
    assert "not permitted in draft mode" in h.model.all_text()


async def test_the_worker_is_told_its_mode(h):
    """Guidance only — enforcement is the ceiling — but a worker told it is
    drafting wastes fewer steps on refused actions."""

    alice = await h.user("alice")
    h.model.push(final("ok"))
    completed(await submit(h, alice, "draft"))
    h.model.push(final("ok"))
    completed(await submit(h, alice, "execute"))

    draft_prompt, execute_prompt = h.model.seen[0][0].content, h.model.seen[1][0].content
    assert "TASK MODE: draft" in draft_prompt
    assert "TASK MODE" not in execute_prompt


# ── the ceiling table ──────────────────────────────────────────────────────


async def test_the_ceiling_table():
    assert MODE_CEILING == {
        TaskMode.EXECUTE: None,
        TaskMode.DRAFT: RiskCategory.LOW_READ,
        TaskMode.SUGGEST: RiskCategory.LOW_READ,
        TaskMode.OBSERVE: RiskCategory.LOW_READ,
    }
    for tier in RiskCategory:
        assert within_ceiling(TaskMode.EXECUTE, tier)
        assert within_ceiling(TaskMode.DRAFT, tier) is (tier is RiskCategory.LOW_READ)
    assert within_ceiling(TaskMode.EXECUTE, None)       # execute adds no ceiling
    assert not within_ceiling(TaskMode.OBSERVE, None)   # an unknown tier is refused
    assert capability_usable(TaskMode.DRAFT, {"a": RiskCategory.LOW_WRITE, "b": RiskCategory.LOW_READ})
    assert not capability_usable(TaskMode.DRAFT, {"a": RiskCategory.LOW_WRITE})
    assert not capability_usable(TaskMode.DRAFT, {})
