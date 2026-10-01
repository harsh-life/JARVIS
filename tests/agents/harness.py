"""The production composition root with the Agent Factory switched on, a
scripted worker model and the runtime suite's fake tools (tests/runtime)."""

from __future__ import annotations

import json
import re
from typing import Any

from tests.agents.support import ALL_TEMPLATES

ALLOWED_HOST = "advisories.example.org"

AGENTS_ON: dict[str, Any] = {
    "agents": {
        "enabled": True,
        "enabled_templates": list(ALL_TEMPLATES),
        "model_profiles": [{
            "profile_id": "general-agentic", "version": 1, "model_ref": "agent.primary",
            "features": ["agentic_reasoning", "tool_calling", "structured_output"],
            "context_window": 8192, "supported_runtimes": ["native"], "display_name": "General agent",
        }],
        "model_profiles_open_to_all": ["general-agentic"],
    },
    "execution": {"network": {"default_destinations": [ALLOWED_HOST]}},
}

DRAFT: dict[str, Any] = {
    "name": "Security advisory digest",
    "purpose": "Check my unread security advisories and summarize anything critical.",
    "task_tags": ["security_research", "summarization"],
    "requested_abilities": ["read_web_allowlisted"],
    "sources": [{"kind": "url", "value": f"https://{ALLOWED_HOST}/feed"}],
}

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def last_compile_id(messages) -> str:
    """The compile_id the last compile observation reported (what a real
    worker would read back)."""

    for message in reversed(list(messages)):
        match = re.search(r'"compile_id":\s*"(' + _UUID + ')"', message.content)
        if match:
            return match.group(1)
    raise AssertionError("no compile observation")


def last_agent_id(messages) -> str:
    for message in reversed(list(messages)):
        match = re.search(r'"agent_id":\s*"(' + _UUID + ')"', message.content)
        if match:
            return match.group(1)
    raise AssertionError("no agent id observed")


def call_json(tool: str, operation: str, args: dict, ref: str | None = None) -> str:
    payload: dict[str, Any] = {"type": "tool_call", "tool": tool, "operation": operation, "arguments": args}
    if ref is not None:
        payload["resource_ref"] = ref
    return json.dumps(payload)


API = "/api/v1/agents"
HEADER = "X-Confirmation-Token"


async def create_agent(h, actor, *, draft: dict | None = None) -> dict:
    """Compile and create an agent over HTTP, approving the confirmation."""

    compiled = await h.client.post(f"{API}/compile", json=draft or DRAFT, headers=actor.auth)
    assert compiled.status_code == 200 and compiled.json()["kind"] == "compiled", compiled.text
    compile_id = compiled.json()["compile_id"]
    first = await h.client.post(API, json={"compile_id": compile_id}, headers=actor.auth)
    assert first.status_code == 403, first.text
    token = first.json()["error"]["details"]["confirmation_token"]
    created = await h.client.post(API, json={"compile_id": compile_id}, headers={**actor.auth, HEADER: token})
    assert created.status_code == 201, created.text
    return created.json()


async def run_agent(h, actor, agent_id: str):
    return await h.client.post(f"{API}/{agent_id}/runs", json={}, headers=actor.auth)
