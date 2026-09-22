"""Unit tests for the runtime's deterministic pieces."""

from __future__ import annotations

import uuid

import pytest

from server.agent.bounds import ConcurrencyGate, ConcurrencyLimits
from server.agent.context import COMPACTION_MARKER, compact
from server.agent.ports import UsageLimitReached
from server.agent.proposals import FinalAnswer, ProposalError, RequestCapabilities, ToolCall, parse_proposal
from server.models.provider import ChatMessage
from shared.schemas.authorization import Principal

pytestmark = pytest.mark.asyncio


# ── proposal parsing ───────────────────────────────────────────────────────


async def test_the_three_proposal_kinds_parse():
    assert isinstance(parse_proposal('{"type":"final_answer","content":"x"}'), FinalAnswer)
    assert isinstance(parse_proposal('```json\n{"type":"tool_call","tool":"t","operation":"o"}\n```'), ToolCall)
    parsed = parse_proposal('noise {"type":"request_capabilities","capabilities":[{"capability":"file.read"}]}')
    assert isinstance(parsed, RequestCapabilities)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "just prose",
        "[1, 2]",
        '{"type":"launch_missiles"}',
        '{"type":"final_answer","content":"x","risk":"low_read"}',
        '{"type":"tool_call","tool":"t","operation":"o","confirmed":true}',
        '{"type":"tool_call","tool":"t","operation":"o","user_id":"someone-else"}',
        '{"type":"tool_call","tool":"t","operation":"o","graph_id":"g"}',
        '{"type":"request_capabilities","capabilities":[]}',
        '{"type":"tool_call","tool":"t","operation":"o","arguments":{"blob":"' + "x" * 20000 + '"}}',
    ],
)
async def test_malformed_or_authority_bearing_proposals_are_rejected(text):
    with pytest.raises(ProposalError):
        parse_proposal(text)


# ── compaction (RT-T9) ─────────────────────────────────────────────────────


async def test_rt_t9_compaction_drops_but_never_invents():
    messages = [ChatMessage("system", "S"), ChatMessage("user", "REQUEST")] + [
        ChatMessage("user", f"observation {i} " + "x" * 200) for i in range(30)
    ]
    compacted = compact(messages, max_chars=2000)

    assert compacted[:2] == messages[:2]
    assert compacted[-1] == messages[-1]
    assert sum(len(m.content) for m in compacted) <= 2000
    originals = {m.content for m in messages}
    assert all(m.content in originals or m.content == COMPACTION_MARKER for m in compacted)


async def test_the_concurrency_gate_counts_per_session_user_and_globally():
    gate = ConcurrencyGate(ConcurrencyLimits(per_session=1, per_user=2, global_=3))
    user = uuid.uuid4()

    def principal(session=None, user_id=None):
        return Principal(user_id=user_id or user, device_id=uuid.uuid4(), session_id=session or uuid.uuid4())

    same_session = principal()
    async with gate.slot(same_session):
        with pytest.raises(UsageLimitReached):
            async with gate.slot(same_session):
                pass
        async with gate.slot(principal()):
            with pytest.raises(UsageLimitReached) as exc:
                async with gate.slot(principal()):
                    pass
            assert exc.value.limit == "per_user_concurrency"
            async with gate.slot(principal(user_id=uuid.uuid4())):
                with pytest.raises(UsageLimitReached) as exc:
                    async with gate.slot(principal(user_id=uuid.uuid4())):
                        pass
                assert exc.value.limit == "global_concurrency"
    async with gate.slot(same_session):
        pass
