"""Opt-in LIVE checks of the composed stack (full-stack validation, 2026-11-26).

Skipped unless `JARVIS_LIVE_STACK=1`. Needs:
  * a local Ollama with `qwen2.5:3b-instruct` at http://127.0.0.1:11434 (cost 0);
  * the memory extra and the provisioned embedder (`python -m server.memory provision`).

Nothing here is scripted on the model side: the worker is the real local model,
running through the production composition root, the real Security Core, the
real Mem0 store and the real usage ledger. The model's text is recorded, never
asserted on (a live model is not deterministic); what is asserted is what the
deterministic infrastructure did with it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from server.storage.models import AgentTask, CapabilityGrant, UsageEvent
from shared.schemas.enums import UsageKind

pytestmark = pytest.mark.skipif(os.environ.get("JARVIS_LIVE_STACK") != "1", reason="live stack is opt-in")

MODEL = os.environ.get("JARVIS_LIVE_MODEL", "qwen2.5:3b-instruct")
EVIDENCE = os.environ.get("JARVIS_LIVE_EVIDENCE")
LIVE_AGENT = {"agent": {"provider": "ollama", "model": MODEL, "endpoint": "http://127.0.0.1:11434",
                        "timeout_seconds": 180,
                        "bounds": {"wall_clock_timeout_seconds": 600, "max_model_calls": 6}}}


def record(name: str, payload: dict) -> None:
    if EVIDENCE:
        Path(EVIDENCE).mkdir(parents=True, exist_ok=True)
        (Path(EVIDENCE) / f"{name}.json").write_text(json.dumps(payload, indent=2, default=str))


async def model_usage(h) -> list[UsageEvent]:
    return [u for u in await h.rows(UsageEvent) if u.kind is UsageKind.MODEL_CALL]


async def test_live_central_ai_request_through_the_runtime(make_harness):
    h = await make_harness(config=LIVE_AGENT)
    alice = await h.user("alice")
    resp = await h.submit(alice, "In one short sentence: what is the capital of France?")
    body = resp.json()
    usage = await model_usage(h)
    tasks = await h.rows(AgentTask)
    record("live-central-ai", {
        "http_status": resp.status_code, "body": body,
        "usage": [{"provider": u.provider, "model": u.model, "tokens": u.tokens_or_units,
                   "estimated_cost": u.estimated_cost} for u in usage],
        "task_status": [t.status for t in tasks],
    })
    # Every model call is metered with provider, model and tokens, at cost 0 (local).
    assert usage, "no model call was metered"
    assert all(u.provider == "ollama" and u.model == MODEL for u in usage)
    assert all(u.tokens_or_units > 0 and u.estimated_cost == 0.0 for u in usage)
    # The runtime either completed with a parsed final answer, paused for the
    # human (the model asked for authority), or ended in an explicit failure
    # code — never an unparsed model string passed through.
    assert resp.status_code in (200, 403, 422, 502, 503), resp.text
    if resp.status_code == 200:
        assert body["status"] == "completed" and isinstance(body["response"], str) and body["response"]
    if resp.status_code == 403:
        assert body["error"]["code"] == "confirmation_required"
        pending = body["error"]["details"]
        # The human declines: nothing is granted and nothing executes.
        declined = await h.confirm(alice, pending["task_id"], pending["confirmation_token"], approve=False)
        grants = await h.rows(CapabilityGrant)
        tool_runs = [u for u in await h.rows(UsageEvent) if u.kind is UsageKind.TOOL_CALL]
        record("live-central-ai-declined", {
            "pending": pending["pending"], "after_decline_status": declined.status_code,
            "after_decline_body": declined.json(), "grants": len(grants), "tool_calls": len(tool_runs),
        })
        assert grants == [] and tool_runs == []


async def test_live_memory_is_hydrated_isolated_and_survives_a_restart(make_harness, tmp_path):
    from tests.memory.conftest import mem0_section, require_memory_stack

    require_memory_stack()
    from server.memory.mem0_provider import build_mem0_provider

    section = mem0_section(tmp_path)
    provider = build_mem0_provider(section)
    h = await make_harness(config=LIVE_AGENT, memory_provider=provider)
    alice, bob = await h.user("alice"), await h.user("bob")
    # Bob is a member of the same graph: membership alone must not expose
    # Alice's private fact to him.
    graph = await h.shared_graph(alice, bob)
    marker = f"cardamom-tea-{uuid.uuid4().hex[:6]}"

    stored = await h.client.post("/api/v1/memory", headers=alice.auth,
                                 json={"fact_type": "preference", "content": f"I prefer {marker} in the morning",
                                       "graph_id": str(graph)})
    assert stored.status_code in (200, 201), stored.text
    fact_id = stored.json().get("fact_id") or stored.json().get("id")

    # Refused by the deterministic write gate (EMO-002/004): relationship/emotional content.
    refused = await h.client.post("/api/v1/memory", headers=alice.auth,
                                  json={"fact_type": "preference", "content": "I feel so lonely and you are my only friend",
                                        "graph_id": str(graph)})
    assert refused.status_code in (400, 422), refused.text
    bad_type = await h.client.post("/api/v1/memory", headers=alice.auth,
                                   json={"fact_type": "relationship", "content": "Bob is my brother", "graph_id": str(graph)})
    assert bad_type.status_code in (400, 422), bad_type.text

    mine = await h.client.get("/api/v1/memory", headers=alice.auth, params={"query": "morning drink"})
    theirs = await h.client.get("/api/v1/memory", headers=bob.auth, params={"query": "morning drink"})
    assert marker in mine.text
    assert marker not in theirs.text
    # Bob (same graph) tries an owner-only operation on Alice's fact: it must be
    # indistinguishable from a fact that does not exist (04 §7), and unchanged.
    by_id = await h.client.patch(f"/api/v1/memory/{fact_id}", headers=bob.auth,
                                 json={"content": "I prefer coffee"}) if fact_id else None

    # A live task hydrates only the owner's authorized memory.
    await h.enter_graph(alice, graph)
    resp = await h.submit(alice, "What do I like to drink in the morning? Answer in one sentence.")
    usage = await model_usage(h)

    # Restart: a separate process opens the same store from disk.
    probe = subprocess.run(
        [sys.executable, "-c", (
            "import asyncio, sys, uuid\n"
            "from server.config.schema import Mem0SectionConfig\n"
            "from server.memory.mem0_provider import build_mem0_provider\n"
            f"p = build_mem0_provider(Mem0SectionConfig(path={str(section.path)!r}, embedder_cache={str(section.embedder_cache)!r}))\n"
            f"facts = asyncio.run(p.list_facts(owner_user_id=uuid.UUID({str(alice.user_id)!r}), readable_graph_ids=frozenset(), limit=50))\n"
            "print('\\n'.join(f.content for f in facts))\n"
        )],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parents[2], timeout=300,
    )
    record("live-memory", {
        "store_status": stored.status_code, "stored_visibility": stored.json().get("visibility"),
        "stored_owner_is_alice": stored.json().get("owner_user_id") == str(alice.user_id),
        "graph_shared_with_bob": True, "gate_refusals": [refused.status_code, bad_type.status_code],
        "owner_sees_marker": marker in mine.text, "other_user_sees_marker": marker in theirs.text,
        "other_user_patch_status": by_id.status_code if by_id is not None else None,
        "task_http_status": resp.status_code, "task_body": resp.json(),
        "model_usage": [{"provider": u.provider, "model": u.model, "tokens": u.tokens_or_units} for u in usage],
        "restart_probe_returncode": probe.returncode,
        "restart_probe_found_marker": marker in probe.stdout,
        "restart_probe_stderr_tail": probe.stderr[-600:],
    })
    if by_id is not None:
        assert by_id.status_code == 404
    assert probe.returncode == 0, probe.stderr[-2000:]
    assert marker in probe.stdout
