"""Every call metered individually; parallel calls reserve before dispatch."""

from __future__ import annotations

import asyncio

import pytest

from experiments.accounting import BudgetLedger, BudgetRefused, CallLedger, MeteredModel
from experiments.providers import scripted_spec
from experiments.trace import SecretInPrompt, SecretScanner
from server.models.provider import ChatMessage, ModelPricing
from tests.experiments.support import PAID, SlowProvider

PRICING = ModelPricing(input_per_1k_tokens=1.0, output_per_1k_tokens=2.0)
MSG = [ChatMessage("user", "x" * 396)]   # → 100 projected prompt tokens


def _model(budget: BudgetLedger, provider=None, guard=None) -> tuple[MeteredModel, SlowProvider, CallLedger]:
    provider = provider or SlowProvider(scripted_spec(PAID, 100))
    ledger = CallLedger()
    return MeteredModel(catalog_id=PAID.catalog_id, provider=provider, pricing=PRICING, ledger=ledger,
                        budget=budget, prompt_guard=guard), provider, ledger


async def test_parallel_calls_cannot_overspend_a_budget_only_some_of_them_fit():
    # Each call projects 0.1 + 0.2 = 0.3; the budget fits exactly two.
    budget = BudgetLedger(overall=0.6, per_call=0.5)
    model, provider, ledger = _model(budget)
    results = await asyncio.gather(*(model.call(MSG, task_id="t", role=f"specialist:{i}", max_tokens=100,
                                                timeout=5) for i in range(5)), return_exceptions=True)
    assert sum(not isinstance(r, Exception) for r in results) == 2
    assert sum(isinstance(r, BudgetRefused) for r in results) == 3
    assert provider.invocations == 2          # refused calls never reached the provider
    assert provider.max_concurrent == 2       # the two that fit really did run in parallel
    assert [r.status for r in ledger.records].count("budget_refused") == 3
    assert budget.spent <= budget.overall and not budget.overspent and budget.reserved == pytest.approx(0)


async def test_every_call_is_its_own_record_with_its_own_cost():
    budget = BudgetLedger(overall=10, per_call=1)
    model, _, ledger = _model(budget)
    for i in range(3):
        await model.call(MSG, task_id="t", role="central", max_tokens=100, timeout=5)
    assert len(ledger.records) == 3 and {r.call_id for r in ledger.records} == {1, 2, 3}
    assert all(r.status == "ok" and r.cost == pytest.approx(0.03) for r in ledger.records)  # 10 in + 10 out
    assert budget.spent == pytest.approx(0.09)


async def test_an_assignment_cap_and_the_per_call_cap_refuse_before_calling():
    budget = BudgetLedger(overall=10, per_call=0.2)
    model, provider, _ = _model(budget)
    with pytest.raises(BudgetRefused):
        await model.call(MSG, task_id="t", role="central", max_tokens=100, timeout=5)        # 0.3 > per call
    budget.per_call = 1
    with pytest.raises(BudgetRefused):
        await model.call(MSG, task_id="t", role="s", max_tokens=100, timeout=5, max_cost=0.1)  # 0.3 > cap
    assert provider.invocations == 0


async def test_a_timeout_is_recorded_and_releases_its_reservation():
    budget = BudgetLedger(overall=1, per_call=1)
    model, provider, ledger = _model(budget, provider=SlowProvider(scripted_spec(PAID, 100), delay=1.0))
    with pytest.raises(asyncio.TimeoutError):
        await model.call(MSG, task_id="t", role="central", max_tokens=100, timeout=0.05)
    assert ledger.records[-1].status == "timed_out"
    assert budget.reserved == pytest.approx(0) and budget.spent == 0


# A key-shaped dummy built at runtime, so no secret-shaped literal sits in a
# tracked file (tests/foundation/test_repo_secret_scan.py) while the harness's
# own scanner still sees exactly the `sk-…` shape it must block and redact.
DUMMY_KEY = "sk" + "-" + "abcdefghijklmnopqrstuv"


async def test_a_prompt_with_a_secret_is_never_sent():
    scanner = SecretScanner(["TEST-ONLY-known-value-123"])
    budget = BudgetLedger(overall=1, per_call=1)
    model, provider, ledger = _model(budget, guard=scanner.check_prompt)
    for leaked in (f"key {DUMMY_KEY}", "TEST-ONLY-known-value-123", "secretstore:secretstore:abc"):
        with pytest.raises(SecretInPrompt):
            await model.call([ChatMessage("user", leaked)], task_id="t", role="central", max_tokens=10, timeout=1)
    assert provider.invocations == 0
    assert [r.status for r in ledger.records] == ["blocked"] * 3
    assert scanner.redact(f"x {DUMMY_KEY} y") == "x [REDACTED] y"
