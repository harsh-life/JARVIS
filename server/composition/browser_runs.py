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

**The kill path** (`cancel`, every owner and operator stop): inside the
caller's own transaction the run's tokens are revoked **first** (AGENT-T23 —
from then on the gateway admits nothing of this run, whatever the container
is still doing), the run and its task are closed and the task's activation
removed; only then is the container told to stop (stop, kill after the
grace, remove) — without waiting for it, so a slow container never holds a
request. The run's supervisor finishes the rest.

**Supervision** (one task per run), every `poll_seconds`, until the
container exits: the run's model token is re-authenticated through the same
core as every gateway request (a stop, a pause, a delete, a change, a
revocation, a lost membership all end it), the run's task is re-read (a stop
that closed it by any other path ends it), the global stop is read, and the
deadline checked. A gateway refusal that ends a run — a budget, the
model-call ceiling — ends it at once (`run_ending_refusal`). Any of them
stops the container.

**Finish**: the result file is read as untrusted data (`read_result`); the
run ends `completed` or `failed` with JARVIS's own code (`runtime_crashed`,
`runtime_failed`, `result_unreadable`, `wall_clock_timeout`,
`emergency_stop`, `max_model_calls`, …) — never a word of the runtime's; its
tokens
are revoked, its task closed and its activation removed; the result goes to
the owner's inbox through the ordinary path (scrubbed, bounded) and nowhere
else; usage (model calls, already metered by the gateway, and the tunnel's
bytes) is attributed to the run; the egress decisions are summarized in the
audit trail; sockets closed, workspace removed.

**Recovery**: at startup, a browser run left unfinished (its supervisor died
with the process) is closed `failed: interrupted`, tokens revoked, task
closed, activation removed, workspace deleted — never resumed; the container
reconciler removes its container.
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
from server.agents.browser import (
    BROWSE,
    BROWSER_CAPABILITY,
    UNREADABLE,
    browse_hosts,
    hosts_scope,
    read_result,
    task_document,
)
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
from server.gateway.model_gateway_port import Disconnected, ModelGatewayReply
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


# Gateway refusals that end the run they refused (docs/29 §12.3): a ceiling
# the run hit is not retried into. Anything else (a rate limit, a provider
# failure, a malformed request) is the runtime's to handle within the run.
_RUN_ENDING = {
    "budget_exceeded": "budget_exceeded",
    "agent_budget_exhausted": "budget_exceeded",
    "max_model_calls": "max_model_calls",
}


def run_ending_refusal(reply: ModelGatewayReply) -> str | None:
    if reply.status < 400:
        return None
    error = reply.payload.get("error") if isinstance(reply.payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return _RUN_ENDING.get(code) if isinstance(code, str) else None


# A run's failure code, as its task records it (`AgentFailureCode`).
_TASK_FAILURE = {
    "wall_clock_timeout": "wall_clock_timeout",
    "emergency_stop": "emergency_stop",
    "budget_exceeded": "budget_exceeded",
    "max_model_calls": "max_model_calls_exceeded",
    "spec_changed": "spec_changed",
    "principal_revoked": "principal_revoked",
    "agent_unavailable": "agent_unavailable",
    "token_revoked": "agent_unavailable",
    "deleted": "agent_unavailable",
}


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
    device_id: uuid.UUID | None
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
    # How the run ends when JARVIS ended it (status, JARVIS's code); `None`
    # while only the container's own exit and result can say.
    outcome: tuple[AgentTaskStatus, str] | None = None
    supervisor: asyncio.Task | None = None
    killer: asyncio.Task | None = None


class _RunGateway:
    """The run's own face of the Model Gateway: the same gateway, plus the
    run's end when it refuses this run for a ceiling it hit."""

    def __init__(self, gateway: "HttpModelGateway", on_end) -> None:
        self._gateway = gateway
        self._on_end = on_end

    async def chat_completion(self, *, authorization: str | None, body: bytes, disconnected: Disconnected,
                              run_id: uuid.UUID | None = None) -> ModelGatewayReply:
        reply = await self._gateway.chat_completion(authorization=authorization, body=body,
                                                    disconnected=disconnected, run_id=run_id)
        ending = run_ending_refusal(reply)
        if ending is not None:
            self._on_end(ending)
        return reply


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
            device_id=principal.device_id, name=container_name(run_id), model_token=issued.model, deadline=_utc(deadline), socket_dir=sockets,
            scratch_dir=scratch,
            proxy=EgressProxy(binding=f"unix:{sockets / EGRESS_SOCKET}", policy=egress, resolve=proxy_resolver(),
                              connect=proxy_connector(), on_decision=lambda d: self._count(run_id, d)),
            listener=ModelGatewayListener(
                app=build_model_gateway_app(_RunGateway(self._gateway, lambda code: self._end(run_id, code)),
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
            live.outcome = (AgentTaskStatus.FAILED, "runtime_unavailable")
            await self._finish(live, exit_code=None)
            raise BrowserRunRefused("runtime_unavailable", "the browser runtime is not available") from None
        live.supervisor = asyncio.create_task(self._supervise(live), name=f"browser-run-{run_id}")
        return await service.get_run(session, run_id) or run

    # ── the kill path ───────────────────────────────────────────────────

    async def cancel(self, session: AsyncSession, audit: AuditLogger, *, run_id: uuid.UUID, reason: str,
                     status: AgentTaskStatus, actor: AuditActor, actor_user: uuid.UUID | None = None) -> bool:
        """Stop one live run, inside the caller's transaction (see the module
        docstring). `True` if it was live here. Never waits for the
        container: the supervisor finishes the run."""

        live = self.live.get(run_id)
        if live is None:
            return False
        service = self._factory.service
        # AGENT-T23: the tokens first — from here the gateway admits nothing.
        if await self._factory.gateway.revoke_run(session, run_id, reason):
            await audit.record(actor=actor, action=AuditAction.AGENT_TOKEN_REVOKED,
                               resource=f"agentrun:{run_id}:{reason}"[:128], result=AuditResult.SUCCESS,
                               user_id=actor_user)
        await service.run_finished(session, run_id, status=status, failure=reason,
                                   cost=await service.run_usage(session, run_id), revoke_reason=reason)
        task = await session.get(AgentTask, live.task_id)
        principal = await row_principal(session, task) if task is not None else None
        if principal is not None:
            await RuntimeSecurityAdapter(core=self._core, session=session, audit=audit).deactivate_task(
                principal=principal, task_id=live.task_id)
        await self._close_task(session, live.task_id, status, reason)
        live.outcome = live.outcome or (status, reason)
        self._signal(live, reason)
        return True

    def _end(self, run_id: uuid.UUID, reason: str) -> None:
        """A run JARVIS ended from inside the request path (a gateway
        ceiling): failed with its code, the container told to stop."""

        live = self.live.get(run_id)
        if live is not None:
            live.outcome = live.outcome or (AgentTaskStatus.FAILED, reason)
            self._signal(live, reason)

    def _signal(self, live: _Live, reason: str) -> None:
        live.stop_reason = live.stop_reason or reason
        if live.killer is None:
            live.killer = asyncio.create_task(self._kill(live), name=f"browser-kill-{live.run_id}")

    async def _kill(self, live: _Live) -> None:
        with contextlib.suppress(Exception):
            await self.engine.stop(live.name)

    async def stop_run(self, run_id: uuid.UUID, reason: str, *,
                       status: AgentTaskStatus = AgentTaskStatus.FAILED) -> bool:
        """Stop one run from outside any request (the provider's cancel, a
        shutdown) and wait for its end: `True` if it was live here."""

        live = self.live.get(run_id)
        if live is None:
            return False
        live.outcome = live.outcome or (status, reason)
        self._signal(live, reason)
        if live.supervisor is not None:
            await asyncio.gather(live.supervisor, return_exceptions=True)
        return True

    async def stop_all(self, reason: str) -> int:
        stopped = 0
        for run_id in list(self.live):
            stopped += await self.stop_run(run_id, reason)
        return stopped

    def matching(self, *, task_id: uuid.UUID | None = None, user_id: uuid.UUID | None = None,
                 device_id: uuid.UUID | None = None, agent_id: uuid.UUID | None = None) -> list[uuid.UUID]:
        """The live runs an operator stop names (no filter: every one)."""

        return [run_id for run_id, live in self.live.items()
                if (task_id is None or live.task_id == task_id)
                and (user_id is None or live.owner_user_id == user_id)
                and (device_id is None or live.device_id == device_id)
                and (agent_id is None or live.agent_id == agent_id)]

    # ── supervision ─────────────────────────────────────────────────────

    async def _supervise(self, live: _Live) -> None:
        exit_code: int | None = None
        try:
            while True:
                exit_code = await self.engine.wait(live.name, timeout=self._poll)
                if exit_code is not None or live.stop_reason is not None:
                    break
                ended = await self._should_stop(live)
                if ended is not None:
                    live.outcome = live.outcome or ended
                    live.stop_reason = live.stop_reason or ended[1]
                    break
            # Stopped or exited on its own, the container is removed now —
            # never left for reconciliation to find.
            await self.engine.stop(live.name)
        except asyncio.CancelledError:
            live.stop_reason = live.stop_reason or "interrupted"
            live.outcome = live.outcome or (AgentTaskStatus.FAILED, "interrupted")
            with contextlib.suppress(Exception):
                await self.engine.stop(live.name)
            raise
        except Exception:  # noqa: BLE001 — a supervisor failure ends the run, never leaves it running
            logger.warning("browser run %s: supervision failed", live.run_id, exc_info=False)
            live.stop_reason = live.stop_reason or "runtime_crashed"
            live.outcome = live.outcome or (AgentTaskStatus.FAILED, "runtime_crashed")
            with contextlib.suppress(Exception):
                await self.engine.stop(live.name)
        finally:
            await asyncio.shield(self._finish(live, exit_code=exit_code))

    async def _should_stop(self, live: _Live) -> tuple[AgentTaskStatus, str] | None:
        failed = AgentTaskStatus.FAILED
        if datetime.now(timezone.utc) >= live.deadline:
            return failed, "wall_clock_timeout"
        async with self._storage.session() as session:
            if not await SupervisorGate(self._latch, session).submissions_open():
                return failed, "emergency_stop"
            task = await session.get(AgentTask, live.task_id, populate_existing=True)
            if task is None or task.finished_at is not None:
                # Closed by another path (an operator's stop of the task):
                # the task's own word stands.
                return failed, (task.failure_code if task is not None and task.failure_code else "emergency_stop")
            outcome = await self._factory.gateway.authenticate(
                session, run_id=live.run_id, agent_id=live.agent_id, purpose=RunTokenPurpose.MODEL,
                token=live.model_token, is_member=self._is_member(session))
        if isinstance(outcome, GatewayDenied):
            return failed, {"run_not_running": "cancelled", "spec_changed": "spec_changed",
                            "invalid_run_token": "token_revoked"}.get(outcome.code.value, "agent_unavailable")
        return None

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
        # The provider's cancel (docs/29 §7.3) is the owner's kind of stop.
        await self.stop_run(run_id, reason, status=AgentTaskStatus.CANCELLED)

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
        failure: str | None
        if live.outcome is not None:
            status, failure = live.outcome
        elif result is None:
            status, failure = AgentTaskStatus.FAILED, "runtime_crashed"
        elif result.status == "completed" and result.final is not None:
            status, failure = AgentTaskStatus.COMPLETED, None
        elif result is UNREADABLE:
            status, failure = AgentTaskStatus.FAILED, "result_unreadable"
        else:
            # The runtime's own words for its failure are data, never a code
            # of JARVIS's (they could name any stop at all).
            status, failure = AgentTaskStatus.FAILED, "runtime_failed"
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
        if run is not None:
            # A run a stop already closed keeps that stop's word.
            run.cost_total = max(run.cost_total, cost)
            status = _TASK_STATUS.get(run.status, status)
            failure = run.failure_code
        await self._close_task(session, live.task_id, status, failure)
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

    @staticmethod
    async def _close_task(session: AsyncSession, task_id: uuid.UUID, status: AgentTaskStatus,
                          failure: str | None) -> None:
        task = await session.get(AgentTask, task_id, populate_existing=True)
        if task is not None and task.finished_at is None:
            task.status = status.value
            task.failure_code = (None if failure is None or status is AgentTaskStatus.COMPLETED
                                 or (status is AgentTaskStatus.CANCELLED and failure == "owner_stop")
                                 else _TASK_FAILURE.get(failure, "internal_error"))
            task.finished_at = task.updated_at = datetime.now(timezone.utc)
        await session.flush()

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
                audit = AuditLogger(session, request_id=uuid.uuid4())
                if run.task_id is not None:
                    task = await session.get(AgentTask, run.task_id)
                    principal = await row_principal(session, task) if task is not None else None
                    if principal is not None:
                        await RuntimeSecurityAdapter(core=self._core, session=session, audit=audit).deactivate_task(
                            principal=principal, task_id=run.task_id)
                    await self._close_task(session, run.task_id, AgentTaskStatus.FAILED, "interrupted")
                await audit.record(actor=AuditActor.SYSTEM, action=AuditAction.AGENT_RUN_FINISHED,
                                   resource=f"agentrun:{run.run_id}:failed:interrupted", result=AuditResult.FAILURE,
                                   user_id=run.owner_user_id)
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


_TASK_STATUS = {"completed": AgentTaskStatus.COMPLETED, "cancelled": AgentTaskStatus.CANCELLED,
                "failed": AgentTaskStatus.FAILED}

__all__ = ["BrowserRunRefused", "BrowserRuns", "engine_for", "proxy_connector", "proxy_resolver",
           "run_ending_refusal"]
