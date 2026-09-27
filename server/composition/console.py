"""The operator console's read-only adapters (28 §3).

`server.dashboard` declares what live state it reads (`server/dashboard/ports.py`)
and cannot import the runtime, the break-glass registry, the device hub, memory,
the vault or the Judge. This module satisfies those ports with **snapshots**:
plain data copied out of each component, never a handle that could act.

Two deliberate limits:

* **Memory**: only counts leave here. Facts are listed through the provider's
  own owner-filtered query, counted, and discarded in this module.
* **Secrets**: whether a configured reference resolves is decided from
  metadata alone — a `secret_references` row that is not revoked, or an
  environment variable being present. Nothing is resolved (DASH-005).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

from server.agent import AgentRuntime
from server.composition.break_glass import BreakGlassRegistry
from server.composition.evaluation import EvaluationJobs
from server.composition.improvements import EvaluationSwitchboard
from server.composition.latch import InProcessLatch
from server.composition.memory import MemoryFacade, VaultFacade
from server.composition.models import ProviderFactory, spec_from_entry
from server.composition.secret_context import key_provider_for
from server.config.schema import AppConfig
from server.dashboard.console import OperatorConsole
from server.dashboard.ports import BreakGlassRecordSummary, LiveTask
from server.execution.device_hub import DeviceHub
from server.gateway.security import SecurityCore
from server.memory.provider import MemoryProvider
from server.secrets.requester import SecretRequester
from server.storage import StorageBackend
from server.storage.models import SecretReference

MAX_FACTS_COUNTED = 10_000
PROBE_TIMEOUT_SECONDS = 3.0


class LiveState:
    def __init__(self, *, runtime: AgentRuntime, latch: InProcessLatch, break_glass: BreakGlassRegistry | None) -> None:
        self._runtime = runtime
        self._latch = latch
        self._break_glass = break_glass

    def live_tasks(self) -> list[LiveTask]:
        tasks = []
        for s in self._runtime.states.live():
            state = ("awaiting_confirmation" if s.pending is not None
                     else "waiting_for_platform" if s.platform_wait is not None else "running")
            tasks.append(LiveTask(
                task_id=s.task_id, user_id=s.principal.user_id, state=state, mode=s.mode.value,
                iterations=s.iterations, model_calls=s.model_calls, tool_calls=s.tool_calls,
                worker_switches=s.worker_switches,
                tripped_source=s.tripped.source if s.tripped is not None else None,
                break_glass_active=bool(self._break_glass and self._break_glass.active_for(s.task_id)),
            ))
        return tasks

    def latched_in_process(self) -> bool:
        return bool(self._latch.latched)

    def break_glass_enabled(self) -> bool:
        return bool(self._break_glass is not None and self._break_glass.enabled)

    def break_glass_records(self) -> list[BreakGlassRecordSummary]:
        if self._break_glass is None:
            return []
        return [
            BreakGlassRecordSummary(
                record_id=r.record_id, task_id=r.task_id, user_id=r.user_id, executables=tuple(r.executables),
                max_invocations=r.max_invocations, remaining=max(r.remaining, 0), reason=r.reason,
                activated_at=r.activated_at, expires_at=r.expires_at,
            )
            for r in self._break_glass.active()
        ]


class Probes:
    def __init__(self, *, config: AppConfig, core: SecurityCore, factory: ProviderFactory,
                 memory: MemoryFacade | None, provider: MemoryProvider | None, vault: VaultFacade | None,
                 device_hub: DeviceHub | None, scheduler_runner, switchboard: EvaluationSwitchboard,
                 jobs: EvaluationJobs | None) -> None:
        self._config = config
        self._core = core
        self._factory = factory
        self._memory = memory
        self._provider = provider
        self._vault = vault
        self._hub = device_hub
        self._runner = scheduler_runner
        self._switchboard = switchboard
        self._jobs = jobs

    def _model_entries(self) -> list[tuple[str, Any]]:
        config = self._config
        entries: list[tuple[str, Any]] = [("agent.primary", config.agent)]
        if config.agent.fallback is not None:
            entries.append(("agent.fallback", config.agent.fallback))
        if config.agent.recovery is not None:
            entries += [(f"agent.recovery.chain[{i}]", e) for i, e in enumerate(config.agent.recovery.chain)]
        entries += [(f"models_as_tools.{e.id}", e) for e in config.models_as_tools]
        if config.evaluation.provider is not None:
            entries.append(("evaluation.provider", config.evaluation.provider))
        return entries

    async def _probe(self, entry) -> str:
        """Only on request (`probe_models=true`): a health probe is a call to
        the provider's declared endpoint."""

        try:
            provider = self._factory(spec_from_entry(entry),
                                     key_provider=key_provider_for(entry.secret_ref, SecretRequester.server()))
            return "ok" if await asyncio.wait_for(provider.health(), timeout=PROBE_TIMEOUT_SECONDS) else "unavailable"
        except Exception:  # noqa: BLE001 — a probe reports
            return "unavailable"

    async def components(self, *, probe_models: bool) -> dict[str, dict]:
        models = []
        for role, entry in self._model_entries():
            models.append({
                "role": role, "provider": entry.provider, "model": entry.model,
                "local": entry.provider == "ollama",
                "status": await self._probe(entry) if probe_models else "not_probed",
            })
        memory: dict[str, Any] = {"status": "disabled"}
        if self._memory is not None:
            try:
                status = await self._memory.status()
                memory = {"status": "ok" if status.get("available") else "unavailable",
                          "writes_enabled": status.get("writes_enabled"), "auto_extract": status.get("auto_extract")}
            except Exception:  # noqa: BLE001
                memory = {"status": "unavailable"}
        runner_task = getattr(self._runner, "_task", None) if self._runner is not None else None
        return {
            "secret_store": {"status": "unlocked" if self._core.secret_store.is_unlocked else "locked"},
            "model_providers": {"entries": models},
            "mem0": memory,
            "vault": await self.vault_status() or {"status": "disabled"},
            "scheduler": {
                "status": "disabled" if not self._config.scheduler.enabled
                else "running" if runner_task is not None and not runner_task.done() else "not_running",
            },
            "device_channel": {
                "status": "disabled" if self._hub is None else "enabled",
                "connected": len(self._hub.connected_device_ids()) if self._hub is not None else 0,
            },
            "judge": self.judge_state(),
        }

    async def connected_device_ids(self) -> set[uuid.UUID]:
        return set(self._hub.connected_device_ids()) if self._hub is not None else set()

    async def memory_fact_counts(self, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, int] | None:
        if self._provider is None:
            return None
        counts: dict[uuid.UUID, int] = {}
        for user_id in user_ids:
            try:
                facts = await self._provider.list_facts(owner_user_id=user_id, readable_graph_ids=frozenset(),
                                                        limit=MAX_FACTS_COUNTED)
            except Exception:  # noqa: BLE001 — a count we cannot take is reported as missing
                continue
            counts[user_id] = sum(1 for f in facts if f.owner_user_id == user_id)
            del facts  # content never leaves this module
        return counts

    async def vault_status(self) -> dict | None:
        if self._vault is None:
            return None
        try:
            status = await self._vault.status()
        except Exception:  # noqa: BLE001
            return {"status": "unavailable"}
        return {"status": "ok" if status.get("available") else "unavailable",
                "indexed_commit": status.get("indexed_commit")}

    def judge_state(self) -> dict:
        view = self._switchboard.view()
        state: dict[str, Any] = {
            "configured_enabled": view.configured_enabled,
            "configured_may_request_stop": view.configured_may_request_stop,
            "judge_enabled": view.judge_enabled,
            "stop_requests_enabled": view.stop_requests_enabled,
            "evaluator": self._config.evaluation.evaluator if self._config.evaluation.enabled else None,
            "live_monitoring": self._config.evaluation.enabled and self._config.evaluation.live.enabled,
        }
        if self._jobs is not None:
            queue = self._jobs.queue
            state["queue"] = {"pending": queue.pending(), "completed": queue.completed, "failed": queue.failed,
                              "dropped": queue.dropped}
        return state


class MetadataResolvability:
    """Whether a configured secret reference resolves — from metadata only."""

    def __init__(self, storage: StorageBackend) -> None:
        self._storage = storage

    async def resolves(self, handle: str) -> bool | None:
        if handle.startswith("env:"):
            return bool(os.environ.get(handle[len("env:"):]))
        name = handle[len("secretstore:"):] if handle.startswith("secretstore:") else handle
        try:
            async with self._storage.session() as session:
                row = await session.get(SecretReference, name)
        except Exception:  # noqa: BLE001
            return None
        return row is not None and row.revoked_at is None


def build_console(
    config: AppConfig, *, runtime: AgentRuntime, latch: InProcessLatch, break_glass: BreakGlassRegistry | None,
    core: SecurityCore, factory: ProviderFactory, memory: MemoryFacade | None, provider: MemoryProvider | None,
    vault: VaultFacade | None, device_hub: DeviceHub | None, scheduler_runner, switchboard: EvaluationSwitchboard,
    jobs: EvaluationJobs | None, storage: StorageBackend,
) -> OperatorConsole:
    return OperatorConsole(
        config=config,
        live=LiveState(runtime=runtime, latch=latch, break_glass=break_glass),
        probes=Probes(config=config, core=core, factory=factory, memory=memory, provider=provider, vault=vault,
                      device_hub=device_hub, scheduler_runner=scheduler_runner, switchboard=switchboard, jobs=jobs),
        secrets=MetadataResolvability(storage),
    )


__all__ = ["LiveState", "MetadataResolvability", "Probes", "build_console"]
