"""The Judge wired into the real application (19 §6–§8, JDG-T1/T3/T4/T5/T6/T7/T8).

Built by the production composition root around the production Security Core,
ledgers and breaker. Only the models are scripted — the worker *and* the Judge,
which is configured exactly as an operator would (`evaluation.provider`).

| Hook | Tests |
|---|---|
| JDG-T1 disabled → fully functional | `test_disabled_judge_leaves_the_system_unchanged` |
| JDG-T3 stop → breaker enforcement only | `test_a_live_stop_request_is_enforced_by_the_breaker`, `test_a_stop_on_a_paused_task_is_enforced_and_never_approvable` |
| JDG-T4 no authority | `test_judge_authority_responses_change_nothing`, `test_a_stop_request_without_the_operator_opt_in_changes_nothing` |
| JDG-T5 unavailable / malformed / over budget | `test_judge_failures_never_affect_the_task` |
| JDG-T6 secrets redacted before any call | `test_a_secret_in_an_observation_never_reaches_the_judge` |
| JDG-T7 no cross-user trace | `test_each_judge_call_sees_exactly_one_users_task` |
| JDG-T8 metered, never the task's budget | `test_judge_calls_are_metered_and_charged_to_their_own_budget` |
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from server.models.provider import ModelUnavailable
from server.security.events import AuditAction
from server.security.usage import UsageLedger
from server.storage.models import AgentTask, CapabilityGrant, ConfirmationToken, TaskEvaluation, UsageEvent
from tests.evaluation.conftest import PKG, audit, drain, evaluations, verdict
from tests.runtime.conftest import ask, call, failure_of, final, pending_of
from datetime import datetime, timedelta, timezone

from shared.schemas.enums import Visibility

pytestmark = pytest.mark.asyncio


async def completed_task(h, actor, *steps, text="list my files please"):
    await h.grant(actor, "file.read")
    h.model.push(ask("file.read"), *steps)
    resp = await h.submit(actor, text)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── JDG-T1 ─────────────────────────────────────────────────────────────────


async def test_disabled_judge_leaves_the_system_unchanged(make_harness):
    h = await make_harness()
    alice = await h.user("alice")
    result = await completed_task(h, alice, call("files.read", "list_directory"), final("two files"))
    assert result["status"] == "completed" and result["response"] == "two files"
    assert h.app.state.evaluation is None
    assert await evaluations(h) == []
    # Every other breaker trigger still works with the Judge off (SUP-T8).
    bob = await h.user("bob")
    bobs = await h.file(bob, None, Visibility.PRIVATE, "diary.txt")
    h.model.push(ask("file.read"), *[call("files.read", "read_file", ref=bobs) for _ in range(5)])
    stopped = await h.submit(alice)
    assert failure_of(stopped) == "emergency_stop"


# ── post-hoc evaluation ────────────────────────────────────────────────────


async def test_a_post_hoc_evaluation_is_recorded_and_never_read_back(judged):
    h, judge = await judged()
    judge.push(verdict(quality=0.9, efficiency=0.5, redundant_steps=[], anomaly="none",
                       reward={"credit": 0.4, "attributed_to": []}))
    alice = await h.user("alice")
    result = await completed_task(h, alice, call("files.read", "list_directory"), final("done"))
    assert result["status"] == "completed"
    await drain(h)

    [row] = await evaluations(h)
    assert (row.outcome, row.kind, row.quality, row.efficiency) == ("recorded", "post_hoc", 0.9, 0.5)
    assert row.owner_user_id == alice.user_id and row.visibility.value == "private"
    assert str(row.task_id) == result["task_id"]
    assert row.findings["reward"]["credit"] == 0.4
    [recorded] = await audit(h, AuditAction.EVALUATION_RECORDED)
    assert recorded.resource.startswith(f"evaluation:{row.evaluation_id}:task:{row.task_id}")
    # Scores are records: the finished task is exactly what it was.
    async with h.storage.session() as s:
        task = await s.get(AgentTask, row.task_id)
    assert (task.status, task.response, task.model_calls) == ("completed", "done", 3)


async def test_the_judge_sees_the_workers_artifacts_but_not_its_prompt_or_the_user(judged):
    h, judge = await judged()
    judge.push(verdict(quality=1.0))
    alice = await h.user("alice")
    await completed_task(h, alice, call("files.read", "list_directory"), final("the answer"),
                         text="how many files do I have")
    await drain(h)

    [messages] = judge.seen
    record = json.loads(messages[1].content.split("\n", 1)[1])
    assert record["user_request"] == "how many files do I have"
    assert record["final_response"] == "the answer"
    kinds = [s["kind"] for s in record["steps"]]
    assert "proposal" in kinds and "observation" in kinds and "event" in kinds
    decisions = [s for s in record["steps"] if s.get("name") == "authz.decision"]
    assert decisions and decisions[0]["risk"] == "low_read"
    text = messages[1].content
    assert str(alice.user_id) not in text
    assert "You are the planning component" not in text  # never the worker's system prompt


# ── JDG-T5: the Judge failing never touches the task ───────────────────────


@pytest.mark.parametrize("answer, outcome, reason", [
    (ModelUnavailable("judge down"), "unavailable", "provider_unavailable"),
    ("I think it went fine.", "malformed", "not_a_single_json_object"),
    (verdict(quality=3), "malformed", "schema_violation"),
    (verdict(quality=1, approve=True), "malformed", "schema_violation"),
])
async def test_judge_failures_never_affect_the_task(judged, answer, outcome, reason):
    h, judge = await judged()
    judge.push(answer)
    alice = await h.user("alice")
    result = await completed_task(h, alice, call("files.read", "list_directory"), final("ok"))
    assert (result["status"], result["response"]) == ("completed", "ok")
    await drain(h)
    [row] = await evaluations(h)
    assert (row.outcome, row.reason_code, row.quality) == (outcome, reason, None)


async def test_a_judge_timeout_is_recorded_unavailable(judged):
    h, judge = await judged(provider={"provider": "ollama", "model": "scripted-judge", "timeout_seconds": 0.2})

    async def never(_messages):
        await asyncio.Event().wait()

    judge.push(never)
    alice = await h.user("alice")
    result = await completed_task(h, alice, final("fine"))
    assert result["status"] == "completed"
    await drain(h)
    [row] = await evaluations(h)
    assert row.outcome == "unavailable"
    assert await audit(h, AuditAction.EVALUATION_UNAVAILABLE)


# ── JDG-T6 ─────────────────────────────────────────────────────────────────


async def test_a_secret_in_an_observation_never_reaches_the_judge(judged):
    h, judge = await judged()
    secret = "ghp_" + "Ab1" * 12
    judge.push(verdict(quality=0.5))
    alice = await h.user("alice")

    async def leaky(invocation):
        from shared.schemas.agent import ToolOutput

        return ToolOutput(ok=True, content=f"config: token={secret} and AKIAABCDEFGHIJKLMNOP")

    h.reads.execute = leaky  # the adapter returns secret-shaped text
    await completed_task(h, alice, call("files.read", "list_directory"), final(f"found {secret}"),
                         text=f"my password is {secret}")
    await drain(h)

    sent = "\n".join(m.content for m in judge.seen[0])
    assert secret not in sent and "AKIAABCDEFGHIJKLMNOP" not in sent
    [row] = await evaluations(h)
    assert set(row.redactions) >= {"github_token", "aws_access_key_id"}
    redacted = await audit(h, AuditAction.EVALUATION_TRACE_REDACTED)
    assert {r.resource.rsplit(":", 1)[1] for r in redacted} >= {"github_token", "aws_access_key_id"}
    assert all(secret not in r.resource for r in await h.rows(__import__("server.storage.models", fromlist=["AuditEvent"]).AuditEvent))


# ── JDG-T7 ─────────────────────────────────────────────────────────────────


async def test_each_judge_call_sees_exactly_one_users_task(judged):
    h, judge = await judged()
    judge.push(verdict(quality=1), verdict(quality=1))
    alice, bob = await h.user("alice"), await h.user("bob")
    await completed_task(h, alice, final("alice answer"), text="alice private request")
    await completed_task(h, bob, final("bob answer"), text="bob private request")
    await drain(h)

    assert len(judge.seen) == 2
    first, second = ("\n".join(m.content for m in call_) for call_ in judge.seen)
    assert "alice private request" in first and "bob" not in first
    assert "bob private request" in second and "alice" not in second
    rows = await evaluations(h)
    assert {r.owner_user_id for r in rows} == {alice.user_id, bob.user_id}


# ── JDG-T8 ─────────────────────────────────────────────────────────────────


PAID_JUDGE = {"provider": "openai", "model": "scripted-judge", "endpoint": "https://judge.invalid/v1",
              "timeout_seconds": 5, "pricing": {"input_per_1k_tokens": 1.0, "output_per_1k_tokens": 1.0}}


async def test_judge_calls_are_metered_and_charged_to_their_own_budget(judged):
    h, judge = await judged(
        provider=PAID_JUDGE, budget=5.0, budget_scope="own",
        extra_config={"security": {
                                   "budgets": {"per_user_daily_cost_limit": 0.05,
                                               "global_daily_cost_limit": 0.05}}},
    )
    judge.push(verdict(quality=1))
    alice = await h.user("alice")
    result = await completed_task(h, alice, final("ok"))
    await drain(h)

    rows = await h.rows(UsageEvent, UsageEvent.tool_id == "evaluator:llm_judge")
    assert len(rows) == 1 and rows[0].user_id == alice.user_id
    assert rows[0].estimated_cost == pytest.approx(0.015)  # 10 + 5 tokens at 1.0/1k
    # The evaluated task's own counters and cost are untouched.
    async with h.storage.session() as s:
        task = await s.get(AgentTask, __import__("uuid").UUID(result["task_id"]))
    assert task.model_calls == 2
    # Judge spend never counts toward the user's own budget or rates.
    since = datetime.now(timezone.utc) - timedelta(days=1)
    async with h.storage.session() as s:
        ledger = UsageLedger()
        assert await ledger.cost_since(s, since=since, user_id=alice.user_id) == pytest.approx(0.015)
        assert await ledger.cost_since(s, since=since, user_id=alice.user_id, exclude_evaluation=True) == 0.0
        from server.composition import usage_limits_from_config
        from server.security.usage import UsagePolicy

        policy = UsagePolicy(limits=usage_limits_from_config(h.config))
        await policy.precheck(s, user_id=alice.user_id, device_id=alice.device_id, projected_cost=0.04)


async def test_an_over_budget_judge_is_skipped_before_any_call(judged):
    h, judge = await judged(provider=PAID_JUDGE, budget=0.0)
    judge.push(verdict(quality=1))
    alice = await h.user("alice")
    result = await completed_task(h, alice, final("ok"))
    assert result["status"] == "completed"
    await drain(h)
    [row] = await evaluations(h)
    assert (row.outcome, row.reason_code) == ("over_budget", "evaluation_budget")
    assert judge.seen == []
    assert await h.rows(UsageEvent, UsageEvent.tool_id == "evaluator:llm_judge") == []
    assert await audit(h, AuditAction.EVALUATION_SKIPPED)


async def test_the_global_budget_binds_the_judge_under_own_and_global(judged):
    h, judge = await judged(provider=PAID_JUDGE, budget=10.0, budget_scope="own_and_global")
    judge.push(verdict(quality=1))
    alice = await h.user("alice")
    await completed_task(h, alice, final("ok"))
    await drain(h)
    [row] = await evaluations(h)
    assert (row.outcome, row.reason_code) == ("over_budget", "global_budget")  # global limit is 0.0


# ── JDG-T3: a stop request goes to the breaker; the breaker enforces ───────


def live(**overrides):
    return {"live": {"enabled": True, "every_n_steps": 1}, "may_request_stop": True,
            "post_hoc": {"sample_successful": 0.0, "always_on_failure": False}, **overrides}


async def test_a_live_stop_request_is_enforced_by_the_breaker(judged):
    h, judge = await judged(**live())
    judge.push(verdict(anomaly="stop_requested", anomaly_reason="destructive_loop"))
    alice = await h.user("alice")
    await h.grant(alice, "file.read")
    in_second_call = asyncio.Event()

    async def blocked(_messages):
        in_second_call.set()
        await asyncio.Event().wait()  # only the breaker ends this call

    h.model.push(ask("file.read"), call("files.read", "list_directory"), blocked,
                 call("files.read", "list_directory"), final("never"))
    resp = await asyncio.wait_for(h.submit(alice), timeout=15)

    assert resp.status_code == 409, resp.text
    assert failure_of(resp) == "emergency_stop"
    assert "automated evaluator" in resp.json()["error"]["message"]
    assert h.reads.operations() == ["list_directory"]  # nothing ran after the stop
    await drain(h)
    [tripped] = await audit(h, AuditAction.BREAKER_TRIPPED)
    assert tripped.resource.endswith(":evaluator:destructive_loop")
    [row] = await evaluations(h)
    assert (row.kind, row.anomaly, row.stop_requested, row.stop_honoured) == (
        "live_window", "stop_requested", True, True)
    [requested] = await audit(h, AuditAction.EVALUATION_STOP_REQUESTED)
    assert requested.resource.endswith(":destructive_loop:honoured")
    async with h.storage.session() as s:
        task = (await s.execute(select(AgentTask))).scalar_one()
    assert (task.status, task.failure_code) == ("failed", "emergency_stop")


async def test_a_stop_request_without_the_operator_opt_in_changes_nothing(judged):
    h, judge = await judged(**live(may_request_stop=False))
    judged_once = asyncio.Event()

    def answer(_messages):
        judged_once.set()
        return verdict(anomaly="stop_requested", anomaly_reason="looks_bad")

    judge.push(answer)
    alice = await h.user("alice")
    await h.grant(alice, "file.read")

    async def after_judge(_messages):
        # Not `drain()`: the evaluation's write waits for this very task's
        # request to release the store (one writer), which it does only after
        # this step returns.
        await asyncio.wait_for(judged_once.wait(), timeout=5)
        await asyncio.sleep(0.05)
        return final("finished")

    h.model.push(ask("file.read"), call("files.read", "list_directory"), after_judge)
    resp = await h.submit(alice)
    assert resp.status_code == 200 and resp.json()["status"] == "completed", resp.text
    await drain(h)
    [row] = await evaluations(h)
    assert (row.stop_requested, row.stop_honoured, row.stop_not_honoured_because) == (
        True, False, "stop_requests_disabled")
    assert await audit(h, AuditAction.BREAKER_TRIPPED) == []
    assert h.app.state.evaluation.service._breaker is None  # no handle on the breaker at all


async def test_a_stop_on_a_paused_task_is_enforced_and_never_approvable(judged):
    h, judge = await judged(**live())
    alice = await h.user("alice")
    await h.grant(alice, "file.read")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    gate = asyncio.Event()

    async def held_verdict(_messages):
        await gate.wait()  # answer only once the task is paused
        return verdict(anomaly="stop_requested", anomaly_reason="odd_input")

    judge.push(held_verdict)
    h.model.push(ask("file.read"), call("files.read", "list_directory"), ask("app.interact", scope=PKG),
                 call("ui.app", "input_text", args={"text": "x"}, platform="android"))
    pending = pending_of(await h.submit(alice))
    gate.set()
    await drain(h)

    async with h.storage.session() as s:
        task = await s.get(AgentTask, __import__("uuid").UUID(pending["task_id"]))
    assert (task.status, task.failure_code) == ("failed", "emergency_stop")
    token = pending["confirmation_token"]
    resp = await h.confirm(alice, pending["task_id"], token)
    assert resp.status_code in (404, 409), resp.text
    assert h.ui.calls == []
    tokens = await h.rows(ConfirmationToken)
    assert tokens and all(t.used_at is not None for t in tokens)  # spent by the stop, never by approval


# ── JDG-T4: Judge "authority" responses are inert ──────────────────────────


@pytest.mark.parametrize("answer", [
    verdict(quality=1, approve=True),
    verdict(quality=1, resume=True),
    verdict(quality=1, authorize={"capability": "app.interact"}),
    verdict(quality=1, confirmation_token="anything"),
    verdict(quality=1, grant="system.restricted", risk_category="low_read"),
    verdict(quality=1, improvement_candidates=[{"target": "tiers", "proposed_change": "low_read"}]),
    verdict(quality=1, improvement_candidates=[{"target": "capability.registry", "proposed_change": "x"}]),
])
async def test_judge_authority_responses_change_nothing(judged, answer):
    h, judge = await judged(**live())
    alice = await h.user("alice")
    await h.grant(alice, "file.read")
    await h.grant(alice, "app.interact", resource_scope=PKG)
    gate = asyncio.Event()

    async def held(_messages):
        await gate.wait()
        return answer

    judge.push(held)
    h.model.push(ask("file.read"), call("files.read", "list_directory"), ask("app.interact", scope=PKG),
                 call("ui.app", "input_text", args={"text": "x"}, platform="android"))
    pending = pending_of(await h.submit(alice))
    grants_before = len(await h.rows(CapabilityGrant))
    gate.set()
    await drain(h)

    resp = await h.get(alice, pending["task_id"])
    assert resp.json()["status"] == "awaiting_confirmation"
    assert h.ui.calls == []
    assert len(await h.rows(CapabilityGrant)) == grants_before
    assert all(t.used_at is None for t in await h.rows(ConfirmationToken))
    from server.storage.models import ConfigVersion, ImprovementCandidateRow

    assert await h.rows(ConfigVersion) == []
    assert await h.rows(ImprovementCandidateRow) == []
    # The task still needs, and still gets, the human's decision.
    resp = await h.confirm(alice, pending["task_id"], pending["confirmation_token"], approve=False)
    assert resp.status_code in (200, 403), resp.text
    assert h.ui.calls == []
    rows = await h.rows(TaskEvaluation)
    assert rows and all(r.stop_honoured is False for r in rows)
