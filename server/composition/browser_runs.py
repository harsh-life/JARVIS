"""Browser Use runs — the JARVIS side of an external run (Phase 6, slices 6D–6F).

`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]` (docs/29). OD-AF-6: Browser Use,
P2 contained workspace. The container is **untrusted execution
infrastructure**; everything that decides is here or below.

**Start** (`start`, called by the owner's `POST /agents/{id}/runs`, after the
same checks every run passes — ownership, active, graph, runnable, budget):

1. the hosts are the spec's (`browse_hosts`, from the hash-verified spec) and
   all within the operator's egress policy (`policy_for_run`);
2. the owner must already hold a standing `browser.session` grant covering
   them — the run's task activation is made **from** it, never prompted for;
3. the engine (04) decides `browser.session`/`browse` (`low_write`,
   OD-AF-15) for the owner on that task, with the hosts as its scope — an
   answer other than allow refuses the run;
4. the run's two tokens; a private workspace; the run's task file; the run's
   own Model Gateway listener (bound to this run: another run's token is
   refused on it) and the run's own egress proxy (the spec's hosts);
5. the container, digest-pinned, with the two sockets, the **model** token
   and the two socket paths — no tool token (a P2 run makes no tool calls),
   no principal, session, device credential, provider key or secret.

**Supervision** (one task per run), every `poll_seconds`, until the
container exits: the run's model token is re-authenticated through the same
core as every gateway request (a stop, a pause, a delete, a change, a
revocation, a lost membership all end it), the global stop is read, and the
deadline checked. Any of them stops the container — the deterministic kill
path (stop, kill after the grace, remove).

**Finish**: the result file is read as untrusted data (`read_result`); the
run ends `completed` or `failed` with a code (`runtime_crashed`,
`result_unreadable`, `wall_clock_timeout`, `emergency_stop`, …); its tokens
are revoked, its task closed and its activation removed; the result goes to
the owner's inbox through the ordinary path (scrubbed, bounded) and nowhere
else; usage (model calls, already metered by the gateway, and the tunnel's
bytes) is attributed to the run; the egress decisions are summarized in the
audit trail; sockets closed, workspace removed.

**Recovery**: at startup, a browser run left unfinished (its supervisor died
with the process) is closed `failed: interrupted`, tokens revoked — never
resumed; the container reconciler removes its container.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.agent.ports import ActionRequest
from server.agent.records import row_principal
from server.agents.browser import BROWSE, BROWSER_CAPABILITY, browse_hosts, hosts_scope, read_result, task_document
from server.agents.providers.browser_use import BrowserUseRuntimeProvider
from server.agents.gateway.core import GatewayDenied
from server.agents.registry.runtimes import BROWSER_USE_RUNTIME_ID
from server.composition.latch import InProcessLatch, SupervisorGate
from server.composition.model_gateway import ModelGatewayListener
from server.composition.security_port import RuntimeSecurityAdapter
from server.composition.usage_port import RuntimeUsageAdapter
from server.execution.containers import (
    CONTAINER_SOCKETS,
    ContainerEngine,
    ContainerError,
    ContainerSpec,
    EngineSettings,
    container_name,
    prepare_workspace,
    remove_workspace,
)
from server.gateway.routers.model_gateway import build_model_gateway_app
from server.net.egress_proxy import EgressProxy, ProxyDecision, policy_for_run
from server.net.resolve import system_resolver
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.storage.models import AgentRunRow, AgentTask
from shared.schemas.agent import AgentTaskStatus
from shared.schemas.agent_factory import AgentRunContext, AgentRunStatus, CompiledAgentSpec, RunTokenPurpose
from shared.schemas.authorization import Operation, Principal, ResourceType
from shared.schemas.enums import AuditActor, AuditResult, PermissionDecisionValue, UsageKind

if TYPE_CHECKING:
    from server.composition.agents import AgentFactory
    from server.composition.model_gateway import HttpModelGateway
    from server.config.schema import AppConfig
    from server.gateway.security import SecurityCore
    from server.storage import StorageBackend

logger = logging.getLogger("hypermind.agents.browser_runs")

MODEL_SOCKET = "model.sock"
EGRESS_SOCKET = "egress.sock"


# Seams the tests replace; production uses these.
def engine_for(settings: EngineSettings) -> ContainerEngine:
    return ContainerEngine(settings)


def proxy_resolver():
    return system_resolver


def proxy_connector():
    return None


class BrowserRunRefused(Exception):
    """A browser run that must not start. `code` is the run's failure code."""

    def __init__(self, code: str, message: str, *, forbidden: bool = False) -> None:
        super().__init__(message)
        self.code, self.message, self.forbidden = code, message, forbidden


@dataclass
class _Live:
    run_id: uuid.UUID
    agent_id: uuid.UUID
    owner_user_id: uuid.UUID
    task_id: uuid.UUID
    name: str
    model_token: str = field(repr=False)
    deadline: datetime
    socket_dir: Path
    scratch_dir: Path
    proxy: EgressProxy
    listener: ModelGatewayListener
    hosts: tuple[str, ...] = ()
    max_steps: int = 1
    decisions: dict[str, int] = field(default_factory=dict)
    stop_reason: str | None = None
    supervisor: asyncio.Task | None = None


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class BrowserRuns:
    def __init__(self, *, factory: "AgentFactory", storage: "StorageBackend", core: "SecurityCore",
                 gateway: "HttpModelGateway", config: "AppConfig", latch: InProcessLatch, image: str,
                 poll_seconds: float = 1.0) -> None:
        containers = config.agents.containers
        self._factory = factory
        self._storage = storage
        self._core = core
        self._gateway = gateway
        self._config = config
        self._latch = latch
        self._image = image
        self._poll = poll_seconds
        self._run_dir = Path(containers.run_dir or "/nonexistent")
        self.engine = engine_for(EngineSettings(
            podman=containers.podman, ignore_cgroups=containers.ignore_cgroups, memory_mb=containers.memory_mb,
            cpus=containers.cpus, pids_limit=containers.pids_limit, kill_grace_seconds=containers.kill_grace_seconds))
        self.live: dict[uuid.UUID, _Live] = {}
        # The run's container is started only through the provider, which
        # sees nothing but the run's `AgentRunContext` (docs/29 §7.3).
        self.provider = BrowserUseRuntimeProvider(self)
        self._engine_ok = False

    # ── the lifespan service ────────────────────────────────────────────

    async def start(self) -> None:
        """At startup: verify the engine, then close what the last process
        left running (never resumed)."""

        await self.engine.verify()
        self._engine_ok = True
        await self.recover()

    async def stop(self) -> None:
        await self.stop_all("shutdown")

    # ── start ───────────────────────────────────────────────────────────

    async def start_run(self, session: AsyncSession, *, principal: Principal, audit: AuditLogger,
                        spec: CompiledAgentSpec, run_budget: float) -> AgentRunRow:
        service = self._factory.service
        hosts = browse_hosts(spec.envelope_ceiling)
        if not hosts:
            raise BrowserRunRefused("no_hosts", "this agent names no host to browse")
        network = self._config.execution.network
        try:
            egress = policy_for_run(hosts, operator_destinations=network.default_destinations,
                                    operator_internet=network.default_internet)
        except ValueError:
            raise BrowserRunRefused("egress_not_permitted",
                                    "a host this agent browses is outside the server's egress policy",
                                    forbidden=True) from None
        scope = hosts_scope(hosts)
        security = RuntimeSecurityAdapter(core=self._core, session=session, audit=audit)
        if not await security.holds_standing_grant(principal=principal, graph_id=spec.graph_id,
                                                   capability=BROWSER_CAPABILITY, resource_scope=scope):
            raise BrowserRunRefused("capability_not_granted",
                                    "grant browser.session for these hosts before running this agent",
                                    forbidden=True)

        run_id, task_id = uuid.uuid4(), uuid.uuid4()
        deadline = self._factory.run_deadline(spec)
        run = await service.create_run(session, spec=spec, run_id=run_id)
        now = datetime.now(timezone.utc)
        session.add(AgentTask(task_id=task_id, user_id=principal.user_id, device_id=principal.device_id,
                              session_id=principal.session_id, graph_id=spec.graph_id,
                              status=AgentTaskStatus.RUNNING.value, mode=spec.run_mode.value, created_at=now,
                              updated_at=now))
        await session.flush()
        await service.run_started(session, run_id, task_id=task_id)
        # The run's activation comes from the owner's standing grant, for this
        # task only and never past the run's deadline; then the engine decides.
        await security.activate_for_task(principal=principal, task_id=task_id, capability=BROWSER_CAPABILITY,
                                         resource_scope=scope, expires_at=deadline)
        verdict = await security.authorize_action(ActionRequest(
            principal=principal, graph_id=spec.graph_id, task_id=task_id, capability=BROWSER_CAPABILITY,
            capability_operation=BROWSE, resource_type=ResourceType.TOOL_ACTION, operation=Operation.CREATE,
            resource_ref=None, arguments={"hosts": list(hosts)}, resource_scope=scope))
        if verdict.decision is not PermissionDecisionValue.ALLOW:
            await self._abandon(session, run_id, task_id, principal, "not_authorized")
            raise BrowserRunRefused("not_authorized", "browsing these hosts is not allowed for this run",
                                    forbidden=True)

        issued = await self._factory.gateway.issue(session, run, deadline=deadline)
        await self._audit(audit, principal, AuditAction.AGENT_TOKEN_ISSUED, f"agentrun:{run_id}:model,tool")
        try:
            sockets, scratch = prepare_workspace(self._run_dir, run_id)
        except ContainerError:
            await self._abandon(session, run_id, task_id, principal, "runtime_unavailable")
            raise BrowserRunRefused("runtime_unavailable", "the browser runtime is not available") from None
        input_text = self._factory.run_input(spec, run_id)
        live = _Live(
            run_id=run_id, agent_id=spec.agent_id, owner_user_id=principal.user_id, task_id=task_id,
            name=container_name(run_id), model_token=issued.model, deadline=_utc(deadline), socket_dir=sockets,
            scratch_dir=scratch,
            proxy=EgressProxy(binding=f"unix:{sockets / EGRESS_SOCKET}", policy=egress, resolve=proxy_resolver(),
                              connect=proxy_connector(), on_decision=lambda d: self._count(run_id, d)),
            listener=ModelGatewayListener(
                app=build_model_gateway_app(self._gateway,
                                            max_request_bytes=self._config.agents.model_gateway.max_request_bytes,
                                            run_id=run_id),
                binding=f"unix:{sockets / MODEL_SOCKET}"),
            hosts=hosts,
            max_steps=max(1, min(spec.bounds.max_model_calls, self._config.agent.bounds.max_model_calls)),
        )
        self.live[run_id] = live
        await self._audit(audit, principal, AuditAction.AGENT_RUN_STARTED, f"agentrun:{run_id}:{BROWSER_USE_RUNTIME_ID}")
        await session.commit()
        try:
            await live.listener.start()
            await live.proxy.start()
            # docs/29 §11.2: what the provider receives. The tool token is not
            # in it — a P2 runtime makes no tool call (register §2L).
            await self.provider.start_run(AgentRunContext(
                run_id=run_id, agent_id=spec.agent_id, version=spec.version, spec_hash=spec.spec_hash,
                input_text=input_text, deadline=deadline,
                model_endpoint=f"unix:{CONTAINER_SOCKETS}/{MODEL_SOCKET}", model_run_token=issued.model))
        except Exception as exc:  # noqa: BLE001 — anything that kept the container from starting
            logger.warning("browser run %s: the container did not start (%s)", run_id, type(exc).__name__,
                           exc_info=False)
            live.stop_reason = "runtime_unavailable"
            await self._finish(live, exit_code=None)
            raise BrowserRunRefused("runtime_unavailable", "the browser runtime is not available") from None
        live.supervisor = asyncio.create_task(self._supervise(live), name=f"browser-run-{run_id}")
        return await service.get_run(session, run_id) or run

    # ── supervision and the kill path ───────────────────────────────────

    async def _supervise(self, live: _Live) -> None:
        exit_code: int | None = None
        try:
            while True:
                exit_code = await self.engine.wait(live.name, timeout=self._poll)
                if exit_code is not None or live.stop_reason is not None:
                    break
                reason = await self._should_stop(live)
                if reason is not None:
                    live.stop_reason = reason
                    break
            # Stopped or exited on its own, the container is removed now —
            # never left for reconciliation to find.
            await self.engine.stop(live.name)
        except asyncio.CancelledError:
            live.stop_reason = live.stop_reason or "interrupted"
            with contextlib.suppress(Exception):
                await self.engine.stop(live.name)
            raise
        except Exception:  # noqa: BLE001 — a supervisor failure ends the run, never leaves it running
            logger.warning("browser run %s: supervision failed", live.run_id, exc_info=False)
            live.stop_reason = live.stop_reason or "runtime_crashed"
            with contextlib.suppress(Exception):
                await self.engine.stop(live.name)
        finally:
            await asyncio.shield(self._finish(live, exit_code=exit_code))

    async def _should_stop(self, live: _Live) -> str | None:
        if datetime.now(timezone.utc) >= live.deadline:
            return "wall_clock_timeout"
        async with self._storage.session() as session:
            if not await SupervisorGate(self._latch, session).submissions_open():
                return "emergency_stop"
            outcome = await self._factory.gateway.authenticate(
                session, run_id=live.run_id, agent_id=live.agent_id, purpose=RunTokenPurpose.MODEL,
                token=live.model_token, is_member=self._is_member(session))
        if isinstance(outcome, GatewayDenied):
            return {"run_not_running": "cancelled", "spec_changed": "spec_changed"}.get(outcome.code.value,
                                                                                         "agent_unavailable")
        return None

    async def stop_run(self, run_id: uuid.UUID, reason: str) -> bool:
        """The kill path for one run: `True` if it was live here."""

        live = self.live.get(run_id)
        if live is None:
            return False
        live.stop_reason = live.stop_reason or reason
        with contextlib.suppress(Exception):
            await self.engine.stop(live.name)
        if live.supervisor is not None:
            await asyncio.gather(live.supervisor, return_exceptions=True)
        return True

    async def stop_all(self, reason: str) -> int:
        stopped = 0
        for run_id in list(self.live):
            stopped += await self.stop_run(run_id, reason)
        return stopped

    # ── the provider's port (`server/agents/providers/browser_use.py`) ───

    async def launch(self, *, run_id: uuid.UUID, agent_id: uuid.UUID, env, task_input: str) -> str:
        live = self.live.get(run_id)
        if live is None or live.agent_id != agent_id:
            raise ContainerError("no such run")
        (live.scratch_dir / "task.json").write_text(task_document(task_input, live.hosts, max_steps=live.max_steps))
        return await self.engine.start(ContainerSpec(
            run_id=run_id, agent_id=agent_id, image=self._image, socket_dir=str(live.socket_dir),
            scratch_dir=str(live.scratch_dir), env=dict(env)))

    async def kill(self, run_id: uuid.UUID, reason: str) -> None:
        await self.stop_run(run_id, reason)

    async def status_of(self, run_id: uuid.UUID) -> AgentRunStatus:
        if run_id in self.live:
            return AgentRunStatus.RUNNING
        async with self._storage.session() as session:
            run = await session.get(AgentRunRow, run_id)
        return AgentRunStatus(run.status) if run is not None else AgentRunStatus.FAILED

    async def running(self) -> list[uuid.UUID]:
        return list(self.live)

    async def healthy(self) -> bool:
        return self._engine_ok

    # ── finish ──────────────────────────────────────────────────────────

    async def _finish(self, live: _Live, *, exit_code: int | None) -> None:
        if self.live.pop(live.run_id, None) is None:
            return
        with contextlib.suppress(Exception):
            await live.proxy.stop()
        with contextlib.suppress(Exception):
            await live.listener.stop()
        result = None
        path = live.scratch_dir / "result.json"
        with contextlib.suppress(OSError):
            if path.is_file():
                result = read_result(path.read_bytes()[: 64 * 1024 + 1])
        if live.stop_reason is not None:
            status = (AgentTaskStatus.CANCELLED if live.stop_reason in ("owner_stop", "cancelled", "agent_unavailable",
                                                                       "agent_paused", "agent_deleted")
                      else AgentTaskStatus.FAILED)
            failure: str | None = live.stop_reason
        elif result is None:
            status, failure = AgentTaskStatus.FAILED, "runtime_crashed"
        elif result.status == "completed" and result.final is not None:
            status, failure = AgentTaskStatus.COMPLETED, None
        else:
            status, failure = AgentTaskStatus.FAILED, result.error or "runtime_failed"
        response = result.final if result is not None and status is AgentTaskStatus.COMPLETED else None
        try:
            async with self._storage.session() as session:
                await self._close(session, live, status, failure, response)
                await session.commit()
        except Exception:  # noqa: BLE001 — the workspace still goes
            logger.warning("browser run %s: could not record its end", live.run_id, exc_info=False)
        finally:
            remove_workspace(self._run_dir, live.run_id)

    async def _close(self, session: AsyncSession, live: _Live, status: AgentTaskStatus, failure: str | None,
                     response: str | None) -> None:
        from server.composition.agents import inbox_body

        service = self._factory.service
        audit = AuditLogger(session, request_id=uuid.uuid4())
        task = await session.get(AgentTask, live.task_id, populate_existing=True)
        principal = await row_principal(session, task) if task is not None else None
        bytes_total = 0
        if principal is not None:
            stats = live.proxy.stats
            bytes_total = stats.bytes_up + stats.bytes_down
            usage = RuntimeUsageAdapter(policy=self._core_usage(), session=session, request_id=uuid.uuid4())
            usage_id = await usage.record(principal=principal, graph_id=task.graph_id, kind=UsageKind.TOOL_CALL,
                                          units=bytes_total, estimated_cost=0.0, provider=None, model=None,
                                          tool_id=BROWSER_CAPABILITY)
            await service.attribute_usage(session, live.run_id, usage_id)
            security = RuntimeSecurityAdapter(core=self._core, session=session, audit=audit)
            await security.deactivate_task(principal=principal, task_id=live.task_id)
        cost = await service.run_usage(session, live.run_id)
        run = await service.run_finished(session, live.run_id, status=status, failure=failure, cost=cost,
                                         revoke_reason=failure or "finished")
        if task is not None and task.finished_at is None:
            task.status = status.value
            task.failure_code = failure if failure in _TASK_FAILURES else (None if failure is None else "agent_unavailable")
            task.finished_at = task.updated_at = datetime.now(timezone.utc)
        summary = ",".join(f"{k}={v}" for k, v in sorted(live.decisions.items())) or "none"
        await audit.record(actor=AuditActor.AGENT, action=AuditAction.AGENT_EGRESS_SUMMARY,
                           resource=f"agentrun:{live.run_id}:egress:{summary}:bytes={bytes_total}"[:128],
                           result=AuditResult.SUCCESS, user_id=live.owner_user_id)
        await audit.record(actor=AuditActor.AGENT, action=AuditAction.AGENT_RUN_FINISHED,
                           resource=f"agentrun:{live.run_id}:{status.value}{':' + failure if failure else ''}"[:128],
                           result=AuditResult.SUCCESS if status is AgentTaskStatus.COMPLETED else AuditResult.FAILURE,
                           user_id=live.owner_user_id)
        if run is not None and run.finished_at is not None:
            body, withheld, truncated = inbox_body(response)
            item = await service.inbox_deliver(session, run, body=body, withheld=withheld, truncated=truncated)
            if item is not None:
                await audit.record(actor=AuditActor.AGENT, action=AuditAction.AGENT_INBOX_DELIVERED,
                                   resource=f"agentinbox:{item.item_id}", result=AuditResult.SUCCESS,
                                   user_id=live.owner_user_id)

    async def _abandon(self, session: AsyncSession, run_id: uuid.UUID, task_id: uuid.UUID, principal: Principal,
                       failure: str) -> None:
        await self._factory.service.run_finished(session, run_id, status=AgentTaskStatus.FAILED, failure=failure,
                                                 cost=0.0, revoke_reason=failure)
        task = await session.get(AgentTask, task_id)
        if task is not None:
            task.status = AgentTaskStatus.FAILED.value
            task.finished_at = task.updated_at = datetime.now(timezone.utc)
        security = RuntimeSecurityAdapter(core=self._core, session=session,
                                          audit=AuditLogger(session, request_id=uuid.uuid4()))
        await security.deactivate_task(principal=principal, task_id=task_id)
        await session.commit()

    # ── recovery ────────────────────────────────────────────────────────

    async def recover(self) -> list[uuid.UUID]:
        """At startup: every unfinished browser run not supervised here is
        closed `failed: interrupted` — never resumed."""

        closed = []
        async with self._storage.session() as session:
            rows = (await session.execute(select(AgentRunRow).where(AgentRunRow.finished_at.is_(None)))).scalars().all()
            for run in rows:
                if run.run_id in self.live:
                    continue
                loaded = await self._factory.service.load(session, run.agent_id, fresh=True)
                spec = loaded[1] if loaded is not None else None
                if spec is None or spec.selection.runtime_id != BROWSER_USE_RUNTIME_ID:
                    continue
                await self._factory.service.run_finished(session, run.run_id, status=AgentTaskStatus.FAILED,
                                                         failure="interrupted", cost=run.cost_total,
                                                         revoke_reason="interrupted")
                if run.task_id is not None:
                    task = await session.get(AgentTask, run.task_id)
                    if task is not None and task.finished_at is None:
                        task.status = AgentTaskStatus.FAILED.value
                        task.finished_at = task.updated_at = datetime.now(timezone.utc)
                closed.append(run.run_id)
            await session.commit()
        for run_id in closed:
            remove_workspace(self._run_dir, run_id)
        return closed

    # ── helpers ─────────────────────────────────────────────────────────

    def _count(self, run_id: uuid.UUID, decision: ProxyDecision) -> None:
        live = self.live.get(run_id)
        if live is not None:
            live.decisions[decision.reason] = live.decisions.get(decision.reason, 0) + 1

    def _is_member(self, session: AsyncSession):
        async def is_member(graph_id: uuid.UUID, user_id: uuid.UUID) -> bool:
            return await self._core.graph_repository.is_active_member(session, graph_id=graph_id, user_id=user_id)
        return is_member

    def _core_usage(self) -> Any:  # the same usage policy the gateway meters with
        return self._gateway.usage_policy

    @staticmethod
    async def _audit(audit: AuditLogger, principal: Principal, action: AuditAction, resource: str) -> None:
        await audit.record(actor=AuditActor.USER, action=action, resource=resource[:128], result=AuditResult.SUCCESS,
                           user_id=principal.user_id, device_id=principal.device_id, session_id=principal.session_id)


_TASK_FAILURES = frozenset({"wall_clock_timeout", "emergency_stop", "agent_unavailable"})

__all__ = ["BrowserRunRefused", "BrowserRuns", "engine_for", "proxy_connector", "proxy_resolver"]
