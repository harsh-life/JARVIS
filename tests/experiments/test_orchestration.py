"""The JARVIS-owned plan validator, the dispatcher, specialist isolation and
the proposal-only agentic-runtime contract — all with scripted backends."""

from __future__ import annotations

import asyncio
import json

import pytest

from experiments.contracts import CompletionStatus, EvidenceItem, Plan, PlanNode
from experiments.orchestration.backends import DeterministicTemplates, ModelPlanner, ScriptedAgenticRuntime
from experiments.orchestration.dispatch import Dispatcher, run_agentic
from experiments.orchestration.ports import (
    DelegateProposal,
    EvidenceRef,
    FinishProposal,
    ModelCallProposal,
    OrchestrationRequest,
)
from experiments.orchestration.validator import PlanLimits, PlanRejected, validate_plan
from experiments.providers import scripted_spec
from experiments.specialists import run_specialist
from shared.schemas.intelligence import ContextPolicy, ExecutionMode, StateModel
from tests.experiments.support import pool, responder_by_role, snapshot

EVIDENCE = {e.id: e for e in [
    EvidenceItem(id="E1", title="One", content="ALPHA-CONTENT the first fact."),
    EvidenceItem(id="E2", title="Two", content="BRAVO-CONTENT the second fact."),
    EvidenceItem(id="E3", title="Three", content="CHARLIE-CONTENT unrelated."),
]}


def _limits(snap, **kw):
    base = PlanLimits.from_snapshot(snap, task_id="t", evidence_ids=set(EVIDENCE), remaining_budget=1.0)
    return PlanLimits(**{**base.__dict__, **kw})


def _node(sid, spec="research", refs=("E1",), deps=()):
    return PlanNode(subtask_id=sid, specialization=spec, instruction=f"do {sid}", evidence_refs=refs, depends_on=deps)


@pytest.mark.parametrize("nodes, code", [
    ((), "empty_plan"),
    (tuple(_node(f"s{i}") for i in range(4)), "fan_out_exceeded"),
    ((_node("a"), _node("a")), "duplicate_subtask"),
    ((_node("a", spec="astrology"),), "specialization_not_assigned:astrology"),
    ((_node("a", refs=("E9",)),), "evidence_not_supplied:a"),
    ((_node("a", deps=("zzz",)),), "bad_dependency:a"),
    ((_node("a", deps=("b",)), _node("b", deps=("a",))), "cycle"),
])
def test_the_validator_refuses_with_stable_codes(nodes, code):
    with pytest.raises(PlanRejected) as exc:
        validate_plan(Plan(task_id="t", nodes=nodes), _limits(snapshot()))
    assert code in exc.value.issues


def test_wrong_task_depth_budget_and_context_policy():
    snap = snapshot(fan_out=4)
    with pytest.raises(PlanRejected, match="wrong_task"):
        validate_plan(Plan(task_id="other", nodes=(_node("a"),)), _limits(snap))
    chain = (_node("a"), _node("b", deps=("a",)), _node("c", deps=("b",)), _node("d", deps=("c",)))
    with pytest.raises(PlanRejected, match="too_deep"):
        validate_plan(Plan(task_id="t", nodes=chain), _limits(snap))
    paid = snapshot(spec_budget=0.4, per_call=0.5, overall=1.0)
    with pytest.raises(PlanRejected, match="budget_insufficient"):
        validate_plan(Plan(task_id="t", nodes=(_node("a"), _node("b"))), _limits(paid, remaining_budget=0.5))
    none_policy = snap.document.specialists[0].model_copy(update={"context_policy": ContextPolicy(mode="none")})
    limits = _limits(snap)
    limits.assignments["research"] = none_policy
    with pytest.raises(PlanRejected, match="context_not_permitted"):
        validate_plan(Plan(task_id="t", nodes=(_node("a"),)), limits)


def test_work_packages_take_limits_from_the_assignment_not_the_plan():
    snap = snapshot()
    packages = validate_plan(Plan(task_id="t", nodes=(_node("b", deps=("a",)), _node("a"))), _limits(snap))
    assert [p.subtask_id for p in packages] == ["a", "b"]           # dependency order
    a = snap.document.specialists[0]
    assert packages[0].budget.max_output_tokens == a.max_output_tokens
    assert packages[0].deadline_seconds == min(a.timeout_seconds, snap.document.execution.per_call_timeout_seconds)
    assert packages[0].output_schema == "research.v1"


async def test_a_specialist_sees_only_its_named_evidence_and_cannot_cite_beyond_it():
    snap = snapshot()
    providers = []
    reply = json.dumps({"status": "completed", "body": {
        "answer": "see https://evil.example/x", "findings": [
            {"claim": "first", "evidence_refs": ["E1"]}, {"claim": "made up", "evidence_refs": ["E3", "E7"]}]}})
    p = pool(snap, responder_by_role(specialist=reply), providers=providers)
    package = validate_plan(Plan(task_id="t", nodes=(_node("a", refs=("E1",)),)), _limits(snap))[0]
    bundle = await run_specialist(package, assignment_id="r", config_hash=snap.config_hash, evidence=EVIDENCE,
                                  model=p.get("agent.primary", max_tokens=100))
    sent = "\n".join(m.content for m in providers[0].calls[0])
    assert "ALPHA-CONTENT" in sent and "BRAVO-CONTENT" not in sent and "CHARLIE-CONTENT" not in sent
    assert bundle.status is CompletionStatus.PARTIAL
    assert bundle.evidence_refs == ("E1",) and set(bundle.unresolved_refs) == {"E3", "E7"}
    assert bundle.unverified_urls == ("https://evil.example/x",)
    assert bundle.config_hash == snap.config_hash and bundle.usage.calls == 1


async def test_schema_invalid_output_is_a_failed_bundle_not_an_exception():
    snap = snapshot()
    p = pool(snap, responder_by_role(specialist='{"status": "completed", "body": {"steps": "nope"}}'))
    package = validate_plan(Plan(task_id="t", nodes=(_node("a", spec="planning"),)), _limits(snap))[0]
    bundle = await run_specialist(package, assignment_id="p", config_hash="h", evidence=EVIDENCE,
                                  model=p.get("agent.primary", max_tokens=100))
    assert bundle.status is CompletionStatus.FAILED and bundle.error.code == "schema_invalid"


async def test_a_failed_dependency_skips_its_dependents_and_parallel_runs_concurrently():
    snap = snapshot(mode="parallel")
    seen = []

    def specialist(messages):
        text = "\n".join(m.content for m in messages)
        seen.append(messages[1].content)
        if "do bad" in text:
            return "not json at all"
        return json.dumps({"status": "completed", "body": {"answer": "fine", "findings": []}})

    p = pool(snap, responder_by_role(specialist=specialist))
    nodes = (_node("bad"), _node("ok1"), _node("ok2"), _node("after", deps=("bad",)))
    packages = validate_plan(Plan(task_id="t", nodes=nodes), _limits(snap, max_fan_out=4))
    d = Dispatcher(assignments={s.specialization: s for s in snap.document.specialists},
                   model_for=lambda a: p.get(a.catalog_id, max_tokens=100), evidence=EVIDENCE,
                   config_hash=snap.config_hash, mode=ExecutionMode.PARALLEL)
    bundles = {b.subtask_id: b for b in await d.run(packages, deadline=10)}
    assert bundles["bad"].status is CompletionStatus.FAILED
    assert bundles["after"].status is CompletionStatus.SKIPPED
    assert bundles["ok1"].status is CompletionStatus.COMPLETED and bundles["ok2"].status is CompletionStatus.COMPLETED
    assert not any("do after" in s for s in seen)


async def test_the_overall_deadline_cancels_what_is_still_running():
    snap = snapshot()

    never = asyncio.Event()

    class Hang:
        def __init__(self, spec):
            self.spec = spec

        async def invoke(self, messages, *, timeout):
            await never.wait()

        async def health(self):
            return True

    p = pool(snap, None)
    p.scripted = lambda resolved, max_tokens: Hang(scripted_spec(resolved, max_tokens))
    packages = validate_plan(Plan(task_id="t", nodes=(_node("a"), _node("b", deps=("a",)))), _limits(snap))
    d = Dispatcher(assignments={s.specialization: s for s in snap.document.specialists},
                   model_for=lambda a: p.get(a.catalog_id, max_tokens=100), evidence=EVIDENCE,
                   config_hash="h", mode=ExecutionMode.SEQUENTIAL)
    bundles = await d.run(packages, deadline=0.05)
    assert [b.status for b in bundles] == [CompletionStatus.CANCELLED, CompletionStatus.CANCELLED]


async def test_deterministic_templates_and_model_planner_return_plans_the_validator_checks():
    snap = snapshot(adapter="model_planner")
    request = OrchestrationRequest(task_id="t", prompt="p", evidence=(EvidenceRef("E1", "One"),),
                                   allowed_specializations=("research",), max_fan_out=3, deadline_seconds=5,
                                   config_hash=snap.config_hash, template=(_node("a"),))
    assert (await DeterministicTemplates().plan(request)).plan.nodes == (_node("a"),)
    providers = []
    plan_reply = json.dumps({"rationale": "r", "nodes": [
        {"subtask_id": "x", "specialization": "research", "instruction": "i", "evidence_refs": ["E1"]}]})
    p = pool(snap, responder_by_role(planner=plan_reply), providers=providers)
    planner = ModelPlanner(p.get("agent.primary", max_tokens=200))
    assert planner.validate_config(snap) == []
    outcome = await planner.plan(request)
    assert [n.subtask_id for n in outcome.plan.nodes] == ["x"]
    sent = "\n".join(m.content for m in providers[0].calls[0])
    assert "One" in sent and "ALPHA-CONTENT" not in sent        # the planner sees titles, not content
    assert p.ledger.records[0].role == "planner"
    bad = pool(snap, responder_by_role(planner="I think you should research it."))
    assert (await ModelPlanner(bad.get("agent.primary", max_tokens=200)).plan(request)).error == "unparseable_plan"


async def test_an_agentic_runtime_only_proposes_and_jarvis_validates_meters_and_dispatches():
    snap = snapshot(adapter="scripted_agentic_runtime", fan_out=3)
    p = pool(snap, responder_by_role(
        specialist=json.dumps({"status": "completed", "body": {"answer": "done", "findings": []}}),
        central="internal reasoning text"))
    runtime = ScriptedAgenticRuntime(script=[
        ModelCallProposal(prompt="think about it"),
        DelegateProposal(nodes=(_node("a"), _node("b", spec="assessment", refs=("E2",)))),
        DelegateProposal(nodes=(_node("c"), _node("d"))),                       # 2 + 2 > fan-out 3
        DelegateProposal(nodes=(_node("e", spec="planning", refs=("E9",)),)),   # evidence not supplied
        FinishProposal(summary="coordinated"),
    ])
    assert runtime.validate_config(snap) == []
    request = OrchestrationRequest(task_id="t", prompt="p", evidence=(EvidenceRef("E1", "One"), EvidenceRef("E2", "Two")),
                                   allowed_specializations=("research", "assessment", "planning"), max_fan_out=3,
                                   deadline_seconds=5, config_hash=snap.config_hash)
    d = Dispatcher(assignments={s.specialization: s for s in snap.document.specialists},
                   model_for=lambda a: p.get(a.catalog_id, max_tokens=100), evidence=EVIDENCE,
                   config_hash=snap.config_hash, mode=ExecutionMode.SEQUENTIAL)
    session = await runtime.start(request)
    outcome = await run_agentic(session, request, limits=_limits(snap), dispatcher=d,
                                orchestration_model=p.get("agent.primary", max_tokens=100))
    assert outcome.summary == "coordinated"
    assert sorted(b.subtask_id for b in outcome.bundles) == ["a", "b"]
    assert "fan_out_exceeded" in outcome.refused and "evidence_not_supplied:e" in outcome.refused
    roles = [r.role for r in p.ledger.records]
    assert roles[0] == "orchestrator" and roles.count("orchestrator") == 1
    assert sum(r.startswith("specialist:") for r in roles) == 2
    # The runtime saw results and refusals — never evidence content.
    observed = repr(runtime.observed)
    assert "ALPHA-CONTENT" not in observed and "BRAVO-CONTENT" not in observed
    assert runtime.observed[1].model_text == "internal reasoning text"


async def test_a_runtime_that_never_finishes_is_cancelled():
    snap = snapshot(adapter="scripted_agentic_runtime")
    runtime = ScriptedAgenticRuntime(script=[ModelCallProposal(prompt="again")] * 10)
    p = pool(snap, responder_by_role(central="hmm"))
    request = OrchestrationRequest(task_id="t", prompt="p", evidence=(), allowed_specializations=("research",),
                                   max_fan_out=3, deadline_seconds=5, config_hash=snap.config_hash)
    d = Dispatcher(assignments={}, model_for=lambda a: None, evidence={}, config_hash="h",
                   mode=ExecutionMode.SEQUENTIAL)
    outcome = await run_agentic(await runtime.start(request), request, limits=_limits(snap), dispatcher=d,
                                orchestration_model=p.get("agent.primary", max_tokens=50), max_turns=3)
    assert outcome.cancelled and runtime.cancelled and outcome.turns == 3


def test_external_persistent_state_is_refused_by_every_backend():
    snap = snapshot()
    persistent = snap.model_copy(update={"backend": snap.backend.model_copy(update={"state_model": StateModel.EXTERNAL_PERSISTENT})})
    assert "external_state_not_permitted" in DeterministicTemplates().validate_config(persistent)
