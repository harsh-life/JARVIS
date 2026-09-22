"""05 §2 `PARSE` — deterministic proposal parsing, never free-text authorization.

`server.agent.proposal.parse_proposal` is the only place a model's raw string
output is interpreted. These tests prove it never does more than validate a
strict JSON schema — no partial parsing, no "does this look like a tool call"
heuristic, and no extra field silently accepted.
"""

from __future__ import annotations

import pytest

from server.agent.proposal import parse_proposal
from shared.schemas.runtime import ProposalKind, ProposalParseError


def test_final_answer_parses():
    proposal = parse_proposal('{"kind": "final_answer", "final_text": "done"}')
    assert proposal.kind is ProposalKind.FINAL_ANSWER
    assert proposal.final_text == "done"


def test_tool_call_parses():
    raw = '{"kind": "tool_call", "tool_id": "notes", "capability": "file.write", "operation": "write_file", "arguments": {"path": "a.txt"}}'
    proposal = parse_proposal(raw)
    assert proposal.kind is ProposalKind.TOOL_CALL
    assert proposal.tool_id == "notes"
    assert proposal.arguments == {"path": "a.txt"}


def test_fenced_json_parses():
    raw = '```json\n{"kind": "final_answer", "final_text": "done"}\n```'
    proposal = parse_proposal(raw)
    assert proposal.final_text == "done"


def test_non_json_text_is_a_parse_error():
    with pytest.raises(ProposalParseError):
        parse_proposal("Sure! I'll go ahead and delete that file for you.")


def test_prose_wrapped_around_json_is_a_parse_error():
    """`[LOCKED]` (05 §0): free-form prose is never scanned for an embedded
    object — that would blur into interpreting free-text as authorization."""

    raw = 'Here is my plan: {"kind": "final_answer", "final_text": "done"} — hope that helps!'
    with pytest.raises(ProposalParseError):
        parse_proposal(raw)


def test_missing_required_field_is_a_parse_error():
    with pytest.raises(ProposalParseError):
        parse_proposal('{"kind": "tool_call", "tool_id": "notes"}')


def test_unknown_extra_field_is_a_parse_error():
    """`AgentProposal`'s `extra="forbid"` (inherited from `ORMBase`) means a
    model cannot smuggle an unrecognised field — e.g. an attempted `user_id`
    or `approved` — past validation; it simply fails to parse."""

    raw = '{"kind": "final_answer", "final_text": "done", "approved": true}'
    with pytest.raises(ProposalParseError):
        parse_proposal(raw)


def test_empty_output_is_a_parse_error():
    with pytest.raises(ProposalParseError):
        parse_proposal("")


def test_malformed_json_is_a_parse_error():
    with pytest.raises(ProposalParseError):
        parse_proposal('{"kind": "final_answer", "final_text": }')
