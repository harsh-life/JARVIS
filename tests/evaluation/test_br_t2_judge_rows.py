"""BR-T2, Stage 5 dimension — the Judge's improvement path (RG5, docs/OD_A1_BR_T2.md §3e).

The earlier BR-T2 tables predate the Judge and the console. Stage 5 adds one
data path none of them measures: a Judge candidate is written from **one
user's** task trace (19 §4, JDG-T7), and an approved `worker.system_prompt`
change is applied to **every** user's worker prompt (19 §9). Approval re-checks
the target, the range and secret-shaped text (JDG-T9); nothing checks for a
user's private, non-secret content. The console shows the candidate's text
redacted by default (DSH-B1); reading it is the separate, audited DASH-006
view, which approval does not require.

Measured on the production composition root with a scripted Judge. The Judge's
output is attacker-shaped on purpose: a real model summarising A's trace can
write A's details into its suggestion. Asserted in both directions like the
other BR-T2 modules — a REACHABLE row that stops being reachable means the
boundary changed; update the documents rather than the assertion.

Attacker models: **authorized** — ordinary users plus the operator acting
through the operator's own approval path. Anything reachable this way is not
inside OD-A1 (a)'s accepted class (a compromised process); it goes to the owner.
"""

from __future__ import annotations

import pytest

from server.storage.models import ImprovementCandidateRow
from tests.evaluation.conftest import SU, drain, verdict
from tests.runtime.conftest import ask, final

pytestmark = pytest.mark.asyncio

CONTROL = "/api/v1/admin/control/evaluation"
PRIVATE = "Alice's passport is K7Q4471 and she lives at 12 Juniper Lane"  # disposable test data


def _row(n: str, model: str, attempt: str, reachable: bool, why: str) -> tuple[str, str, str, bool, str]:
    return n, model, attempt, reachable, why


async def _alice_task_with_candidate(judged, change: str):
    h, judge = await judged()
    judge.push(verdict(quality=0.5, improvement_candidates=[
        {"target": "worker.system_prompt", "proposed_change": change}]))
    alice, bob = await h.user("alice"), await h.user("bob")
    await h.grant(alice, "file.read")
    h.model.push(ask("file.read"), final("done"))
    assert (await h.submit(alice, f"remember for my trip: {PRIVATE}")).status_code == 200
    await drain(h)
    return h, alice, bob, await h.rows(ImprovementCandidateRow)


async def _bobs_system_prompt(h, bob) -> str:
    h.model.push(final("hello bob"))
    assert (await h.submit(bob, "what's the weather")).status_code == 200
    return h.model.seen[-1][0].content


async def test_br_t2_judge_rows(judged):
    rows = []

    # 38 — the whole path, with an approval made from the default (redacted) console.
    h, alice, bob, [candidate] = await _alice_task_with_candidate(
        judged, f"When planning travel, note: {PRIVATE}.")
    listing = await h.client.get("/api/v1/admin/evaluations", headers=SU)
    assert listing.status_code == 200, listing.text
    assert PRIVATE not in listing.text  # the approver's default view shows no content
    approved = await h.client.post(f"{CONTROL}/candidates/{candidate.candidate_id}/approve",
                                   json={"reason": "looks_useful"}, headers=SU)
    assert approved.status_code == 200, approved.text
    reached = PRIVATE in await _bobs_system_prompt(h, bob)
    rows.append(_row("38", "authorized", "A's private content reaches B's worker prompt through a Judge "
                     "candidate the operator approved from the redacted console", reached,
                     "approval re-checks target, range and secret shapes only; an approved "
                     "worker.system_prompt applies to every user (19 §9)"))
    assert reached, "row 38 changed: re-run BR-T2 and update docs/OD_A1_BR_T2.md §3e"
    assert PRIVATE not in listing.text

    # 39 — the same candidate, never approved.
    h, alice, bob, [candidate] = await _alice_task_with_candidate(
        judged, f"When planning travel, note: {PRIVATE}.")
    reached = PRIVATE in await _bobs_system_prompt(h, bob)
    rows.append(_row("39", "authorized", "the same candidate reaches B while it waits in the queue", reached,
                     "nothing applies without a superuser approval (JDG-T9)"))
    assert not reached

    # 40 — a candidate carrying a secret-shaped value.
    h, alice, bob, queued = await _alice_task_with_candidate(
        judged, "Use api_key = sk-proj-TESTONLYabcdefghijklmnop to call the API.")  # TEST-ONLY fixture
    rows.append(_row("40", "authorized", "a secret-shaped value in a candidate reaches the queue or B",
                     bool(queued), "refused at creation (secret_in_value); approval re-validates too"))
    assert queued == []

    # 41 — the console's default views.
    h, alice, bob, [candidate] = await _alice_task_with_candidate(
        judged, f"When planning travel, note: {PRIVATE}.")
    views = [await h.client.get(f"/api/v1/admin/{v}", headers=SU) for v in ("evaluations", "tasks", "audit")]
    reached = any(PRIVATE in v.text for v in views)
    rows.append(_row("41", "authorized", "A's private content in the operator console's default views", reached,
                     "user content redacted to its length (DSH-B1); the unredacted view is audited first"))
    assert all(v.status_code == 200 for v in views) and not reached

    print("\nBR-T2 Judge / console dimension (docs/OD_A1_BR_T2.md §3e):")
    for n, model, attempt, reachable, why in rows:
        print(f"  [{'REACHABLE' if reachable else 'contained'}] {n} ({model}) {attempt} — {why}")
