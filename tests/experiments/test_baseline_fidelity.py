"""E0 must be the production prompt and parser, not an approximation. The
harness keeps a frozen copy (so it never loads `server.agent`); this test —
which may import both sides — holds the copy byte-for-byte to production."""

from __future__ import annotations

import json

import pytest

from experiments.contracts import EvidenceItem
from experiments.parsing import Unparseable, parse_baseline
from experiments.prompts import (
    BASELINE_RETRY,
    BASELINE_SYSTEM_V1,
    baseline_context,
    baseline_observation,
    evidence_lines,
)
from server.agent.context import context_message, observation, system_prompt
from server.agent.proposals import FinalAnswer, ProposalError, parse_proposal
from shared.schemas.agent import TaskMode


def test_the_frozen_system_prompt_is_the_production_prompt():
    assert BASELINE_SYSTEM_V1 == system_prompt([], [], TaskMode.EXECUTE)


def test_the_context_and_observation_messages_match_production():
    ev = [EvidenceItem(id="E1", title="t", content="c"), EvidenceItem(id="E2", title="u", content="d")]
    ours, theirs = baseline_context("Q?", evidence_lines(ev)), context_message([], [], "Q?", knowledge=evidence_lines(ev))
    assert (ours.role, ours.content) == (theirs.role, theirs.content)
    ours, theirs = baseline_context("Q?", []), context_message([], [], "Q?")
    assert (ours.role, ours.content) == (theirs.role, theirs.content)
    long = "x" * 5000
    assert baseline_observation(long).content == observation(long, limit=4000).content
    assert "Reply with exactly one JSON object in one of the documented forms." in BASELINE_RETRY


VECTORS = [
    '{"type": "final_answer", "content": "Canberra."}',
    '```json\n{"type": "final_answer", "content": "Canberra."}\n```',
    'Sure! {"type": "final_answer", "content": "x", "unresolved": true} hope that helps',
    '{"type": "final_answer", "content": ""}',
    '{"type": "final_answer", "content": "x", "confidence": 0.9}',
    '{"type": "final_answer"}',
    '{"type": "final_answer", "content": 5}',
    '{"type": "tool_call", "tool": "fs", "operation": "delete", "arguments": {}}',
    '{"type": "request_capabilities", "capabilities": [{"capability": "files.write"}]}',
    '{"type": "request_capabilities", "capabilities": []}',
    '{"type": "answer", "content": "x"}',
    '[{"type": "final_answer", "content": "x"}]',
    'no json here',
    '',
    json.dumps({"type": "final_answer", "content": "y" * 20001}),
]


@pytest.mark.parametrize("text", VECTORS)
def test_the_parser_accepts_and_classifies_exactly_like_production(text):
    try:
        prod = parse_proposal(text)
    except ProposalError:
        prod = None
    try:
        kind, content = parse_baseline(text)
    except Unparseable:
        kind = None
    if prod is None:
        assert kind is None
    else:
        assert kind == prod.type
        if isinstance(prod, FinalAnswer):
            assert content == prod.content
