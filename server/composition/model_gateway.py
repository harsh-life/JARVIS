"""The HTTP Model Gateway (docs/29 §12, Phase 6 slice 6A) and its internal listener.

`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]` (docs/29). An external agent
runtime (OD-AF-6: Browser Use, P2) never holds a provider key: its model
calls come here, as OpenAI-compatible requests carrying the run's model
token. Every request passes, in order (docs/29 §12.2):

    the token: well-formed, known, the model gateway's, bound to a live run
        of an active agent on exactly the run's version and hash
        (`AgentGateway.authenticate` — the core the native runtime uses)
    the request: closed shape; `stream: true` → 400
    the alias: exactly `agent-model` → else 403
    the run: running, its task running, not past its deadline, the global
        stop not engaged → else 409
    the profile: the spec's selected one, still enabled and permitted to the
        owner now (`AgentFactory.model_screen` — shared with native runs)
    the bounds: the run's model calls → 429
    the money: the run's budget, the owner's budget and rates (13), the
        agent's month → 429
    the provider: the selected profile's configured entry, its key resolved
        inside `server.models` (06 §1) → 503 if it fails
    the answer: scrubbed of anything secret-shaped, bounded with a marker,
        naming only the alias
    the record: a UsageEvent as the run's owner, joined to the run

Every refusal is audited (`agent.gateway.denied`). Nothing is cached: each
request reads the token, run, task, agent and budgets fresh, so a revocation,
a stop, a pause or a change made anywhere applies to the next request. No
transaction is open while the provider is called (H-1). A caller that goes
away cancels the provider call, which is metered at zero units.

The gateway authorizes no effect: a model call changes nothing outside the
run. The runtime calling it is untrusted; it supplies messages and an alias,
and JARVIS decides the rest.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from server.agent.agent_run import ModelCallFacts
from server.agent.ports import UsageLimitReached
from server.agent.records import row_principal
from server.agents.gateway import tokens as run_tokens
from server.agents.gateway.core import GatewayContext, GatewayDenied
from server.agents.gateway.model_gateway import (
    AGENT_MODEL_ALIAS,
    bearer_token,
    bounded_completion,
    chat_request_refusal,
    http_status,
)
from server.composition.latch import InProcessLatch, SupervisorGate
from server.composition.models import ConfiguredModelResolver
from server.composition.usage_port import RuntimeUsageAdapter
from server.gateway.model_gateway_port import CLIENT_CLOSED, Disconnected, ModelGatewayReply
from server.models.provider import ChatMessage, ModelProvider, ModelUnavailable
from server.net.listen import internal_listen_socket, remove_internal_socket
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.security.secret_patterns import redact_secrets
from server.security.usage import UsagePolicy
from server.storage.models import AgentRunRow, AgentTask, StandingDelegationRow
from shared.schemas.agent import AgentTaskStatus
from shared.schemas.agent_factory import (
    AgentGatewayErrorCode,
    AgentRunStatus,
    ChatCompletionRequest,
    CompiledAgentSpec,
    RunTokenPurpose,
)
from shared.schemas.enums import AuditActor, AuditResult, UsageKind

if TYPE_CHECKING:
    from server.composition.agents import AgentFactory
    from server.config.schema import AppConfig
    from server.gateway.security import SecurityCore
    from server.storage import StorageBackend

logger = logging.getLogger(__name__)

# How often an in-flight provider call checks whether its caller is gone.
DISCONNECT_POLL_SECONDS = 0.25

_MESSAGES = {
    "invalid_run_token": "the run token is missing, unknown, expired or revoked",
    "schema_invalid": "the request is not a supported chat completion request",
    "stream_unsupported": "stream: true is not supported; send stream: false",
    "n_unsupported": "only n = 1 is supported",
    "model_not_allowed": f"the only model this run may request is {AGENT_MODEL_ALIAS!r}",
    "run_not_running": "the run is not running",
    "budget_exceeded": "a budget would be exceeded",
    "agent_budget_exhausted": "this agent's monthly budget would be exceeded",
    "rate_limited": "a rate limit was reached",
    "max_model_calls": "this run has made all the model calls it may",
    "dependency_unavailable": "the model is unavailable",
}


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class _ContextHint:
    """Which run a refused token was for, when the store knows it — for the
    audit trail only, never for a decision."""

    run_id: uuid.UUID
    agent_id: uuid.UUID


class _Refused(Exception):
    def __init__(self, code: AgentGatewayErrorCode, detail: str, *, reason: str | None = None,
                 hint: _ContextHint | None = None) -> None:
        super().__init__(code.value)
        self.code = code
        self.detail = detail
        self.reason = reason   # a more specific message key than the code's
        self.hint = hint


class HttpModelGateway:
    """`ModelGatewayPort` (server/gateway/model_gateway_port.py)."""

    def __init__(self, *, factory: "AgentFactory", storage: "StorageBackend", core: "SecurityCore",
                 usage: UsagePolicy, latch: InProcessLatch, config: "AppConfig",
                 provider_factory: Callable[..., ModelProvider]) -> None:
        self._factory = factory
        self._storage = storage
        self._core = core
        self._usage = usage
        self.latch = latch
        self._config = config
        self._provider_factory = provider_factory
        self._max_completion_chars = config.agents.model_gateway.max_completion_chars

    # ── the request ─────────────────────────────────────────────────────

    async def chat_completion(self, *, authorization: str | None, body: bytes,
                              disconnected: Disconnected) -> ModelGatewayReply:
        request_id = uuid.uuid4()
        async with self._storage.session() as session:
            audit = AuditLogger(session, request_id=request_id)
            usage = RuntimeUsageAdapter(policy=self._usage, session=session, request_id=request_id)
            context: GatewayContext | None = None
            try:
                context = await self._authenticate(session, authorization)
                request = self._parse(body)
                admitted = await self._admit(session, usage, context, request)
            except _Refused as refused:
                await session.rollback()
                await self._audit_denied(session, audit, context, refused)
                await session.commit()
                return self._refusal(refused)
            provider, principal, graph_id, messages, deadline = admitted
            # H-1: nothing is held open while the provider answers; the call
            # just admitted stays counted until its ledger row commits.
            with usage.holding_admissions():
                await session.commit()
            return await self._invoke(session, audit, usage, context, provider, principal, graph_id, messages,
                                      deadline, disconnected)

    async def _authenticate(self, session: AsyncSession, authorization: str | None) -> GatewayContext:
        token = bearer_token(authorization)
        invalid = AgentGatewayErrorCode.INVALID_RUN_TOKEN
        if token is None or not run_tokens.well_formed_token(token):
            raise _Refused(invalid, "malformed")
        row = await self._factory.service.run_token(session, run_tokens.token_digest(token))
        if row is None:
            raise _Refused(invalid, "unknown")
        outcome = await self._factory.gateway.authenticate(
            session, run_id=row.run_id, agent_id=row.agent_id, purpose=RunTokenPurpose.MODEL, token=token,
            is_member=self._is_member(session))
        if isinstance(outcome, GatewayDenied):
            raise _Refused(outcome.code, outcome.detail, hint=_ContextHint(row.run_id, row.agent_id))
        return outcome

    @staticmethod
    def _parse(body: bytes) -> ChatCompletionRequest:
        schema = AgentGatewayErrorCode.SCHEMA_INVALID
        try:
            request = ChatCompletionRequest.model_validate(json.loads(body))
        except (ValueError, ValidationError, TypeError):
            raise _Refused(schema, "body") from None
        refused = chat_request_refusal(request)
        if refused is not None:
            raise _Refused(schema, refused, reason=refused)
        return request

    async def _admit(self, session: AsyncSession, usage: RuntimeUsageAdapter, context: GatewayContext,
                     request: ChatCompletionRequest):
        not_running = AgentGatewayErrorCode.RUN_NOT_RUNNING
        if request.model != AGENT_MODEL_ALIAS:
            raise _Refused(AgentGatewayErrorCode.MODEL_NOT_ALLOWED, "alias")
        # 18 §5: the operator's global stop refuses every model call too.
        if not await SupervisorGate(self.latch, session).submissions_open():
            raise _Refused(not_running, "halted")
        run = await session.get(AgentRunRow, context.run_id, populate_existing=True)
        if run is None or run.status != AgentRunStatus.RUNNING.value or run.task_id is None:
            raise _Refused(not_running, "run_status")
        task = await session.get(AgentTask, run.task_id, populate_existing=True)
        if task is None or task.status != AgentTaskStatus.RUNNING.value or task.user_id != context.owner_user_id:
            raise _Refused(not_running, "task_status")
        screened = await self._factory.model_screen(session, ModelCallFacts(alias=request.model), context)
        if screened is not None:
            raise _Refused(screened.code, screened.detail)
        loaded = await self._factory.service.load(session, context.agent_id, fresh=True)
        if loaded is None or loaded[1] is None or loaded[1].spec_hash != context.spec_hash:
            raise _Refused(AgentGatewayErrorCode.AGENT_UNAVAILABLE, "spec")
        spec = loaded[1]
        deadline = _utc(run.started_at) + timedelta(seconds=self._run_seconds(spec))
        if datetime.now(timezone.utc) >= deadline:
            raise _Refused(not_running, "deadline")
        limit = min(self._config.agent.bounds.max_model_calls, spec.bounds.max_model_calls)
        if task.model_calls >= limit:
            raise _Refused(AgentGatewayErrorCode.MAX_MODEL_CALLS, "limit")

        principal = await row_principal(session, task)
        provider = await self._provider(session, principal, task.graph_id, spec)
        messages = [ChatMessage(m.role, m.text) for m in request.messages]
        projected = provider.spec.projected_cost(prompt_chars=sum(len(m.content) for m in messages))
        await self._check_money(session, usage, run, spec, principal, projected)
        # The bound, taken atomically: concurrent requests cannot overrun it.
        if not await self._factory.service.count_model_call(session, task.task_id, limit=limit):
            raise _Refused(AgentGatewayErrorCode.MAX_MODEL_CALLS, "limit")
        return provider, principal, task.graph_id, messages, deadline

    def _run_seconds(self, spec: CompiledAgentSpec) -> float:
        return min(float(spec.bounds.max_run_seconds), self._config.agent.bounds.wall_clock_timeout_seconds)

    async def _provider(self, session: AsyncSession, principal, graph_id, spec: CompiledAgentSpec) -> ModelProvider:
        """docs/29 §7.4: the selected profile's configured entry, built and
        its key resolved exactly as any server-configured worker (06 §1)."""

        profile = self._factory.registries.model_profiles.get(spec.selection.model_profile_id)
        if profile is None:
            raise _Refused(AgentGatewayErrorCode.MODEL_NOT_ALLOWED, "profile_unavailable")
        resolver = ConfiguredModelResolver(config=self._config, session=session,
                                           graph_repository=self._core.graph_repository,
                                           provider_factory=self._provider_factory)
        try:
            resolved = await resolver.resolve(principal=principal, graph_id=graph_id,
                                              agent_model_ref=profile.profile.model_ref)
        except ModelUnavailable:
            raise _Refused(AgentGatewayErrorCode.DEPENDENCY_UNAVAILABLE, "unresolved") from None
        return resolved.chain[0]

    async def _check_money(self, session: AsyncSession, usage: RuntimeUsageAdapter, run: AgentRunRow,
                           spec: CompiledAgentSpec, principal, projected: float) -> None:
        """docs/29 §12.2 usage precheck: the run's budget, the owner's budget
        and rates (13), then the agent's month (read live)."""

        delegation = None
        if run.delegation_id is not None:
            delegation = await session.get(StandingDelegationRow, run.delegation_id, populate_existing=True)
            if delegation is None:
                raise _Refused(AgentGatewayErrorCode.AGENT_UNAVAILABLE, "delegation")
        if projected > 0:
            run_budget = min(self._config.agent.bounds.per_task_budget, spec.budget.per_run,
                             delegation.budget_per_run if delegation is not None else spec.budget.per_run)
            spent = await self._factory.service.run_usage(session, run.run_id)
            if spent + projected > run_budget:
                raise _Refused(AgentGatewayErrorCode.BUDGET_EXCEEDED, "per_run_budget")
        try:
            await usage.precheck(principal=principal, projected_cost=projected)
        except UsageLimitReached as exc:
            code = (AgentGatewayErrorCode.BUDGET_EXCEEDED if exc.is_budget
                    else AgentGatewayErrorCode.RATE_LIMITED)
            raise _Refused(code, exc.limit) from None
        if projected > 0:
            refused = await self._factory.agent_budget(session, agent_id=run.agent_id,
                                                       delegation_id=run.delegation_id, projected_cost=projected)
            if refused is not None:
                raise _Refused(AgentGatewayErrorCode(refused), "agent_month")

    # ── the provider call ───────────────────────────────────────────────

    async def _invoke(self, session: AsyncSession, audit: AuditLogger, usage: RuntimeUsageAdapter,
                      context: GatewayContext,
                      provider: ModelProvider, principal, graph_id, messages: list[ChatMessage],
                      deadline: datetime, disconnected: Disconnected) -> ModelGatewayReply:
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        timeout = max(0.1, min(float(provider.spec.timeout_seconds), remaining))
        call = asyncio.ensure_future(provider.invoke(messages, timeout=timeout))
        gone = False
        try:
            while not call.done():
                done, _ = await asyncio.wait({call}, timeout=min(DISCONNECT_POLL_SECONDS, timeout))
                if done:
                    break
                if await disconnected():
                    gone = True
                    break
                if datetime.now(timezone.utc) >= deadline:
                    break
        finally:
            if not call.done():
                call.cancel()
                with contextlib.suppress(BaseException):
                    await call
        result = None
        if not gone and call.done() and not call.cancelled():
            try:
                result = call.result()
            except (ModelUnavailable, asyncio.TimeoutError):
                result = None
            except Exception:  # noqa: BLE001 — a provider's own error never reaches the runtime
                logger.warning("model gateway: the provider call failed", exc_info=False)
                result = None
        if result is None:
            # Every attempt is metered — a failed or cancelled one at zero.
            await self._meter(session, usage, context, provider, principal, graph_id, units=0, cost=0.0)
            if gone:
                return ModelGatewayReply(status=CLIENT_CLOSED)
            refused = _Refused(AgentGatewayErrorCode.DEPENDENCY_UNAVAILABLE, "provider")
            await self._audit_denied(session, audit, context, refused)
            await session.commit()
            return self._refusal(refused)
        cost = provider.spec.pricing.cost(prompt_tokens=result.prompt_tokens,
                                          completion_tokens=result.completion_tokens)
        await self._meter(session, usage, context, provider, principal, graph_id,
                          units=result.total_tokens, cost=cost)
        content, truncated = bounded_completion(redact_secrets(result.content)[0],
                                                max_chars=self._max_completion_chars)
        return ModelGatewayReply(status=200, payload={
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": AGENT_MODEL_ALIAS,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "length" if truncated else "stop"}],
            "usage": {"prompt_tokens": max(0, result.prompt_tokens),
                      "completion_tokens": max(0, result.completion_tokens),
                      "total_tokens": result.total_tokens},
        })

    async def _meter(self, session: AsyncSession, usage: RuntimeUsageAdapter, context: GatewayContext,
                     provider: ModelProvider, principal, graph_id, *, units: int, cost: float) -> None:
        usage_id = await usage.record(principal=principal, graph_id=graph_id, kind=UsageKind.MODEL_CALL,
                                      units=units, estimated_cost=cost, provider=provider.spec.provider,
                                      model=provider.spec.model)
        await self._factory.service.attribute_usage(session, context.run_id, usage_id)
        await session.commit()

    # ── refusals ────────────────────────────────────────────────────────

    def _is_member(self, session: AsyncSession):
        async def is_member(graph_id: uuid.UUID, user_id: uuid.UUID) -> bool:
            return await self._core.graph_repository.is_active_member(session, graph_id=graph_id, user_id=user_id)
        return is_member

    @staticmethod
    def _refusal(refused: _Refused) -> ModelGatewayReply:
        status, code = http_status(refused.code)
        message = _MESSAGES.get(refused.reason or code, _MESSAGES.get(code, "refused"))
        return ModelGatewayReply(status=status, payload={"error": {"code": code, "message": message}})

    @staticmethod
    async def _audit_denied(session: AsyncSession, audit: AuditLogger, context: GatewayContext | None,
                            refused: _Refused) -> None:
        hint = refused.hint
        run_id = context.run_id if context is not None else (hint.run_id if hint is not None else None)
        owner = context.owner_user_id if context is not None else None
        if owner is None and run_id is not None:
            run = await session.get(AgentRunRow, run_id)
            owner = run.owner_user_id if run is not None else None
        _status, code = http_status(refused.code)
        subject = f"agentrun:{run_id}" if run_id is not None else "gateway"
        await audit.record(actor=AuditActor.AGENT, action=AuditAction.AGENT_GATEWAY_DENIED,
                           resource=f"{subject}:model:{code}:{refused.detail}"[:128],
                           result=AuditResult.BLOCKED, user_id=owner)


# ── the internal listener ───────────────────────────────────────────────────


class ModelGatewayListener:
    """A background service (`create_app`'s lifespan) serving the internal
    Model Gateway app on its internal binding only — never the public port."""

    def __init__(self, *, app: Any, binding: str) -> None:
        self.app = app
        self.binding = binding
        self._server = None
        self._task: asyncio.Task | None = None
        self._socket: Any = None

    async def start(self) -> None:
        import uvicorn

        self._socket = internal_listen_socket(self.binding)
        config = uvicorn.Config(self.app, lifespan="off", access_log=False, log_level="warning",
                                server_header=False, date_header=False, proxy_headers=False)
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = lambda: None  # the main server owns signals
        self._task = asyncio.create_task(self._server.serve(sockets=[self._socket]))
        while not self._server.started:
            if self._task.done():
                self._task.result()
                raise RuntimeError("model gateway listener stopped while starting")
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, timeout=10)
        if self._socket is not None:
            self._socket.close()
        remove_internal_socket(self.binding)
        self._server = self._task = self._socket = None


__all__ = ["HttpModelGateway", "ModelGatewayListener"]
