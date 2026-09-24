"""Runtime × lifecycle, revocation, and the accepted OD-A1 residual.

OD-A1 is RESOLVED FOR PILOT — ACCEPTED RESIDUAL (docs/DECISION_REGISTER.md). The
runtime must not widen that residual: it adds no new persisted plaintext of the
working transcript, and its logical boundaries still fail closed.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from server.storage.models import AgentTask
from shared.schemas.enums import Visibility
from tests.runtime.conftest import ask, call, failure_of, final, pending_of

pytestmark = pytest.mark.asyncio


async def test_revoking_the_starting_device_stops_the_task_immediately(h):
    """SESSION-002: revocation is immediate. A task started on a device that is
    then revoked does not continue — even when approved from another device."""

    alice = await h.user("alice")
    await h.grant(alice, "file.write")
    f = await h.file(alice, None, Visibility.PRIVATE, "a.txt")
    h.model.push(ask("file.write"), call("files.write", "delete_file", ref=f))
    details = pending_of(await h.submit(alice))

    other = await h.user("alice")
    revoke = await h.client.delete(f"/api/v1/devices/{alice.device_id}", headers=other.auth)
    assert revoke.status_code == 204, revoke.text

    resp = await h.confirm(other, details["task_id"], details["confirmation_token"])

    assert resp.status_code == 401
    assert failure_of(resp) == "principal_revoked"
    assert h.writes.calls == []


async def test_a_paused_action_that_lost_its_volatile_state_fails_closed(h):
    """MEM-001: the paused action lives only in memory. After a restart (simulated)
    it cannot be resumed from a stored copy — the task fails, nothing runs."""

    alice = await h.user("alice")
    await h.grant(alice, "file.write")
    f = await h.file(alice, None, Visibility.PRIVATE, "a.txt")
    h.model.push(ask("file.write"), call("files.write", "delete_file", ref=f))
    details = pending_of(await h.submit(alice))

    h.runtime.states.pop(uuid.UUID(details["task_id"]))
    resp = await h.confirm(alice, details["task_id"], details["confirmation_token"])

    assert resp.status_code == 409
    assert failure_of(resp) == "confirmation_state_lost"
    assert h.writes.calls == []


async def test_the_runtime_persists_no_transcript(h):
    """The accepted OD-A1 residual is not widened: `agent_tasks` has no column
    for the transcript, the proposals, tool output, or a paused action, and the
    volatile state is gone once the task ends."""

    columns = {c.name for c in AgentTask.__table__.columns}
    assert columns == {
        "task_id", "user_id", "device_id", "session_id", "graph_id", "status", "failure_code",
        "response", "iterations", "model_calls", "tool_calls", "created_at", "updated_at", "finished_at",
        # 18 §3/§7: the task's mode — a four-value lifecycle enum, not content.
        "mode",
        # 18 §4/§7: a counter, like `iterations`.
        "worker_switches",
    }

    alice = await h.user("alice")
    h.model.push(final("done"))
    await h.submit(alice, "TRANSCRIPT-MARKER request text")

    assert len(h.runtime.states) == 0
    row = (await h.rows(AgentTask))[0]
    assert "TRANSCRIPT-MARKER" not in repr({c: getattr(row, c) for c in columns})


async def test_the_decision_records_do_not_claim_isolation():
    """INV-20 carried into this branch's documents: the matrix and the register
    record OD-A1 as an *accepted residual*, never as isolation."""

    for path in ("docs/CAPABILITY_MATRIX.md", "docs/DECISION_REGISTER.md"):
        text = Path(path).read_text().lower()
        for false_claim in ("fully isolated", "isolation is proven", "od-a1 is solved", "rce-proof"):
            assert false_claim not in text, (path, false_claim)
    register = Path("docs/DECISION_REGISTER.md").read_text()
    assert "RESOLVED FOR PILOT — ACCEPTED RESIDUAL" in register
