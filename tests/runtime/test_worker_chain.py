"""The worker chain and supervisory recovery — build unit U5
(`docs/18_SUPERVISORY_RUNTIME_RECOVERY_CONTROL.md` §4, §8).

Workers are scripted `ModelProvider`s wired into the real composition root
through operator configuration (`agent.recovery.chain`). Everything else is
production: the parser, the authorization engine, confirmation tokens, the
usage ledger, the audit trail.

| Requirement | Test |
|---|---|
| primary / fallback / 3+ chain | `test_the_primary_answers_when_it_can`, `test_an_unavailable_worker_is_replaced`, `test_a_three_worker_chain_switches_in_order_and_stays_switched` |
| malformed → switch | `test_malformed_output_switches_worker_and_is_not_shown_to_the_next` |
| stall / loop → switch | `test_a_no_progress_stall_switches_worker`, `test_a_repeated_identical_operation_switches_worker` |
| unresolved | `test_unresolved_escalates_when_configured`, `test_unresolved_without_escalation_is_reported_honestly`, `test_unresolved_with_no_worker_left_fails_honestly`, `test_unresolved_never_escalates_to_a_paid_worker` |
| ambiguity is not failure | `test_a_clarifying_question_is_an_answer_not_a_failure` |
| context preserved | `test_a_switch_preserves_principal_task_and_activations` |
| tokens task-bound | `test_a_confirmation_is_bound_to_the_task_not_the_worker` |
| mode / authority | `test_a_replacement_worker_cannot_change_the_mode`, `test_a_replacement_worker_gets_no_authority_of_its_own` |
| audit + metering | `test_every_attempt_is_metered_and_every_switch_audited` |
| exhaustion | `test_exhaustion_fails_with_the_right_code` |
| no config = today | `test_without_recovery_config_behaviour_is_unchanged` |
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from server.agent.recovery import RecoveryPolicy, operation_key
from server.security.events import AuditAction
from server.storage.models import AgentTask, AuditEvent, ConfirmationToken, UsageEvent
from shared.schemas.enums import AuditActor, UsageKind, Visibility
from tests.runtime.conftest import ModelUnavailable, ScriptedModel, ask, call, failure_of, final, pending_of, say

pytestmark = pytest.mark.asyncio

PKG = {"package_name": "com.example"}


def DOWN() -> ModelUnavailable:  # noqa: N802 — reads as a script entry
    return ModelUnavailable("down")


def unresolved(text: str = "I could not work this out") -> str:
    return say({"type": "final_answer", "content": text, "unresolved": True})


async def chain(make_harness, *names: str, paid: tuple[str, ...] = (), recovery: dict | None = None,
                config: dict | None = None):
    """A harness whose worker chain is [primary, *names]."""

    workers = {n: ScriptedModel(n) for n in names}
    entries = []
    for n in names:
        entry = {"provider": "ollama", "model": n}
        if n in paid:
            entry["pricing"] = {"input_per_1k_tokens": 0.1, "output_per_1k_tokens": 0.1}
        entries.append(entry)
    cfg = {"agent": {"recovery": {"chain": entries, **(recovery or {})}}}
    for key, value in (config or {}).items():
        cfg.setdefault(key, {}).update(value) if isinstance(value, dict) else cfg.update({key: value})
    h = await make_harness(config=cfg, models=workers)
    return h, workers


def ok(resp) -> dict:
    assert resp.status_code == 200, resp.text
    return resp.json()


async def audit(h, action: AuditAction) -> list[AuditEvent]:
    return [r for r in await h.rows(AuditEvent) if r.action == action.value]


# ── choosing the worker ────────────────────────────────────────────────────


async def test_the_primary_answers_when_it_can(make_harness):
    h, workers = await chain(make_harness, "w2")
    alice = await h.user("alice")
    h.model.push(final("from the primary"))

    body = ok(await h.submit(alice))

    assert body["response"] == "from the primary"
    assert body["counters"]["worker_switches"] == 0
    assert workers["w2"].seen == []


async def test_an_unavailable_worker_is_replaced(make_harness):
    h, workers = await chain(make_harness, "w2")
    alice = await h.user("alice")
    h.model.push(DOWN())
    workers["w2"].push(final("from w2"))

    body = ok(await h.submit(alice))

    assert body["response"] == "from w2"
    assert body["counters"]["worker_switches"] == 1
    [switched] = await audit(h, AuditAction.AGENT_WORKER_SWITCHED)
    assert switched.resource.startswith(f"worker:{body['task_id']}:0>1:")
    assert switched.resource.endswith(":unavailable")
    assert switched.actor == AuditActor.SYSTEM.value


async def test_a_three_worker_chain_switches_in_order_and_stays_switched(make_harness):
    """w1 down, w2 times out, w3 answers — and keeps answering: a switch is
    for the rest of the task, not one step."""

    h, workers = await chain(make_harness, "w2", "w3")
    alice = await h.user("alice")
    await h.grant(alice, "file.read")
    mine = await h.file(alice, None, Visibility.PRIVATE, "mine.txt")
    h.model.push(DOWN())
    workers["w2"].push(asyncio.TimeoutError())
    workers["w3"].push(ask("file.read"), call("files.read", "read_file", ref=mine), final("from w3"))

    body = ok(await h.submit(alice))

    assert body["response"] == "from w3"
    assert body["counters"]["worker_switches"] == 2
    assert (len(h.model.seen), len(workers["w2"].seen), len(workers["w3"].seen)) == (1, 1, 3)
    assert [c.resource_ref for c in h.reads.calls] == [mine]
    [row] = await h.rows(AgentTask)
    assert row.worker_switches == 2


async def test_malformed_output_switches_worker_and_is_not_shown_to_the_next(make_harness):
    h, workers = await chain(make_harness, "w2")
    alice = await h.user("alice")
    h.model.push("GARBAGE-ONE", "GARBAGE-TWO", "GARBAGE-THREE")  # max_parse_retries (2) + 1
    workers["w2"].push(final("from w2"))

    body = ok(await h.submit(alice))

    assert body["response"] == "from w2"
    assert body["counters"]["worker_switches"] == 1
    seen_by_w2 = "\n".join(m.content for m in workers["w2"].seen[0])
    assert "GARBAGE" not in seen_by_w2 and "not a valid proposal" not in seen_by_w2
    assert (await audit(h, AuditAction.AGENT_WORKER_SWITCHED))[0].resource.endswith(":malformed")


async def test_a_no_progress_stall_switches_worker(make_harness):
    h, workers = await chain(make_harness, "w2", recovery={"stall_window": 2})
    alice = await h.user("alice")
    h.model.push(call("no.such_tool", "x"), call("no.such_tool", "y"), final("never"))
    workers["w2"].push(final("from w2"))

    body = ok(await h.submit(alice))

    assert body["response"] == "from w2"
    [stall] = await audit(h, AuditAction.AGENT_STALL_DETECTED)
    assert stall.resource == f"stall:{body['task_id']}:no_progress"
    assert len(h.model.script) == 1  # the stalled worker was not asked again


async def test_a_repeated_identical_operation_switches_worker(make_harness):
    """The third identical proposal is not executed: the supervisor switches
    worker instead. Argument order does not disguise a repeat."""

    h, workers = await chain(make_harness, "w2", recovery={"loop_repeat_limit": 3, "stall_window": 10})
    alice = await h.user("alice")
    await h.grant(alice, "file.read")
    mine = await h.file(alice, None, Visibility.PRIVATE, "mine.txt")
    same = call("files.read", "read_file", ref=mine)
    h.model.push(ask("file.read"), same, same, same, final("never"))
    workers["w2"].push(final("from w2"))

    body = ok(await h.submit(alice))

    assert body["response"] == "from w2"
    assert len(h.reads.calls) == 2
    [stall] = await audit(h, AuditAction.AGENT_STALL_DETECTED)
    assert stall.resource.endswith(":loop")
    assert operation_key("t", "o", "server", {"a": 1, "b": 2}, None, None) == \
        operation_key("t", "o", "server", {"b": 2, "a": 1}, None, None)


async def test_a_new_worker_starts_with_fresh_detectors(make_harness):
    """Detectors are per worker: the replacement may issue the operation its
    predecessor looped on — once is not a loop."""

    h, workers = await chain(make_harness, "w2", recovery={"loop_repeat_limit": 3, "stall_window": 10})
    alice = await h.user("alice")
    await h.grant(alice, "file.read")
    mine = await h.file(alice, None, Visibility.PRIVATE, "mine.txt")
    same = call("files.read", "read_file", ref=mine)
    h.model.push(ask("file.read"), same, same, same)
    workers["w2"].push(same, final("from w2"))

    body = ok(await h.submit(alice))

    assert body["response"] == "from w2"
    assert len(h.reads.calls) == 3  # two from the looping worker, one from its replacement
    assert body["counters"]["worker_switches"] == 1


# ── unresolved ─────────────────────────────────────────────────────────────


async def test_unresolved_escalates_when_configured(make_harness):
    h, workers = await chain(make_harness, "w2", recovery={"escalate_on_unresolved": True})
    alice = await h.user("alice")
    h.model.push(unresolved("PRIMARY-GAVE-UP"))
    workers["w2"].push(final("resolved by w2"))

    body = ok(await h.submit(alice))

    assert (body["response"], body["unresolved"]) == ("resolved by w2", False)
    assert "PRIMARY-GAVE-UP" not in "\n".join(m.content for m in workers["w2"].seen[0])
    assert (await audit(h, AuditAction.AGENT_WORKER_SWITCHED))[0].resource.endswith(":unresolved")


async def test_unresolved_without_escalation_is_reported_honestly(make_harness):
    h, workers = await chain(make_harness, "w2")
    alice = await h.user("alice")
    h.model.push(unresolved("I could not find that"))

    body = ok(await h.submit(alice))

    assert body["status"] == "completed"
    assert (body["response"], body["unresolved"]) == ("I could not find that", True)
    assert any("could not resolve" in n for n in body["notes"])
    assert workers["w2"].seen == []


async def test_unresolved_with_no_worker_left_fails_honestly(make_harness):
    h, workers = await chain(make_harness, "w2", recovery={"escalate_on_unresolved": True})
    alice = await h.user("alice")
    h.model.push(unresolved())
    workers["w2"].push(unresolved())

    resp = await h.submit(alice)

    assert resp.status_code == 503
    assert failure_of(resp) == "worker_chain_exhausted"
    assert len(await audit(h, AuditAction.AGENT_RECOVERY_EXHAUSTED)) == 1


async def test_unresolved_never_escalates_to_a_paid_worker(make_harness):
    """OD-SUP-3, default no: an unresolved answer is not a reason to spend."""

    h, workers = await chain(make_harness, "paid", paid=("paid",), recovery={"escalate_on_unresolved": True},
                             config={"agent": {"bounds": {"per_task_budget": 5.0}},
                                     "security": {"budgets": {"per_user_daily_cost_limit": 50.0,
                                                              "global_daily_cost_limit": 500.0}}})
    alice = await h.user("alice")
    h.model.push(unresolved())

    resp = await h.submit(alice)

    assert failure_of(resp) == "worker_chain_exhausted"
    assert workers["paid"].seen == []


async def test_a_clarifying_question_is_an_answer_not_a_failure(make_harness):
    h, workers = await chain(make_harness, "w2", recovery={"escalate_on_unresolved": True})
    alice = await h.user("alice")
    h.model.push(final("Which folder do you mean — work or personal?"))

    body = ok(await h.submit(alice, "tidy my folder"))

    assert body["status"] == "completed" and body["unresolved"] is False
    assert body["counters"]["worker_switches"] == 0
    assert workers["w2"].seen == []


# ── a switch changes the worker and nothing else ───────────────────────────


async def test_a_switch_preserves_principal_task_and_activations(make_harness):
    h, workers = await chain(make_harness, "w2")
    alice = await h.user("alice")
    await h.grant(alice, "file.read")
    mine = await h.file(alice, None, Visibility.PRIVATE, "mine.txt")
    h.model.push(ask("file.read"), DOWN())
    # w2 uses the activation the primary obtained — it does not ask again.
    workers["w2"].push(call("files.read", "read_file", ref=mine), final("done"))

    body = ok(await h.submit(alice))

    assert body["active_capabilities"] == ["file.read"]
    [invocation] = h.reads.calls
    assert invocation.user_id == alice.user_id
    assert str(invocation.task_id) == body["task_id"]
    assert invocation.device_id == alice.device_id


async def test_a_confirmation_is_bound_to_the_task_not_the_worker(make_harness):
    """w1 fails; w2 proposes a consequential action; the token approves exactly
    that action for this task, whichever worker then continues."""

    h, workers = await chain(make_harness, "w2", "w3")
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    h.model.push(DOWN())
    workers["w2"].push(ask("app.interact", scope=PKG),
                       call("ui.app", "input_text", args={"text": "exact"}, platform="android"), DOWN())
    workers["w3"].push(final("sent"))

    paused = pending_of(await h.submit(alice))
    resp = await h.confirm(alice, paused["task_id"], paused["confirmation_token"])

    assert ok(resp)["response"] == "sent"
    assert [(c.operation, dict(c.arguments)) for c in h.ui.calls] == [("input_text", {"text": "exact"})]
    columns = {c.name for c in ConfirmationToken.__table__.columns}
    assert not {c for c in columns if "worker" in c or "model" in c or "provider" in c}
    [token] = await h.rows(ConfirmationToken)
    assert token.task_id == paused["task_id"] and token.used_at is not None


async def test_a_replacement_worker_cannot_change_the_mode(make_harness):
    h, workers = await chain(make_harness, "w2")
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    h.model.push(DOWN())
    workers["w2"].push(ask("app.interact", scope=PKG),
                       '{"type": "tool_call", "tool": "ui.app", "operation": "tap", "arguments": {},'
                       ' "platform": "android", "mode": "execute"}',
                       call("ui.app", "tap", args={"id": "x"}, platform="android"), final("draft"))

    body = ok(await h.submit(alice, "draft it", extra_body={"mode": "draft"}))

    assert body["mode"] == "draft"
    assert h.ui.calls == []


async def test_a_replacement_worker_gets_no_authority_of_its_own(make_harness):
    """After a switch, an ungranted capability still needs the human, a floor
    capability is still prohibited, and another user's data is still not found."""

    h, workers = await chain(make_harness, "w2")
    alice, bob = await h.user("alice"), await h.user("bob")
    await h.grant(alice, "file.read")
    bobs = await h.file(bob, None, Visibility.PRIVATE, "bobs.txt")
    h.model.push(DOWN())
    workers["w2"].push(ask("superuser"), ask("file.read"), call("files.read", "read_file", ref=bobs),
                       ask("file.write"))

    paused = pending_of(await h.submit(alice))

    assert paused["pending"]["kind"] == "capability_activation"
    assert paused["pending"]["capability"] == "file.write"   # needs the human
    assert h.reads.calls == []                               # bob's file was never read
    seen = "\n".join(m.content for call_ in workers["w2"].seen for m in call_)
    assert "superuser: prohibited" in seen
    assert "Not found or not permitted." in seen


# ── audit, metering, exhaustion ────────────────────────────────────────────


async def test_every_attempt_is_metered_and_every_switch_audited(make_harness):
    h, workers = await chain(make_harness, "w2", "w3")
    alice = await h.user("alice")
    h.model.push(DOWN())
    workers["w2"].push(DOWN())
    workers["w3"].push(final("done"))

    body = ok(await h.submit(alice))

    calls = [u for u in await h.rows(UsageEvent) if u.kind is UsageKind.MODEL_CALL]
    assert len(calls) == body["counters"]["model_calls"] == 3
    assert [(u.model, u.tokens_or_units) for u in calls][:2] == [("scripted-primary", 0), ("w2", 0)]
    assert calls[2].model == "w3" and calls[2].tokens_or_units > 0
    failed = await audit(h, AuditAction.AGENT_WORKER_FAILED)
    switched = await audit(h, AuditAction.AGENT_WORKER_SWITCHED)
    assert len(failed) == 2 and len(switched) == 2 == body["counters"]["worker_switches"]
    assert [s.resource.split(":")[2] for s in switched] == ["0>1", "1>2"]


async def test_exhaustion_fails_with_the_right_code(make_harness):
    # Every worker down: a dependency outage.
    h, workers = await chain(make_harness, "w2")
    alice = await h.user("alice")
    h.model.push(DOWN())
    workers["w2"].push(DOWN())
    resp = await h.submit(alice)
    assert (resp.status_code, failure_of(resp)) == (503, "model_unavailable")
    assert len(await audit(h, AuditAction.AGENT_RECOVERY_EXHAUSTED)) == 1

    # Every worker malformed: the chain is exhausted.
    h, workers = await chain(make_harness, "w2")
    alice = await h.user("alice")
    h.model.push("x", "y", "z")
    workers["w2"].push("x", "y", "z")
    assert failure_of(await h.submit(alice)) == "worker_chain_exhausted"

    # max_worker_switches reached with workers still left.
    h, workers = await chain(make_harness, "w2", "w3", recovery={"max_worker_switches": 1})
    alice = await h.user("alice")
    h.model.push(DOWN())
    workers["w2"].push(DOWN())
    workers["w3"].push(final("never asked"))
    assert failure_of(await h.submit(alice)) == "worker_chain_exhausted"
    assert workers["w3"].seen == []


async def test_a_single_worker_stall_fails_as_stalled(make_harness):
    h = await make_harness(config={"agent": {"recovery": {"stall_window": 2}}})
    alice = await h.user("alice")
    h.model.push(call("no.such_tool", "x"), call("no.such_tool", "y"))
    resp = await h.submit(alice)
    assert (resp.status_code, failure_of(resp)) == (503, "stalled")


async def test_without_recovery_config_behaviour_is_unchanged(make_harness):
    """No `agent.recovery`: a malformed worker fails the task as before, no
    stall or loop detection runs, and `agent.fallback` is per-step only."""

    h = await make_harness()
    alice = await h.user("alice")
    h.model.push("x", "y", "z")
    assert failure_of(await h.submit(alice)) == "unparseable_proposal"

    h.model.push(*[call("no.such_tool", str(i)) for i in range(5)], final("still fine"))
    body = ok(await h.submit(alice))
    assert body["response"] == "still fine" and body["counters"]["worker_switches"] == 0
    assert await audit(h, AuditAction.AGENT_STALL_DETECTED) == []


async def test_recovery_policy_bounds():
    RecoveryPolicy()
    for bad in ({"max_worker_switches": -1}, {"stall_window": 0}, {"loop_repeat_limit": 1}):
        with pytest.raises(ValueError):
            RecoveryPolicy(**bad)
