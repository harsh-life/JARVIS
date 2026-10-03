"""The Browser Use runtime provider — Phase 6 slice 6D (docs/29 §7.3).

It receives only an `AgentRunContext` and hands the container only what the
context permits: the **model** token and the two socket paths. Never the
tool token; there is no field for a principal, session, device, secret or
key to hand. It decides nothing (AF-C3 forbids it the engine, grants, the
SecretStore and the gateway).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from server.agents.providers.browser_use import BrowserUseRuntimeProvider, container_env
from server.agents.registry.runtimes import BROWSER_USE_RUNTIME_ID, NATIVE_RUNTIME_ID
from shared.schemas.agent_factory import (
    AgentRunContext,
    AgentRunStatus,
    CancelReason,
    RunHandle,
    RuntimeRef,
)

MODEL_TOKEN = "M" * 43
TOOL_TOKEN = "T" * 43


class Port:
    def __init__(self) -> None:
        self.launched: list[dict] = []
        self.killed: list[tuple] = []

    async def launch(self, *, run_id, agent_id, env, task_input):
        self.launched.append({"run_id": run_id, "agent_id": agent_id, "env": dict(env), "task_input": task_input})
        return f"jarvis-run-{run_id}"

    async def kill(self, run_id, reason):
        self.killed.append((run_id, reason))

    async def status_of(self, run_id):
        return AgentRunStatus.RUNNING

    async def running(self):
        return []

    async def healthy(self):
        return True


def _ctx(**overrides) -> AgentRunContext:
    values = dict(run_id=uuid.uuid4(), agent_id=uuid.uuid4(), version=1, spec_hash="h" * 64,
                  input_text="Check the advisories page.", deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
                  run_token=TOOL_TOKEN, model_run_token=MODEL_TOKEN)
    values.update(overrides)
    return AgentRunContext(**values)


async def test_the_container_gets_the_model_token_and_never_the_tool_token() -> None:
    port = Port()
    ctx = _ctx()
    handle = await BrowserUseRuntimeProvider(port).start_run(ctx)
    [launch] = port.launched
    assert launch["env"] == {"JARVIS_RUN_TOKEN": MODEL_TOKEN, "JARVIS_MODEL_SOCKET": "/run/jarvis/model.sock",
                             "JARVIS_EGRESS_SOCKET": "/run/jarvis/egress.sock"}
    assert TOOL_TOKEN not in repr(launch)
    assert launch["task_input"] == ctx.input_text
    assert handle == RunHandle(runtime_id=BROWSER_USE_RUNTIME_ID, run_id=ctx.run_id,
                               external_ref=f"jarvis-run-{ctx.run_id}")


def test_no_model_token_no_run() -> None:
    with pytest.raises(ValueError):
        container_env(_ctx(model_run_token=None))


async def test_cancel_is_the_kill_path_and_only_for_its_own_runs() -> None:
    port = Port()
    provider = BrowserUseRuntimeProvider(port)
    run_id = uuid.uuid4()
    await provider.cancel_run(RunHandle(runtime_id=BROWSER_USE_RUNTIME_ID, run_id=run_id), CancelReason.OWNER_STOP)
    assert port.killed == [(run_id, "owner_stop")]
    with pytest.raises(ValueError):
        await provider.cancel_run(RunHandle(runtime_id=NATIVE_RUNTIME_ID, run_id=run_id), CancelReason.OWNER_STOP)


async def test_nothing_persists_between_runs() -> None:
    provider = BrowserUseRuntimeProvider(Port())
    ref = RuntimeRef(runtime_id=BROWSER_USE_RUNTIME_ID, agent_id=uuid.uuid4(), version=1)
    assert (await provider.deprovision(ref)).removed is True
    assert await provider.export_state(ref) is None
    assert await provider.list_runtime_agents() == []
    assert provider.profile.enabled is True and provider.profile.runtime_id == BROWSER_USE_RUNTIME_ID
