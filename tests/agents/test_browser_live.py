"""A Browser Use run, for real — Phase 6 slices 6D/6E (end to end).

The real Browser Use image (`HYPERMIND_TEST_BROWSER_IMAGE`, a digest-pinned
reference) runs in a real rootless gVisor container, through the production
composition root: the owner's grant, the engine's decision, the run's own
Model Gateway socket (a scripted model stands behind it, as every test's
model does), the run's own egress proxy, the provider, the supervisor, the
inbox. The scripted model drives Browser Use to open the allowed page, then
a page JARVIS does not allow, then report.

What is proven: the run completes through the gateway alone (no provider
key in the container), every page load is decided by JARVIS's proxy (the
allowed host is attempted through it; the other is refused by it, whatever
Browser Use's `allowed_domains` would have said), the result lands in the
owner's inbox, and nothing is left behind (container, sockets, workspace,
tokens).

Gated like the container suite: skipped without the stack and the image,
a failure with `HYPERMIND_REQUIRE_CONTAINER_STACK=1`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import uuid

import pytest

from server.agents.browser import BROWSER_CAPABILITY
from server.agents.registry import runtimes as runtime_registry
from server.storage.models import AgentInboxItemRow, AgentRunRow, AgentRunTokenRow, AuditEvent, UsageEvent
from tests.agents.harness import create_agent, run_agent
from tests.agents.test_browser_runs import BROWSER_DRAFT, HOST, browser_config

IMAGE = os.environ.get("HYPERMIND_TEST_BROWSER_IMAGE", "")
REQUIRE = os.environ.get("HYPERMIND_REQUIRE_CONTAINER_STACK") == "1"
IGNORE_CGROUPS = os.environ.get("HYPERMIND_CONTAINER_IGNORE_CGROUPS") == "1"


def _problem() -> str | None:
    if not IMAGE:
        return "HYPERMIND_TEST_BROWSER_IMAGE is not set"
    if shutil.which("podman") is None or shutil.which("runsc") is None:
        return "podman/runsc are not installed"
    if os.geteuid() == 0:
        return "the container tests run as an unprivileged user"
    if subprocess.run([shutil.which("podman"), "image", "exists", IMAGE], capture_output=True).returncode != 0:
        return f"the browser image {IMAGE} is not present"
    return None


_PROBLEM = _problem()
if _PROBLEM is not None and REQUIRE and IMAGE:
    pytest.fail(f"HYPERMIND_REQUIRE_CONTAINER_STACK=1 but {_PROBLEM}", pytrace=False)
pytestmark = pytest.mark.skipif(_PROBLEM is not None, reason=f"browser stack: {_PROBLEM}")


def _step(action: dict, goal: str) -> str:
    return json.dumps({"evaluation_previous_goal": "ok", "memory": "", "next_goal": goal, "action": [action]})


async def test_a_real_browser_run_goes_only_through_jarvis(make_harness, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runtime_registry, "BROWSER_USE_IMAGE", IMAGE)

    async def connect(ip, port):
        raise OSError("tests have no internet")

    monkeypatch.setattr("server.composition.browser_runs.proxy_connector", lambda: connect)
    monkeypatch.setattr("server.composition.browser_runs.proxy_resolver", lambda: (lambda host, port: ["93.184.216.34"]))
    config = browser_config(tmp_path)
    config["agents"]["containers"]["ignore_cgroups"] = IGNORE_CGROUPS
    config["agents"]["containers"]["kill_grace_seconds"] = 2
    h = await make_harness(config=config, agent_tools=True)
    await h.app.state.agent_factory.factory.browser_runs.start()   # the lifespan: verify the engine
    alice = await h.user("alice")
    agent = await create_agent(h, alice, draft=BROWSER_DRAFT)
    await h.grant(alice, BROWSER_CAPABILITY)
    h.model.push(
        _step({"navigate": {"url": f"https://{HOST}/latest", "new_tab": False}}, "open the allowed page"),
        _step({"navigate": {"url": "https://evil.example.com/", "new_tab": False}}, "try another host"),
        *[_step({"done": {"text": "Checked the advisories page.", "success": True}}, "report")] * 6,
    )

    resp = await run_agent(h, alice, agent["agent_id"])
    assert resp.status_code == 202, resp.text
    run_id = uuid.UUID(resp.json()["run_id"])
    for _ in range(600):
        [row] = await h.rows(AgentRunRow, AgentRunRow.run_id == run_id)
        if row.finished_at is not None:
            break
        await asyncio.sleep(0.5)
    assert row.status == "completed", (row.status, row.failure_code)
    [item] = await h.rows(AgentInboxItemRow, AgentInboxItemRow.run_id == run_id)
    assert "Checked the advisories page." in item.body
    assert len(h.model.seen) >= 2                       # every model call went through the gateway
    assert await h.rows(UsageEvent)                      # ... metered
    summary = [e.resource for e in await h.rows(AuditEvent) if e.action == "agent.egress.summary"]
    assert summary and "host_not_allowed" in summary[0], summary
    assert all(t.revoked_at is not None for t in await h.rows(AgentRunTokenRow, AgentRunTokenRow.run_id == run_id))
    names = subprocess.run([shutil.which("podman"), "ps", "-a", "--format", "{{.Names}}"],
                           capture_output=True, text=True).stdout
    assert f"jarvis-run-{run_id}" not in names
    await h.app.state.agent_factory.factory.browser_runs.stop()
