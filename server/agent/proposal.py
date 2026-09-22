"""Deterministic proposal parsing — 05 §2 `PARSE`.

`[LOCKED]` (05 §0/§2): "The runtime must never interpret free-form model
prose as authorization." The model's raw text output is data, never a
decision; this module's only job is to turn that data into a validated
`AgentProposal` — deterministically, with no judgment call — or to fail with
`ProposalParseError`, which the orchestrator treats as a bounded-retry-then-
fail case (05 §6), never as an implicit denial-and-continue and never as an
implicit final answer.

The wire format is a single JSON object, optionally fenced in a ```json
code block (small concession to how models are usually prompted/trained to
emit structured output) — never partial free-text parsing, never a "does this
look like a tool call" heuristic. Anything else is a parse failure.
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from shared.schemas.runtime import AgentProposal, ProposalParseError

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_proposal(raw_text: str) -> AgentProposal:
    """Deterministically parse a model's raw output into an `AgentProposal`.

    Raises `ProposalParseError` for anything that is not exactly one JSON
    object matching `AgentProposal`'s schema — including extra top-level
    keys (`AgentProposal.model_config` forbids them, inherited from
    `ORMBase`), so a model cannot smuggle an extra field like `user_id` or
    `approved` past validation; it would simply fail to parse.
    """

    candidate = _extract_json_object(raw_text)
    if candidate is None:
        raise ProposalParseError("no JSON object found in model output")

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ProposalParseError(f"invalid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ProposalParseError("proposal JSON must be an object")

    try:
        return AgentProposal.model_validate(payload)
    except ValidationError as exc:
        raise ProposalParseError(f"proposal failed schema validation: {exc}") from exc


def _extract_json_object(raw_text: str) -> str | None:
    text = raw_text.strip()
    if not text:
        return None

    fence_match = _FENCE_RE.search(text)
    if fence_match:
        return fence_match.group(1)

    # Not fenced: accept only if the whole trimmed text is one JSON object
    # (starts with `{`, ends with `}`) — never scan for an embedded object
    # inside surrounding prose, which would blur into free-text parsing.
    if text.startswith("{") and text.endswith("}"):
        return text

    return None


__all__ = ["parse_proposal"]
