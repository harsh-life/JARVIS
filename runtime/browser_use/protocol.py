"""The JARVIS side of the Browser Use container — pure helpers, no Browser Use.

Runs **inside** the untrusted container (OD-AF-6, P2). Nothing here is
authority: the container's only ways out are two Unix sockets JARVIS mounted
in (`/run/jarvis/model.sock`, `/run/jarvis/egress.sock`), and what each
accepts is decided on the JARVIS side (the Model Gateway, docs/29 §12; the
egress proxy, OD-AF-13).

This module is importable without Browser Use, so the host's test suite
checks it directly:

* `gateway_request` — the closed OpenAI-compatible body the Model Gateway
  accepts: the alias `agent-model`, text messages, `stream: false`. No tools,
  no response format, no images (use_vision is off in v1);
* `with_schema` / `extract_json` — Browser Use asks for structured output;
  the gateway refuses `response_format`, so the schema travels in the system
  prompt and the JSON is taken from the text answer;
* `read_task` / `result_document` — the run's input and output files in
  `/scratch`, bounded, as data.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

MODEL_ALIAS = "agent-model"
MAX_TASK_CHARS = 32_000
MAX_RESULT_CHARS = 16_000
MAX_HOSTS = 32


def gateway_request(messages: Iterable[tuple[str, str]]) -> dict[str, Any]:
    body = [{"role": role if role in ("system", "user", "assistant") else "user", "content": text}
            for role, text in messages]
    if not body:
        raise ValueError("no messages")
    return {"model": MODEL_ALIAS, "messages": body, "stream": False}


def with_schema(messages: list[tuple[str, str]], schema: dict[str, Any]) -> list[tuple[str, str]]:
    """Put the JSON schema in the system prompt (adding one if needed)."""

    instruction = ("\n\nRespond with a single JSON object and nothing else. It must validate against this JSON "
                   f"schema:\n<json_schema>\n{json.dumps(schema, separators=(',', ':'))}\n</json_schema>")
    out = list(messages)
    if out and out[0][0] == "system":
        out[0] = ("system", out[0][1] + instruction)
    else:
        out.insert(0, ("system", instruction.strip()))
    return out


def extract_json(text: str) -> str:
    """The first complete JSON object in `text` (a fenced block or bare)."""

    start = text.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:index + 1]
                    try:
                        json.loads(candidate)
                    except ValueError:
                        break
                    return candidate
        start = text.find("{", start + 1)
    raise ValueError("no JSON object in the model's answer")


def read_task(raw: str) -> dict[str, Any]:
    """The run's task file, written by JARVIS. Validated anyway: a malformed
    one ends the run rather than guessing."""

    data = json.loads(raw)
    task = data.get("task")
    hosts = data.get("hosts")
    max_steps = data.get("max_steps")
    if not isinstance(task, str) or not task.strip() or len(task) > MAX_TASK_CHARS:
        raise ValueError("task")
    if not isinstance(hosts, list) or not hosts or len(hosts) > MAX_HOSTS \
            or not all(isinstance(h, str) and h for h in hosts):
        raise ValueError("hosts")
    if not isinstance(max_steps, int) or not 1 <= max_steps <= 100:
        raise ValueError("max_steps")
    return {"task": task, "hosts": hosts, "max_steps": max_steps}


def result_document(*, status: str, final: str | None, steps: int, error: str | None = None) -> str:
    if status not in ("completed", "failed"):
        raise ValueError("status")
    return json.dumps({
        "status": status,
        "final": (final or "")[:MAX_RESULT_CHARS] if final is not None else None,
        "steps": max(0, int(steps)),
        "error": (error or "")[:200] if error else None,
    })


__all__ = ["MODEL_ALIAS", "extract_json", "gateway_request", "read_task", "result_document", "with_schema"]
