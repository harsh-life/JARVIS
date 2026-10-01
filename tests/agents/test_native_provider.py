"""docs/29 §7.3–§7.4 — the native runtime provider (Phase 2, slice A).

The provider is an engine, never an authority: it receives an
`AgentRunContext` (no principal, session, device, secret or key) and hands it
to a port the composition root binds to the present user's request. Its
lifecycle: provision is a no-op and idempotent, cancel is safe to repeat,
deprovision purges runtime-side state so `list_runtime_agents()` never lists
the agent again (AGENT-T19, native half).
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from server.agent.agent_run import AgentRunBinding
from server.agent.envelope import Envelope
from server.agents.providers.native import NativeRuntimeProvider
from shared.schemas.agent_factory import (
    AgentRunContext,
    AgentRunStatus,
    CancelReason,
    RunHandle,
    RuntimeRef,
)
from shared.schemas.enums import RiskCategory
from tests.agents.test_compiler import compile_, compiled


class FakePort:
    def __init__(self):
        self.started: list[AgentRunContext] = []
        self.cancelled: list[tuple[uuid.UUID, CancelReason]] = []
        self.purged: list[uuid.UUID] = []
        self.status = AgentRunStatus.RUNNING

    async def start(self, ctx: AgentRunContext) -> AgentRunStatus:
        self.started.append(ctx)
        return AgentRunStatus.COMPLETED

    async def cancel(self, run_id: uuid.UUID, reason: CancelReason) -> None:
        self.cancelled.append((run_id, reason))

    async def status_of(self, run_id: uuid.UUID) -> AgentRunStatus:
        return self.status

    async def purge(self, agent_id: uuid.UUID) -> None:
        self.purged.append(agent_id)

    async def export(self, agent_id: uuid.UUID) -> bytes:
        return b'{"notebook": []}'


def ctx(**overrides) -> AgentRunContext:
    base = dict(run_id=uuid.uuid4(), agent_id=uuid.uuid4(), version=1, spec_hash="a" * 64,
                input_text="run", deadline=datetime.now(timezone.utc) + timedelta(minutes=2))
    base.update(overrides)
    return AgentRunContext(**base)


async def test_the_native_provider_is_the_registered_native_profile():
    provider = NativeRuntimeProvider(FakePort())
    assert provider.profile.runtime_id == "native"
    assert provider.profile.human_approval_mode == "jarvis_gateway"
    assert (await provider.health()).ok


async def test_provision_is_a_no_op_and_idempotent():
    port = FakePort()
    provider = NativeRuntimeProvider(port)
    spec = compiled(compile_())
    first, second = await provider.provision(spec), await provider.provision(spec)
    assert first == second == RuntimeRef(runtime_id="native", agent_id=spec.agent_id, version=spec.version)
    assert port.started == [] and port.purged == []
    assert await provider.list_runtime_agents() == []


async def test_a_run_is_started_from_the_context_alone():
    port = FakePort()
    provider = NativeRuntimeProvider(port)
    context = ctx()
    handle = await provider.start_run(context)
    assert handle == RunHandle(runtime_id="native", run_id=context.run_id)
    [received] = port.started
    assert received is context   # nothing added: no principal, no key, no endpoint
    assert await provider.run_status(handle) is AgentRunStatus.RUNNING


async def test_cancel_is_safe_to_repeat():
    port = FakePort()
    provider = NativeRuntimeProvider(port)
    handle = RunHandle(runtime_id="native", run_id=uuid.uuid4())
    await provider.cancel_run(handle, CancelReason.OWNER_STOP)
    await provider.cancel_run(handle, CancelReason.OWNER_STOP)
    assert port.cancelled == [(handle.run_id, CancelReason.OWNER_STOP)] * 2


async def test_agent_t19_deprovision_purges_runtime_state():
    port = FakePort()
    provider = NativeRuntimeProvider(port)
    ref = RuntimeRef(runtime_id="native", agent_id=uuid.uuid4(), version=3)
    receipt = await provider.deprovision(ref)
    assert receipt.removed and receipt.ref == ref
    assert port.purged == [ref.agent_id]
    assert ref not in await provider.list_runtime_agents()


async def test_export_is_delegated_to_jarvis_state():
    provider = NativeRuntimeProvider(FakePort())
    assert await provider.export_state(RuntimeRef(runtime_id="native", agent_id=uuid.uuid4(), version=1)) \
        == b'{"notebook": []}'


@pytest.mark.parametrize("runtime_id", ["letta", "openhands", "other"])
async def test_the_native_provider_refuses_foreign_handles(runtime_id):
    port = FakePort()
    provider = NativeRuntimeProvider(port)
    with pytest.raises(ValueError):
        await provider.cancel_run(RunHandle(runtime_id=runtime_id, run_id=uuid.uuid4()), CancelReason.OWNER_STOP)
    with pytest.raises(ValueError):
        await provider.deprovision(RuntimeRef(runtime_id=runtime_id, agent_id=uuid.uuid4(), version=1))
    assert port.cancelled == [] and port.purged == []


def test_a_run_binding_carries_a_ceiling_never_an_identity_or_credential():
    names = {f.name for f in dataclasses.fields(AgentRunBinding)}
    assert not names & {"principal", "user_id", "owner_user_id", "graph_id", "session_id", "device_id",
                        "secret_ref", "api_key", "endpoint", "grants", "capabilities", "token"}
    binding = AgentRunBinding.from_spec(compiled(compile_()), run_id=uuid.uuid4(), model_ref="agent.primary",
                                        budget_per_run=0.0)
    assert isinstance(binding.envelope, Envelope)
    assert binding.envelope.risk_ceiling is RiskCategory.LOW_READ
    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.spec_hash = "0" * 64  # type: ignore[misc]
