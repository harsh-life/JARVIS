"""Container reconciliation and its configuration — Phase 6 slice 6C.

docs/29 §25.2: at startup and every 10 minutes, every managed container that
is not a live run's is removed and audited. The engine is verified first:
containers switched on without a rootless Podman with `runsc` is a startup
failure, never a weaker boundary.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from server.composition.containers import ContainerReconciler
from server.config.schema import AgentContainersConfig, AppConfig
from server.execution.containers import ContainerError, container_name
from server.storage.models import AuditEvent
from tests.agents.harness import AGENTS_ON, create_agent
from tests.runtime.conftest import base_config_payload


class FakeEngine:
    def __init__(self, *, verify_error: Exception | None = None) -> None:
        self.verify_error = verify_error
        self.reconciled: list[set] = []
        self.orphans: list[str] = []

    async def verify(self) -> None:
        if self.verify_error is not None:
            raise self.verify_error

    async def reconcile(self, live):
        self.reconciled.append(set(live))
        removed, self.orphans = self.orphans, []
        return removed


def _config(**containers) -> AppConfig:
    payload = base_config_payload()
    payload["agents"] = {"enabled": True, "containers": containers}
    return AppConfig.model_validate(payload)


def test_containers_are_off_by_default() -> None:
    assert AgentContainersConfig().enabled is False
    assert _config().agents.containers.enabled is False


def test_enabling_containers_needs_the_factory_an_absolute_engine_and_a_run_directory() -> None:
    _config(enabled=True, podman="/usr/bin/podman", run_dir="/var/lib/jarvis/runs")
    for bad in ({"enabled": True, "run_dir": None}, {"enabled": True, "podman": "podman", "run_dir": "/x/runs"},
                {"enabled": True, "run_dir": "relative/runs"}, {"enabled": True, "run_dir": "/"},
                {"enabled": True, "run_dir": "/tmp/../etc"}):
        with pytest.raises(ValidationError):
            _config(**bad)
    payload = base_config_payload()
    payload["agents"] = {"enabled": False, "containers": {"enabled": True, "run_dir": "/var/lib/jarvis/runs"}}
    with pytest.raises(ValidationError, match="agents.enabled"):
        AppConfig.model_validate(payload)


def test_reconciliation_runs_at_least_every_ten_minutes() -> None:
    assert AgentContainersConfig().reconcile_interval_seconds == 600
    with pytest.raises(ValidationError):
        AgentContainersConfig(reconcile_interval_seconds=601)
    with pytest.raises(ValidationError):
        AgentContainersConfig(kill_grace_seconds=0)


async def test_live_runs_are_the_unfinished_ones(make_harness) -> None:
    h = await make_harness(config=AGENTS_ON, agent_tools=True)
    alice = await h.user("alice")
    agent = await create_agent(h, alice)
    service = h.app.state.agent_factory.factory.service
    async with h.storage.session() as s:
        spec = (await service.load(s, uuid.UUID(agent["agent_id"]), fresh=True))[1]
        live = await service.create_run(s, spec=spec, run_id=uuid.uuid4())
        done = await service.create_run(s, spec=spec, run_id=uuid.uuid4())
        done.status, done.finished_at = "completed", datetime.now(timezone.utc)
        await s.commit()
    engine = FakeEngine()
    reconciler = ContainerReconciler(engine=engine, storage=h.storage, interval_seconds=600)
    assert await reconciler.live_runs() == {live.run_id}


async def test_every_removed_orphan_is_audited(make_harness) -> None:
    h = await make_harness(config=AGENTS_ON, agent_tools=True)
    engine = FakeEngine()
    orphan = container_name(uuid.uuid4())
    engine.orphans = [orphan]
    reconciler = ContainerReconciler(engine=engine, storage=h.storage, interval_seconds=600)
    assert await reconciler.reconcile() == [orphan]
    rows = [e for e in await h.rows(AuditEvent) if e.action == "agent.orphan.deprovisioned"]
    assert [r.resource for r in rows] == [f"container:{orphan}"]


async def test_start_verifies_the_engine_and_refuses_a_weaker_one(make_harness) -> None:
    h = await make_harness(config=AGENTS_ON, agent_tools=True)
    reconciler = ContainerReconciler(engine=FakeEngine(verify_error=ContainerError("podman is not rootless")),
                                     storage=h.storage, interval_seconds=600)
    with pytest.raises(ContainerError, match="rootless"):
        await reconciler.start()


async def test_start_reconciles_at_once(make_harness) -> None:
    h = await make_harness(config=AGENTS_ON, agent_tools=True)
    engine = FakeEngine()
    reconciler = ContainerReconciler(engine=engine, storage=h.storage, interval_seconds=600)
    await reconciler.start()
    try:
        assert len(engine.reconciled) == 1
    finally:
        await reconciler.stop()


async def test_the_composition_root_wires_the_reconciler_only_when_switched_on(make_harness, tmp_path) -> None:
    off = await make_harness(config=AGENTS_ON, agent_tools=True)
    assert off.app.state.container_reconciler is None
    on = await make_harness(config={**AGENTS_ON, "agents": {**AGENTS_ON["agents"], "containers": {
        "enabled": True, "podman": "/usr/bin/podman", "run_dir": str(tmp_path / "runs")}}}, agent_tools=True)
    reconciler = on.app.state.container_reconciler
    assert reconciler is not None and reconciler.engine.settings.podman == "/usr/bin/podman"
