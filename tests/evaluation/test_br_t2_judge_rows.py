"""BR-T2, the retired Judge's improvement path (docs/OD_A1_BR_T2.md §3e) — re-measured after retirement.

HISTORICAL FINDING (preserved; the vulnerable path no longer exists)
--------------------------------------------------------------------
Until the retirement (last present at a30e34b, `tests/evaluation/test_br_t2_judge_rows.py`
in that commit), this module measured the LLM Judge's improvement path on the
production composition root with a scripted Judge whose suggestion copied the
user's private details — as a real model summarising a trace can. The measured
result was:

| # | Attempt | Model | Result (pre-retirement) |
|---|---|---|---|
| 38 | A's private content reaches B's worker prompt through a Judge candidate the operator approved from the redacted console | authorized | **REACHABLE** |
| 39 | The same candidate reaches B while it waits in the queue | authorized | contained |
| 40 | A secret-shaped value in a candidate reaches the queue or B | authorized | contained |
| 41 | A's private content in the console's default views | authorized | contained |

Row 38 was reachable without any compromised process: an approved
`worker.system_prompt` candidate applied to **every** user's worker prompt, and
approval screened only target, range and secret shapes — never a user's
private, non-secret content. That finding is why the Judge and the global
improvement mechanism were retired rather than patched (docs/DECISION_REGISTER.md,
D-JDG-RETIRE). The original assertion `assert reached` is kept in git history and
quoted in docs/OD_A1_BR_T2.md §3e; it is not re-run because the code it
exercised is gone.

RE-MEASUREMENT (this module)
----------------------------
The same attacker model (authorized: ordinary users plus the operator acting
through every route that still exists) and the same private string, on the
production composition root with deterministic evaluation **enabled**. Every
row is asserted contained; a row that becomes reachable means a learning or
global-tuning path has come back — re-run BR-T2 and update the documents rather
than the assertion.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from server.storage.models import ConfigVersion, ImprovementCandidateRow, TaskEvaluation
from tests.evaluation.conftest import SU, drain
from tests.runtime.conftest import ask, call, final

pytestmark = pytest.mark.asyncio

CONTROL = "/api/v1/admin/control/evaluation"
PRIVATE = "Alice's passport is K7Q4471 and she lives at 12 Juniper Lane"  # disposable test data


async def _alice_task(h):
    alice, bob = await h.user("alice"), await h.user("bob")
    await h.grant(alice, "file.read")
    h.model.push(ask("file.read"), call("files.read", "list_directory"), final(f"noted: {PRIVATE}"))
    assert (await h.submit(alice, f"remember for my trip: {PRIVATE}")).status_code == 200
    await drain(h)
    return alice, bob


async def _bobs_system_prompts(h, bob) -> list[str]:
    seen = len(h.model.seen)
    h.model.push(final("hello bob"))
    assert (await h.submit(bob, "what's the weather")).status_code == 200
    return [messages[0].content for messages in h.model.seen[seen:]]


async def test_br_t2_judge_rows_after_retirement(evaluated):
    rows = []
    h = await evaluated()
    alice, bob = await _alice_task(h)

    # 38R — the whole former path: nothing writes a candidate, nothing can approve one,
    # and even a pre-retirement approved `worker.system_prompt` carrying A's content
    # (planted directly, as historical data would be) never reaches B.
    candidates = await h.rows(ImprovementCandidateRow)
    approve = await h.client.post(f"{CONTROL}/candidates/{uuid.uuid4()}/approve",
                                  json={"reason": "looks_useful"}, headers=SU)
    async with h.storage.session() as s:
        s.add(ConfigVersion(target_key="worker.system_prompt", value=f"When planning travel, note: {PRIVATE}.",
                            action="approve", reason="historical", created_at=datetime.now(timezone.utc),
                            created_by="pre-retirement"))
        await s.commit()
    reached = any(PRIVATE in p for p in await _bobs_system_prompts(h, bob))
    rows.append(("38R", "A's private content reaches B's worker prompt through the (retired) improvement path",
                 reached or bool(candidates) or approve.status_code not in (404, 405),
                 "no evaluator writes candidates; no approval route; historical config versions are never read"))
    assert candidates == [] and approve.status_code in (404, 405), approve.text
    assert not reached, "row 38R changed: a global tuning path is back — re-run BR-T2, update §3e"

    # 38R-b — the deterministic record itself carries no user text another user could be shown.
    [evaluation] = await h.rows(TaskEvaluation)
    in_record = PRIVATE in repr(evaluation.findings) or PRIVATE in repr(evaluation.redactions)
    rows.append(("38R-b", "A's private content is copied into the evaluation record", in_record,
                 "findings are step refs and failure categories only; the trace is not persisted"))
    assert not in_record and evaluation.owner_user_id == alice.user_id

    # 39R / 40R — there is no queue for a suggestion to wait in.
    rows.append(("39R/40R", "a suggestion (plain or secret-shaped) is queued from A's task",
                 bool(await h.rows(ImprovementCandidateRow)), "no candidate writer exists"))
    assert await h.rows(ImprovementCandidateRow) == []

    # 41R — the console's default views.
    views = [await h.client.get(f"/api/v1/admin/{v}", headers=SU) for v in ("evaluations", "tasks", "audit")]
    leaked = any(PRIVATE in v.text for v in views)
    rows.append(("41R", "A's private content appears in the operator console's default views", leaked,
                 "user content redacted to its length (DSH-B1); retired candidates shown as counts only"))
    assert all(v.status_code == 200 for v in views), [v.text for v in views]
    assert not leaked

    print("\nBR-T2 retired-Judge dimension, re-measured (docs/OD_A1_BR_T2.md §3e):")
    for n, attempt, reachable, why in rows:
        print(f"  [{'REACHABLE' if reachable else 'contained'}] {n} (authorized) {attempt} — {why}")
