"""Explicit context assembly — 05 §7, 11 §3.

`[LOCKED]` (05 §7 of this branch's instructions): "the model should receive
only the context it is authorized and intended to see," separated into
system/runtime instructions, task state, conversation/session state,
authorized memory, tool/capability descriptions, and observations — never a
raw database record, another user's memory, or any of `12`'s SecretStore
contents.

This module builds exactly that separation, deterministically, with no model
or free-form scoring involved — the only "AI" in this file is the text that
ends up inside a `ModelMessage`, never a decision this file makes for itself.
"""

from __future__ import annotations

from shared.schemas.agent_config import ToolContract
from shared.schemas.runtime import MemoryItem, ModelMessage

SYSTEM_PREAMBLE = (
    "You are the Hypermind Track B agent. You may only act by proposing one "
    "of: a tool call, a model-tool call, or a final answer, in the exact "
    "structured form described below. Every proposal is independently "
    "authorized by deterministic infrastructure before anything executes; "
    "you cannot grant yourself a capability, bypass a denial, or act as any "
    "user other than the one who submitted this task."
)

# 05 §7 / 11 §3: relevance-bounded, never a lifetime dump. `[IMPL]` the exact
# K (OD-MEM-1 in 11 §9 leaves the ranking strategy open); the bound's
# *existence* is what MEM-T8/RT-T9 require, and this is this branch's
# concrete default.
DEFAULT_MEMORY_LIMIT = 8

# 05 §6: "oversized tool/model output -> truncate + note; never blow the
# context window silently." Characters, not tokens — a conservative proxy
# that needs no tokenizer dependency in a module this decoupled.
MAX_OBSERVATION_CHARS = 4000


def render_tool_catalog(contracts: list[ToolContract]) -> str:
    """07 §1's discovery surface, rendered as text: the model learns which
    tools/model-tools exist and their declared input shape — never their
    implementation, never a secret, never a filesystem/network detail beyond
    what the contract itself already declares publicly."""

    if not contracts:
        return "No tools are currently enabled for this task."
    lines = ["Available tools (invoke only by tool_id, exactly as listed):"]
    for contract in contracts:
        lines.append(
            f"- {contract.tool_id} (requires capability {contract.required_capability!r}): "
            f"{contract.description}"
        )
    return "\n".join(lines)


def render_memory(items: list[MemoryItem]) -> str:
    """11 §3: only what hydration already visibility-filtered — this
    function has no access to anything else and cannot widen the set."""

    if not items:
        return "No relevant prior memory."
    lines = ["Relevant prior context:"]
    lines.extend(f"- {item.content}" for item in items[:DEFAULT_MEMORY_LIMIT])
    return "\n".join(lines)


def truncate_observation(text: str) -> tuple[str, bool]:
    """05 §6's truncate-and-note rule. Returns `(text, was_truncated)` so the
    caller can append an explicit note rather than silently shortening."""

    if len(text) <= MAX_OBSERVATION_CHARS:
        return text, False
    return text[:MAX_OBSERVATION_CHARS], True


def build_initial_messages(
    *,
    input_text: str,
    tool_contracts: list[ToolContract],
    memory_items: list[MemoryItem],
) -> list[ModelMessage]:
    """05 §7's hydration pipeline, rendered into the normalized message list
    `06 §1`'s `ModelProvider.invoke` accepts.

    Order matters for a human/model reading it, not for any authorization
    property — every one of these inputs was already authorized/filtered by
    the caller (the memory hydrator, the tool registry lookup) before this
    function ever sees it; this function only arranges already-safe text.
    """

    system_text = "\n\n".join(
        [
            SYSTEM_PREAMBLE,
            render_tool_catalog(tool_contracts),
            render_memory(memory_items),
        ]
    )
    return [
        ModelMessage(role="system", content=system_text),
        ModelMessage(role="user", content=input_text),
    ]


def append_observation(
    messages: list[ModelMessage], *, label: str, text: str
) -> list[ModelMessage]:
    """05 §2's `OBS -> MODEL` edge: feed a result (or a denial, per 05 §1
    `BACK`) back in as a `tool`-role message, bounded per `truncate_observation`.
    """

    bounded, truncated = truncate_observation(text)
    if truncated:
        bounded += "\n[truncated]"
    messages.append(ModelMessage(role="tool", content=bounded, name=label))
    return messages


__all__ = [
    "DEFAULT_MEMORY_LIMIT",
    "MAX_OBSERVATION_CHARS",
    "SYSTEM_PREAMBLE",
    "append_observation",
    "build_initial_messages",
    "render_memory",
    "render_tool_catalog",
    "truncate_observation",
]
