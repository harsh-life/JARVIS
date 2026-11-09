"""Helpers for the experiment-harness tests: an in-memory snapshot and
scripted providers. No network; no server state."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from experiments.accounting import BudgetLedger, CallLedger
from experiments.providers import ModelPool, ScriptedProvider, scripted_spec
from shared.schemas.intelligence import (
    ExperimentSnapshot,
    IntegrationStatus,
    IntelligenceProfileDocument,
    ResolvedBackend,
    ResolvedModel,
    profile_hash,
)

LOCAL = ResolvedModel(catalog_id="agent.primary", provider="ollama", model="scripted-local", local=True,
                      input_per_1k_tokens=0, output_per_1k_tokens=0, timeout_seconds=30)
PAID = ResolvedModel(catalog_id="workbench.paid", provider="openai_compatible", model="scripted-paid", local=False,
                     input_per_1k_tokens=1.0, output_per_1k_tokens=2.0, timeout_seconds=30)

BACKENDS = {
    "deterministic_templates": ("workflow_engine", "fixed_template", "stateless"),
    "model_planner": ("planner", "plan_only", "stateless"),
    "scripted_agentic_runtime": ("agentic_runtime", "jarvis_mediated", "run_scoped"),
}


def snapshot(*, adapter="deterministic_templates", central="central.contract", mode="sequential",
             specialists=("research", "assessment", "planning"), catalog="agent.primary", per_call=0.0,
             overall=0.0, fan_out=3, spec_budget=0.0) -> ExperimentSnapshot:
    kind, delegation, state = BACKENDS[adapter]
    doc = IntelligenceProfileDocument.model_validate({
        "central": {"catalog_id": catalog, "prompt_profile": central},
        "orchestration": {"backend_id": "b", "max_fan_out": fan_out,
                          "model_catalog_id": catalog if adapter != "deterministic_templates" else None},
        "specialists": [{"specialization": s, "catalog_id": catalog, "prompt_profile": f"{s}.default",
                         "output_schema": f"{s}.v1", "budget": spec_budget} for s in specialists],
        "execution": {"mode": mode, "per_call_budget": per_call, "overall_budget": overall},
    })
    return ExperimentSnapshot(
        profile_id="test", profile_version=1, config_hash=profile_hash(doc), document=doc,
        models={m.catalog_id: m for m in (LOCAL, PAID)},
        backend=ResolvedBackend(backend_id="b", kind=kind, adapter=adapter,
                                integration=IntegrationStatus.INTEGRATED, delegation_mode=delegation,
                                state_model=state, supports_parallel=True, max_fan_out=4, max_timeout_seconds=60,
                                config_version="1"),
        exported_at=datetime.now(timezone.utc),
    )


def responder_by_role(*, central=None, specialist=None, planner=None, baseline=None):
    """Route a scripted reply by which prompt is being answered."""

    def respond(messages):
        system = messages[0].content
        if "planning component of an assistant" in system:
            return baseline(messages) if callable(baseline) else baseline
        if "plan work for a team" in system:
            return planner(messages) if callable(planner) else planner
        if "specialist inside a larger assistant" in system:
            return specialist(messages) if callable(specialist) else specialist
        return central(messages) if callable(central) else central

    return respond


def scripted_factory(respond, providers: list | None = None):
    def make(resolved, max_tokens):
        p = ScriptedProvider(scripted_spec(resolved, max_tokens), respond)
        if providers is not None:
            providers.append(p)
        return p

    return make


def pool(snap: ExperimentSnapshot, respond, *, providers=None, guard=None) -> ModelPool:
    return ModelPool(snapshot=snap, ledger=CallLedger(),
                     budget=BudgetLedger(overall=snap.document.execution.overall_budget,
                                         per_call=snap.document.execution.per_call_budget),
                     scripted=scripted_factory(respond, providers), prompt_guard=guard)


def central_json(answer="ok", **kw) -> str:
    return json.dumps({"answer": answer, **kw})


class SlowProvider:
    """Counts concurrent invocations; sleeps so calls overlap."""

    def __init__(self, spec, delay=0.05, reply="x" * 40):
        self.spec = spec
        self.delay = delay
        self.reply = reply
        self.invocations = 0
        self.max_concurrent = 0
        self._live = 0

    async def invoke(self, messages, *, timeout):
        from server.models.provider import ModelResult

        self.invocations += 1
        self._live += 1
        self.max_concurrent = max(self.max_concurrent, self._live)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self._live -= 1
        return ModelResult(content=self.reply, prompt_tokens=10, completion_tokens=10)

    async def health(self):
        return True
