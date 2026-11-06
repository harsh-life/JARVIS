"""Extracting one JSON object from model output (harness-side; the E0
baseline uses the production parser instead)."""

from __future__ import annotations

import json
import re


class Unparseable(Exception):
    pass


def extract_object(text: str) -> dict:
    if not isinstance(text, str) or not text.strip():
        raise Unparseable("empty output")
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end < start:
        raise Unparseable("no JSON object found")
    try:
        value = json.loads(stripped[start:end + 1])
    except (ValueError, RecursionError):
        raise Unparseable("output is not valid JSON") from None
    if not isinstance(value, dict):
        raise Unparseable("output must be a JSON object")
    return value


# The production parser's acceptance rules for E0 (server/agent/proposals.py):
# a final answer must be exactly {type, content[, unresolved]} with 1..20,000
# chars of content; a tool call needs a tool and an operation; a capability
# request needs 1..8 capabilities. Argument-level checks on actions are not
# replicated — the harness records any action proposal and runs nothing.
MAX_ANSWER_CHARS = 20_000


def parse_baseline(text: str) -> tuple[str, str | None]:
    """→ ("final_answer", content) | ("tool_call", None) | ("request_capabilities", None);
    raises Unparseable like `ProposalError`."""

    payload = extract_object(text)
    kind = payload.get("type")
    if kind == "final_answer":
        content = payload.get("content")
        if (set(payload) - {"type", "content", "unresolved"} or not isinstance(content, str)
                or not 1 <= len(content) <= MAX_ANSWER_CHARS
                or not isinstance(payload.get("unresolved", False), bool)):
            raise Unparseable("invalid proposal fields")
        return "final_answer", content
    if kind == "tool_call":
        if not (isinstance(payload.get("tool"), str) and payload["tool"]
                and isinstance(payload.get("operation"), str) and payload["operation"]):
            raise Unparseable("invalid proposal fields")
        return "tool_call", None
    if kind == "request_capabilities":
        caps = payload.get("capabilities")
        if not isinstance(caps, list) or not 1 <= len(caps) <= 8:
            raise Unparseable("invalid proposal fields")
        return "request_capabilities", None
    raise Unparseable("invalid proposal fields: type")


# Whole-string placeholders a model writes into a list or text field to mean
# "nothing": "None", "N/A", "No contradictions found." — never a sentence that
# merely starts with "Nothing" or "No".
_PLACEHOLDER = re.compile(
    r"^(?:none|n/?a|nothing|null|-|no (?:contradictions?|conflicts?|issues?|actions?|uncertaint(?:y|ies))"
    r"(?: (?:were |was )?(?:found|identified|detected))?)?(?: found)?\.?$",
    re.IGNORECASE,
)


def is_placeholder(value: object) -> bool:
    return isinstance(value, str) and bool(_PLACEHOLDER.match(value.strip()))
