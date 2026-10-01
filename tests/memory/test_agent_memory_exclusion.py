"""docs/29 §16.2 (AGENT-T17) — agent output never becomes the owner's memory.

On the real Mem0 stack with `memory.auto_extract: true`: an ordinary task's
answer is extracted (the control), while an agent run in `execute` mode — the
only mode in which formation is ever attempted — is not. Its result goes to
the owner's inbox only; no extraction call is even made (and so none is
metered).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from server.storage.models import AgentInboxItemRow, UsageEvent
from shared.schemas.enums import UsageKind
from tests.agents.harness import AGENTS_ON, create_agent, run_agent
from tests.agents.test_envelope_gate import FILE_DRAFT
from tests.memory.test_memory_runtime_real import MEM, extraction_output
from tests.runtime.conftest import final

pytestmark = pytest.mark.asyncio

CONFIG = {**AGENTS_ON, "memory": {"auto_extract": True}}


async def _model_calls(h, user_id) -> int:
    async with h.storage.session() as s:
        rows = (await s.execute(select(UsageEvent).where(UsageEvent.user_id == user_id,
                                                         UsageEvent.kind == UsageKind.MODEL_CALL))).scalars().all()
    return len(rows)


async def test_an_agent_runs_output_is_never_extracted_into_memory(make_harness, mem0_provider):
    h = await make_harness(config=CONFIG, memory_provider=mem0_provider, agent_tools=True)
    alice = await h.user("alice")
    await h.shared_graph(alice)        # memory is formed in the session's graph (21 §3)
    agent = await create_agent(h, alice, draft=FILE_DRAFT)
    assert agent["template_id"] == "file_organizer"          # runs in execute mode

    h.model.push(final("The user prefers metric units."),
                 extraction_output(("preference", "The user prefers metric units")))
    resp = await run_agent(h, alice, agent["agent_id"])
    assert resp.status_code == 202, resp.text
    view = resp.json()
    assert view["status"] == "completed" and view["task"]["mode"] == "execute"
    assert view["task"]["counters"]["model_calls"] == 1      # no extraction call
    assert await _model_calls(h, alice.user_id) == 1
    assert (await h.client.get(MEM, headers=alice.auth)).json()["items"] == []
    [item] = await h.rows(AgentInboxItemRow)
    assert item.body == "The user prefers metric units."     # it went to the inbox instead

    # The control: the same answer from an ordinary task *is* extracted.
    h.model.script.clear()
    h.model.push(final("Here is your conversion."),
                 extraction_output(("preference", "The user prefers metric units")))
    ordinary = await h.submit(alice, "convert 5 miles; I always use metric")
    assert ordinary.status_code == 200, ordinary.text
    facts = (await h.client.get(MEM, headers=alice.auth)).json()["items"]
    assert [f["content"] for f in facts] == ["The user prefers metric units"]
