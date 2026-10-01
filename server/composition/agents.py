"""The Agent Factory, joined to the configuration and the security layer
(docs/29 §3.1, §23).

`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]` (docs/29). `server.agents` is pure
and store-light by contract (AF-C1/AF-C2); this module is where it meets the
server configuration, the model factory, the scheduler's schedule parser, the
egress policy and the authorization engine. **It adds no policy of its own**:
every decision below is `AuthorizationEngine.authorize` (04), exactly as for
the scheduler (`server/composition/scheduler.py`).

Two paths reach the factory, and both end at the engine:

* **The factory worker** (an ordinary user task) proposes `agent.define.*`,
  `agent.inspect.*`, `agent.delete.*` tool calls. The runtime has already
  checked activation, the mode ceiling, D1–D5, the floor and the tier — and, for
  `create`/`update`/`delete`, paused for the owner's confirmation of the exact
  call — before `AgentToolAdapter` runs. Everything identity-shaped comes from
  the `ToolInvocation` the runtime built and the task's own row, never from the
  worker's arguments.
* **The owner's HTTP API** (`AgentFactoryFacade`): each operation is authorized
  against the `agentdefinition` resource before the service is touched;
  creating, changing and deleting return `confirmation_required` with a token
  bound to exactly that preview or agent.

Provider credentials never pass through here: a model profile references a
configured model entry whose `secret_ref` stays in the server configuration
and is resolved only when `server.models` builds an adapter (06 §1).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import re
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Iterator, Mapping
from urllib.parse import urlsplit

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.agent.agent_run import AgentRunBinding, GatewayAdmission, ModelCallFacts, RoutedModelCall, RunTokens
from server.agents.compiler import OwnerContext, parse_draft, revalidation_required
from server.agents.gateway.core import AgentGateway, GatewayAdmitted, GatewayContext, GatewayDenied, GatewayReplayed
from server.agents.gateway.model_gateway import budget_refusal, model_refusal
from server.agents.gateway.model_routing import RouteRefused, route_model_call
from server.agents.instructions import assemble_input
from server.agents.revision import authority_of, redraft_with_purpose
from server.agents.providers.native import NativeRuntimeProvider
from server.agents.registry.runtimes import NATIVE_RUNTIME_ID
from server.agents.registry import AgentRegistries, ModelEntryFacts, build_registries
from server.agents.rendering import render_card, spec_view
from server.agents.service import OPERATOR_PAUSED, AgentDefinitionService, PreviewRefused
from server.capabilities.registry import (
    AGENT_DEFINE_CAPABILITY,
    AGENT_DELETE_CAPABILITY,
    AGENT_INSPECT_CAPABILITY,
)
from server.config.schema import LOCAL_MODEL_PROVIDERS, AppConfig
from server.memory.gate import LexiconEmotionClassifier
from server.security.secret_patterns import find_secret, redact_secrets
from server.gateway.errors import AppError
from server.gateway.security import SecurityCore
from server.graph.authorization import AccessRequest, AuthorizationOutcome
from server.graph.ports import ResourceDescriptor
from server.models.factory import IMPLEMENTED_PROVIDERS
from server.net.destinations import hostname_allowed
from server.scheduler.schedule import parse_schedule
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.scheduler.service import NewReminder, PreparedReminder, ReminderRefused
from server.security.usage import LimitExceeded
from server.storage.models import (
    AgentCompilePreviewRow,
    AgentConfiguration,
    AgentDefinitionRow,
    AgentRunRow,
    AgentTask,
    ReminderDelivery,
    ScheduledJob,
)
from server.tools.registry import ToolDefinition
from shared.schemas.agent import (
    AgentFailureCode,
    AgentResult,
    AgentTaskStatus,
    ExecutionPlatform,
    OperationSpec,
    ToolInvocation,
    ToolOutput,
)
from shared.schemas.agent_config import ToolContract
from shared.schemas.agent_factory import (
    AgentDetail,
    AgentDraft,
    AgentListResponse,
    AgentGatewayErrorCode,
    AgentGatewayRequest,
    AgentRunContext,
    AgentRunListResponse,
    AgentRunStatus,
    AgentRunView,
    AgentStatus,
    AgentView,
    CancelReason,
    AgentExport,
    AgentInboxItemView,
    AgentInboxResponse,
    AgentPurposeCandidateList,
    AgentPurposeCandidateView,
    AgentSpecVersionExport,
    CompiledAgentSpec,
    NotebookEntryView,
    NotebookResponse,
    RunHandle,
    RuntimeRef,
    CompiledAgentSpecView,
    CompileOutcome,
    CreateAgentRequest,
    ModelCallRequest,
    RunTokenPurpose,
    TriggerKind,
)
from shared.schemas.authorization import DenialSurface, Operation, Principal, ResourceType
from shared.schemas.enums import (
    AgentConfigScopeType,
    AuditActor,
    AuditResult,
    JobStatus,
    RiskCategory,
    Visibility,
)
from shared.schemas.scheduler import ReasonSource
from shared.schemas.errors import ErrorCode

if TYPE_CHECKING:
    from server.composition.facade import AgentTaskFacade
    from server.scheduler.service import SchedulerService

# ── registries (validated at every startup) ────────────────────────────────


def _facts(entry) -> ModelEntryFacts:
    pricing = entry.pricing
    return ModelEntryFacts(
        provider=entry.provider,
        model=entry.model,
        input_per_1k_tokens=pricing.input_per_1k_tokens if pricing is not None else 0.0,
        output_per_1k_tokens=pricing.output_per_1k_tokens if pricing is not None else 0.0,
    )


def model_entry_facts(config: AppConfig) -> dict[str, ModelEntryFacts]:
    """The model entries a profile may reference (docs/29 §6.2), as routing
    facts only: no endpoint and no `secret_ref` leaves the configuration here."""

    facts = {"agent.primary": _facts(config.agent)}
    if config.agent.fallback is not None:
        facts["agent.fallback"] = _facts(config.agent.fallback)
    for entry in config.models_as_tools:
        facts[f"models_as_tools.{entry.id}"] = _facts(entry)
    return facts


def registries_from_config(config: AppConfig) -> AgentRegistries:
    """Build and validate the registries; `AgentRegistryError` is fatal to
    startup whether or not `agents.enabled` (a latent error is found at the
    deployment that introduced it, not the one that switches the feature on)."""

    agents = config.agents
    return build_registries(
        enabled_templates=tuple(agents.enabled_templates),
        model_profiles=tuple(agents.model_profiles),
        open_to_all=tuple(agents.model_profiles_open_to_all),
        runtime_toggles={k: v.enabled for k, v in agents.runtimes.items()},
        model_entries=model_entry_facts(config),
        implemented_providers=IMPLEMENTED_PROVIDERS,
        local_providers=frozenset(LOCAL_MODEL_PROVIDERS),
    )


# ── the engine's projection of a definition ───────────────────────────────


class AgentDefinitionLoader:
    """`ResourceLoader` for `agentdefinition` (docs/29 §4.1): the engine's
    projection of a definition to its authorization facts — owner, private
    visibility, graph scope — and nothing else. A deleted definition is not
    loadable, so every operation on it is `not_found`, for its owner too."""

    async def load(self, session: AsyncSession, resource_type: ResourceType,
                   resource_ref: str) -> ResourceDescriptor | None:
        if resource_type is not ResourceType.AGENTDEFINITION:
            return None
        try:
            agent_id = uuid.UUID(str(resource_ref))
        except (ValueError, TypeError):
            return None
        row = await session.get(AgentDefinitionRow, agent_id)
        if row is None or row.status == AgentStatus.DELETED.value:
            return None
        return ResourceDescriptor(
            resource_type=ResourceType.AGENTDEFINITION,
            resource_ref=str(row.agent_id),
            owner_user_id=row.owner_user_id,
            # v1 agents are private (the table's check constraint says so too).
            visibility=Visibility.PRIVATE,
            graph_id=row.graph_id,
            source_user_id=row.owner_user_id,
        )


# ── the owner's server-side context ────────────────────────────────────────


# docs/29 §17.1: a delivery its device was sent — a tap can come only from one.
_RECEIVED = ("sent", "acked")

# docs/29 §17.1: an update leaves these agents active — so reminded.
_REMINDED_AFTER_UPDATE = (AgentStatus.ACTIVE.value, AgentStatus.NEEDS_REAPPROVAL.value)


class AgentReminders:
    """docs/29 §17.1: an agent's `reminder` trigger as an ordinary scheduler
    job — created through the scheduler's own service and the one engine, by
    the composition root (the scheduler never imports the factory, AF-C4).

    The job is the owner's own, private, worded "Run agent: {name}" (the name
    the owner approved on the card) on exactly the compiled schedule, and
    carries the agent's id as data. A firing delivers that message to the
    owner's devices and nothing else: the tap is the owner asking to run it.
    Pausing, re-approval and deletion cancel it (the safe direction, as part
    of that change's own authorization); resuming and updating re-create it.

    Two steps, because a refusal still commits the request's transaction (its
    audit trail must survive, `deps.SECURITY_REFUSALS`): `plan` runs every
    check that could refuse the reminder before the agent's own change is
    written, and `commit` writes the job after it — never a half-made agent."""

    def __init__(self, *, scheduler: "SchedulerService", core: SecurityCore) -> None:
        self._scheduler = scheduler
        self._core = core

    async def _has_active(self, session: AsyncSession, agent_id: uuid.UUID, owner_user_id: uuid.UUID) -> bool:
        return (await session.execute(select(ScheduledJob.job_id).where(
            ScheduledJob.agent_id == agent_id, ScheduledJob.owner_user_id == owner_user_id,
            ScheduledJob.status == JobStatus.ACTIVE,
        ).limit(1))).first() is not None

    async def plan(self, session: AsyncSession, audit: AuditLogger, *, principal: Principal,
                   spec: CompiledAgentSpec) -> PreparedReminder | None:
        """The quota (unless this replaces the agent's own active reminder),
        the schedule and the one engine — `None` if the spec has no reminder.
        Writes nothing."""

        trigger = spec.trigger
        if trigger.kind is not TriggerKind.REMINDER or not trigger.cron:
            return None
        if not await self._has_active(session, spec.agent_id, spec.owner_user_id):
            try:
                await self._scheduler.precheck_quota(session, user_id=principal.user_id)
            except LimitExceeded as exc:
                raise AppError(ErrorCode.RATE_LIMITED, "reminder limit reached",
                               details={"limit": exc.limit, "reason": "reminder_quota"}) from None
        try:
            prepared = self._scheduler.prepare(NewReminder(
                owner_user_id=spec.owner_user_id, task_reason=f"Run agent: {spec.name}",
                schedule=f"CRON_TZ={trigger.timezone} {trigger.cron}", reason_source=ReasonSource.USER,
                graph_id=spec.graph_id, device_id=principal.device_id, session_id=principal.session_id,
                agent_id=spec.agent_id,
            ))
        except ReminderRefused as exc:
            raise AppError(ErrorCode.CONFLICT, "this agent's reminder cannot be scheduled",
                           details={"reason": exc.reason}) from None
        # The one engine, as for any reminder: D1 on the agent's graph.
        outcome = await self._core.engine.authorize(session, AccessRequest(
            principal=principal, operation=Operation.CREATE, resource_type=ResourceType.SCHEDULEDJOB,
            graph_id=spec.graph_id), audit=audit)
        if not outcome.allowed:
            raise _refusal(outcome)
        return prepared

    async def commit(self, session: AsyncSession, audit: AuditLogger, prepared: PreparedReminder | None,
                     *, actor: AuditActor = AuditActor.USER) -> None:
        if prepared is not None:
            await self._scheduler.commit(session, prepared, audit=audit, actor=actor)

    async def cancel(self, session: AsyncSession, audit: AuditLogger, *, agent_id: uuid.UUID,
                     owner_user_id: uuid.UUID, actor: AuditActor = AuditActor.USER,
                     device_id: uuid.UUID | None = None, session_id: uuid.UUID | None = None) -> int:
        jobs = (await session.execute(select(ScheduledJob).where(
            ScheduledJob.agent_id == agent_id, ScheduledJob.owner_user_id == owner_user_id,
            ScheduledJob.status == JobStatus.ACTIVE,
        ))).scalars().all()
        for job in jobs:
            await self._scheduler.cancel(session, job, audit=audit, actor=actor, device_id=device_id,
                                         session_id=session_id)
        return len(jobs)


class AgentFactory:
    """What both paths share: the service, and the owner context the
    compiler needs — built from live server state, never from a draft."""

    def __init__(self, *, config: AppConfig, service: AgentDefinitionService, core: SecurityCore) -> None:
        self._config = config
        self._service = service
        self._core = core
        # docs/29 §11: the one choke point every request of every agent run
        # passes (in-process for native runs).
        self._gateway = AgentGateway(service)
        # docs/29 §17 (Phase 4): set when the scheduler is enabled too.
        self.reminders: AgentReminders | None = None

    @property
    def service(self) -> AgentDefinitionService:
        return self._service

    @property
    def gateway(self) -> AgentGateway:
        return self._gateway

    @property
    def registries(self) -> AgentRegistries:
        return self._service.registries

    async def primary_model_ref(self, session: AsyncSession, *, user_id: uuid.UUID,
                                graph_id: uuid.UUID | None) -> str:
        """docs/29 §6.2 via OD-RT-3: the owner's resolved primary as a model
        reference. `agent.primary` only when the server configuration is what
        applies to them; a user's (or their graph's) own configured model is
        never usable for an agent through a profile it does not match."""

        user_row = (await session.execute(select(AgentConfiguration.config_id).where(
            AgentConfiguration.scope_type == AgentConfigScopeType.USER,
            AgentConfiguration.scope_id == user_id,
        ))).scalars().first()
        if user_row is not None:
            return f"user-configuration:{user_row}"
        if graph_id is not None and await self._core.graph_repository.is_active_member(
            session, graph_id=graph_id, user_id=user_id
        ):
            graph_row = (await session.execute(select(AgentConfiguration.config_id).where(
                AgentConfiguration.scope_type == AgentConfigScopeType.GRAPH,
                AgentConfiguration.scope_id == graph_id,
            ))).scalars().first()
            if graph_row is not None:
                return f"graph-configuration:{graph_row}"
        return "agent.primary"

    def url_allowed(self, url: str) -> bool:
        """The operator's EgressPolicy for the generic network tool, read — the
        compiler can only ask, never widen it (docs/29 §9.3)."""

        network = self._config.execution.network
        host = urlsplit(url).hostname or ""
        return bool(host) and (network.default_internet or hostname_allowed(host, network.default_destinations))

    @staticmethod
    def trigger_preview(cron: str, timezone_name: str, now: datetime) -> datetime:
        schedule = parse_schedule(f"CRON_TZ={timezone_name} {cron}")
        if not schedule.recurring:
            raise ValueError("schedule_invalid")
        fire = schedule.next_after(now)
        if fire is None:
            raise ValueError("schedule_invalid")
        return fire

    @staticmethod
    def runtime_health() -> dict[str, bool]:
        # The native runtime is this process (docs/29 §7.4).
        return {"native": True}

    async def owner_context(self, session: AsyncSession, *, user_id: uuid.UUID,
                            graph_id: uuid.UUID | None) -> OwnerContext:
        agents, bounds = self._config.agents, self._config.agent.bounds
        return OwnerContext(
            owner_user_id=user_id,
            graph_id=graph_id,
            owner_primary_model_ref=await self.primary_model_ref(session, user_id=user_id, graph_id=graph_id),
            budget_per_run_policy=agents.default_budget_per_run,
            budget_per_month_policy=agents.default_budget_per_month,
            active_agent_count=await self._service.active_count(session, user_id),
            max_agents_per_user=agents.max_agents_per_user,
            runtime_max_seconds=bounds.wall_clock_timeout_seconds,
            runtime_max_model_calls=bounds.max_model_calls,
            runtime_max_tool_calls=bounds.max_tool_calls,
            url_allowed=self.url_allowed,
        )

    def run_input(self, spec: CompiledAgentSpec, run_id: uuid.UUID) -> str:
        """docs/29 §9.7: the run's input, assembled by code from the spec,
        within the runtime's input limit."""

        return assemble_input(spec, run_id, max_chars=self._config.agent.bounds.max_input_chars)

    def run_deadline(self, spec: CompiledAgentSpec) -> datetime:
        seconds = min(float(spec.bounds.max_run_seconds), self._config.agent.bounds.wall_clock_timeout_seconds)
        return datetime.now(timezone.utc) + timedelta(seconds=seconds)

    async def compile(self, session: AsyncSession, *, user_id: uuid.UUID, graph_id: uuid.UUID | None,
                      draft: AgentDraft, task_id: uuid.UUID | None,
                      agent: AgentDefinitionRow | None) -> CompileOutcome:
        return await self._service.compile(
            session, draft=draft, owner=await self.owner_context(session, user_id=user_id, graph_id=graph_id),
            task_id=task_id, agent=agent, runtime_health=self.runtime_health(),
            trigger_preview=self.trigger_preview,
        )


def _card_json(spec: CompiledAgentSpec, registries: AgentRegistries) -> dict[str, Any]:
    return render_card(spec, registries).model_dump(mode="json")


# ── the worker's path (docs/29 §2.1) ───────────────────────────────────────


@dataclass(frozen=True)
class _Scope:
    session: AsyncSession
    audit: AuditLogger


# The request-scoped transaction an agent tool call writes in — installed by
# the task facade around runtime calls, like `CURRENT_REMINDER_SCOPE`.
CURRENT_AGENT_FACTORY_SCOPE: ContextVar[_Scope | None] = ContextVar("current_agent_factory_scope", default=None)


@contextlib.contextmanager
def agent_factory_scope(session: AsyncSession, audit: AuditLogger) -> Iterator[None]:
    token = CURRENT_AGENT_FACTORY_SCOPE.set(_Scope(session=session, audit=audit))
    try:
        yield
    finally:
        CURRENT_AGENT_FACTORY_SCOPE.reset(token)


def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _outcome_payload(outcome: CompileOutcome) -> dict[str, Any]:
    payload: dict[str, Any] = {"kind": outcome.kind}
    if outcome.compile_id is not None:
        payload["compile_id"] = str(outcome.compile_id)
        payload["expires_at"] = outcome.expires_at.isoformat() if outcome.expires_at else None
    if outcome.spec_preview is not None:
        payload["card"] = outcome.spec_preview.card.model_dump(mode="json")
    if outcome.questions:
        payload["questions"] = [q.model_dump(mode="json") for q in outcome.questions]
    if outcome.reason_codes:
        payload["reason_codes"] = list(outcome.reason_codes)
    return payload


def _compile_id(arguments: Any) -> uuid.UUID | None:
    if not isinstance(arguments, dict) or set(arguments) != {"compile_id"}:
        return None
    try:
        return uuid.UUID(str(arguments["compile_id"]))
    except (ValueError, TypeError):
        return None


def _agent_ref(ref: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(ref))
    except (ValueError, TypeError):
        return None


class AgentToolAdapter:
    """`agent.define` / `agent.inspect` / `agent.delete` on the server
    platform. The runtime authorized the exact call before this runs; the
    adapter binds identity from the invocation and the task row, refuses any
    argument outside the operation's closed shape, and re-checks ownership
    of everything it loads by id."""

    def __init__(self, factory: AgentFactory) -> None:
        self._factory = factory

    async def execute(self, invocation: ToolInvocation) -> ToolOutput:
        scope = CURRENT_AGENT_FACTORY_SCOPE.get()
        if scope is None:
            return ToolOutput(ok=False, error="agents_unavailable")
        task = await scope.session.get(AgentTask, invocation.task_id)
        if task is None or task.user_id != invocation.user_id:
            return ToolOutput(ok=False, error="task_unavailable")
        handler = {
            (AGENT_DEFINE_CAPABILITY, "compile"): self._compile,
            (AGENT_DEFINE_CAPABILITY, "compile_update"): self._compile_update,
            (AGENT_DEFINE_CAPABILITY, "create"): self._create,
            (AGENT_DEFINE_CAPABILITY, "update"): self._update,
            (AGENT_INSPECT_CAPABILITY, "list"): self._list,
            (AGENT_INSPECT_CAPABILITY, "get"): self._get,
            (AGENT_DELETE_CAPABILITY, "delete"): self._delete,
        }.get((invocation.tool_id, invocation.operation))
        if handler is None:
            return ToolOutput(ok=False, error="unknown_operation")
        return await handler(scope, invocation, task)

    async def _audit(self, scope: _Scope, invocation: ToolInvocation, action: AuditAction, resource: str,
                     result: AuditResult = AuditResult.SUCCESS) -> None:
        await scope.audit.record(actor=AuditActor.AGENT, action=action, resource=resource, result=result,
                                 user_id=invocation.user_id, device_id=invocation.device_id)

    @staticmethod
    def _task_principal(task: AgentTask) -> Principal:
        """The principal of the task the worker runs in — from its own row."""

        return Principal(user_id=task.user_id, device_id=task.device_id, session_id=task.session_id,
                         active_graph_id=task.graph_id)

    async def _owned(self, scope: _Scope, invocation: ToolInvocation):
        agent_id = _agent_ref(invocation.resource_ref)
        loaded = await self._factory.service.load(scope.session, agent_id) if agent_id else None
        if loaded is None or loaded[0].owner_user_id != invocation.user_id:
            return None
        return loaded

    async def _compile_draft(self, scope, invocation, task, agent) -> ToolOutput:
        draft, fields = parse_draft(invocation.arguments)
        if draft is None:
            await self._audit(scope, invocation, AuditAction.AGENT_DRAFT_REJECTED,
                              f"agentdraft:fields:{','.join(fields)}", AuditResult.BLOCKED)
            return ToolOutput(
                ok=False, error="draft_invalid",
                content="The draft was rejected as a whole. Invalid or forbidden fields: " + ", ".join(fields)
                        + ". A draft carries only name, purpose, desired_outcome, task_tags, template_hint, "
                          "requested_abilities, sources, trigger_request, output_request, model_preference, "
                          "preferred_model_features, budget_preference_per_run and notes_for_user.",
            )
        outcome = await self._factory.compile(scope.session, user_id=invocation.user_id, graph_id=task.graph_id,
                                              draft=draft, task_id=invocation.task_id, agent=agent)
        if outcome.compile_id is not None:
            await self._audit(scope, invocation, AuditAction.AGENT_COMPILED, f"agentpreview:{outcome.compile_id}")
        return ToolOutput(ok=outcome.kind == "compiled", content=_dumps(_outcome_payload(outcome)),
                          error=None if outcome.kind == "compiled" else outcome.kind)

    async def _compile(self, scope, invocation, task) -> ToolOutput:
        return await self._compile_draft(scope, invocation, task, None)

    async def _compile_update(self, scope, invocation, task) -> ToolOutput:
        loaded = await self._owned(scope, invocation)
        if loaded is None:
            return ToolOutput(ok=False, error="not_found", content="Not found or not permitted.")
        definition, spec = loaded
        if spec is None:
            return ToolOutput(ok=False, error="agent_revoked", content="That agent is revoked and cannot be updated.")
        return await self._compile_draft(scope, invocation, task, definition)

    async def _create(self, scope, invocation, task) -> ToolOutput:
        compile_id = _compile_id(invocation.arguments)
        if compile_id is None:
            return ToolOutput(ok=False, error="invalid_arguments",
                              content="create takes exactly one argument: `compile_id`.")
        service = self._factory.service
        preview = await service.load_preview(scope.session, compile_id=compile_id, owner_user_id=invocation.user_id,
                                             task_id=invocation.task_id, for_agent=None)
        if preview is None:
            return ToolOutput(ok=False, error="preview_not_found",
                              content="No such preview in this task (it may have expired). Compile again.")
        try:
            # docs/29 §17.1: every check on the reminder before anything is written.
            planned = await self._plan_reminder(scope, task, service.preview_spec(preview))
            definition, spec = await service.create_from_preview(
                scope.session, preview, created_by_device_id=invocation.device_id,
                created_from_task_id=invocation.task_id)
        except AppError as exc:
            return ToolOutput(ok=False, error=str((exc.details or {}).get("reason") or exc.code.value))
        except PreviewRefused as exc:
            await self._audit(scope, invocation, AuditAction.AGENT_REFUSED, f"agentpreview:{compile_id}:{exc.reason}",
                              AuditResult.BLOCKED)
            return ToolOutput(ok=False, error=exc.reason)
        if self._factory.reminders is not None:
            await self._factory.reminders.commit(scope.session, scope.audit, planned, actor=AuditActor.AGENT)
        await self._audit(scope, invocation, AuditAction.AGENT_CREATED, f"agentdefinition:{definition.agent_id}")
        return ToolOutput(ok=True, content=_dumps({
            "agent_id": str(definition.agent_id), "version": spec.version, "status": definition.status,
            **_card_json(spec, self._factory.registries),
        }))

    async def _update(self, scope, invocation, task) -> ToolOutput:
        compile_id = _compile_id(invocation.arguments)
        if compile_id is None:
            return ToolOutput(ok=False, error="invalid_arguments",
                              content="update takes exactly one argument: `compile_id`.")
        loaded = await self._owned(scope, invocation)
        if loaded is None:
            return ToolOutput(ok=False, error="not_found", content="Not found or not permitted.")
        definition, spec = loaded
        service = self._factory.service
        preview = await service.load_preview(scope.session, compile_id=compile_id, owner_user_id=invocation.user_id,
                                             task_id=invocation.task_id, for_agent=definition.agent_id)
        if preview is None or spec is None:
            return ToolOutput(ok=False, error="preview_not_found",
                              content="No such update preview for that agent in this task. Compile again.")
        try:
            new_spec = service.preview_spec(preview)
            # Planned only if the update leaves the agent active (a paused
            # agent's reminder stays suppressed until it is resumed).
            planned = (await self._plan_reminder(scope, task, new_spec) if definition.status in _REMINDED_AFTER_UPDATE
                       else self._reminders_or_refuse(new_spec))
            definition, new = await service.update_from_preview(scope.session, definition, preview)
        except AppError as exc:
            return ToolOutput(ok=False, error=str((exc.details or {}).get("reason") or exc.code.value))
        except PreviewRefused as exc:
            return ToolOutput(ok=False, error=exc.reason)
        await self._replace_reminder(scope, task, definition, planned)
        await self._audit(scope, invocation, AuditAction.AGENT_UPDATED,
                          f"agentdefinition:{definition.agent_id}:v{new.version}")
        return ToolOutput(ok=True, content=_dumps({
            "agent_id": str(definition.agent_id), "version": new.version, **_card_json(new, self._factory.registries),
        }))

    def _reminders_or_refuse(self, spec: CompiledAgentSpec) -> None:
        if spec.trigger.kind is TriggerKind.REMINDER and self._factory.reminders is None:
            raise AppError(ErrorCode.CONFLICT, "this server has no scheduler for the agent's reminder",
                           details={"reason": "reminders_unavailable"})

    async def _plan_reminder(self, scope: _Scope, task: AgentTask, spec: CompiledAgentSpec) -> PreparedReminder | None:
        self._reminders_or_refuse(spec)
        if self._factory.reminders is None:
            return None
        return await self._factory.reminders.plan(scope.session, scope.audit, principal=self._task_principal(task),
                                                  spec=spec)

    async def _replace_reminder(self, scope: _Scope, task: AgentTask, definition: AgentDefinitionRow,
                                planned: PreparedReminder | None) -> None:
        reminders = self._factory.reminders
        if reminders is None:
            return
        await reminders.cancel(scope.session, scope.audit, agent_id=definition.agent_id,
                               owner_user_id=definition.owner_user_id, actor=AuditActor.AGENT,
                               device_id=task.device_id)
        if definition.status == AgentStatus.ACTIVE.value:
            await reminders.commit(scope.session, scope.audit, planned, actor=AuditActor.AGENT)

    async def _list(self, scope, invocation, task) -> ToolOutput:
        if invocation.arguments:
            return ToolOutput(ok=False, error="invalid_arguments", content="list takes no arguments.")
        service = self._factory.service
        items = [service.view(d, s).model_dump(mode="json")
                 for d, s in await service.list_for_owner(scope.session, invocation.user_id)]
        return ToolOutput(ok=True, content=_dumps({"items": items}))

    async def _get(self, scope, invocation, task) -> ToolOutput:
        loaded = await self._owned(scope, invocation)
        if loaded is None:
            return ToolOutput(ok=False, error="not_found", content="Not found or not permitted.")
        definition, spec = loaded
        payload = {"agent": self._factory.service.view(definition, spec).model_dump(mode="json")}
        if spec is not None:
            payload["card"] = _card_json(spec, self._factory.registries)
        return ToolOutput(ok=True, content=_dumps(payload))

    async def _delete(self, scope, invocation, task) -> ToolOutput:
        if invocation.arguments:
            return ToolOutput(ok=False, error="invalid_arguments", content="delete takes no arguments.")
        loaded = await self._owned(scope, invocation)
        if loaded is None:
            return ToolOutput(ok=False, error="not_found", content="Not found or not permitted.")
        definition, _ = loaded
        if self._factory.reminders is not None:
            await self._factory.reminders.cancel(scope.session, scope.audit, agent_id=definition.agent_id,
                                                 owner_user_id=definition.owner_user_id, actor=AuditActor.AGENT,
                                                 device_id=invocation.device_id)
        await self._factory.service.delete(scope.session, definition)
        await self._audit(scope, invocation, AuditAction.AGENT_DELETED, f"agentdefinition:{definition.agent_id}")
        return ToolOutput(ok=True, content=_dumps({"agent_id": str(definition.agent_id), "status": "deleted"}))


_DRAFT_HELP = (
    "The arguments of `compile`/`compile_update` are an AgentDraft — the user's intent only: name, purpose, "
    "desired_outcome, task_tags (research, monitoring, summarization, classification, extraction, "
    "document_processing, reporting, notification_prep, knowledge_maintenance, data_analysis, api_workflow, "
    "security_research), optional template_hint, requested_abilities (read_web_allowlisted, read_user_memory, "
    "read_vault, read_sandbox_files, write_sandbox_files, read_agent_notebook, write_agent_notebook, "
    "create_reminder, invoke_model_tool), sources [{kind: url|memory_topic|vault_domain|sandbox_path, value}], "
    "trigger_request {kind: on_demand|reminder, cron, timezone}, model_preference (faster|cheaper|thorough), "
    "preferred_model_features, budget_preference_per_run, notes_for_user. It can name no permission, owner, "
    "graph, budget ceiling, runtime, model or endpoint: JARVIS decides those. `compile` returns a compile_id "
    "and the card the user will approve; `create` takes only {compile_id} and needs the user's confirmation."
)


def _contract(tool_id: str, capability: str, description: str, risk: RiskCategory, confirm: bool) -> ToolContract:
    return ToolContract(
        tool_id=tool_id, version="1", description=description,
        input_schema={"type": "object"}, output_schema={"type": "string"},
        required_capability=capability, filesystem={}, network={},
        risk_category=risk, timeout_seconds=10, confirmation_required=confirm,
        failure_behavior="observation", audit="every invocation",
    )


def agent_tool_definitions(factory: AgentFactory) -> list[ToolDefinition]:
    adapter = AgentToolAdapter(factory)
    agent = ResourceType.AGENTDEFINITION.value
    action = ResourceType.TOOL_ACTION.value
    return [
        ToolDefinition(
            contract=_contract(
                AGENT_DEFINE_CAPABILITY, AGENT_DEFINE_CAPABILITY,
                "Define a reusable agent for the user from their goal (docs/29). " + _DRAFT_HELP
                + " `compile_update` and `update` take the agent's id as resource_ref.",
                RiskCategory.CONSEQUENTIAL, True,
            ),
            operations={
                "compile": OperationSpec(action, Operation.CREATE.value),
                "compile_update": OperationSpec(agent, Operation.READ.value, requires_resource_ref=True),
                "create": OperationSpec(agent, Operation.CREATE.value),
                "update": OperationSpec(agent, Operation.WRITE.value, requires_resource_ref=True),
            },
            adapters={ExecutionPlatform.SERVER: adapter},
        ),
        ToolDefinition(
            contract=_contract(
                AGENT_INSPECT_CAPABILITY, AGENT_INSPECT_CAPABILITY,
                "List the user's agents (`list`, no arguments) or read one (`get`, resource_ref = agent id).",
                RiskCategory.LOW_READ, False,
            ),
            operations={
                "list": OperationSpec(action, Operation.CREATE.value),
                "get": OperationSpec(agent, Operation.READ.value, requires_resource_ref=True),
            },
            adapters={ExecutionPlatform.SERVER: adapter},
        ),
        ToolDefinition(
            contract=_contract(
                AGENT_DELETE_CAPABILITY, AGENT_DELETE_CAPABILITY,
                "Delete one of the user's agents (resource_ref = agent id); needs the user's confirmation.",
                RiskCategory.CONSEQUENTIAL, True,
            ),
            operations={"delete": OperationSpec(agent, Operation.DELETE.value, requires_resource_ref=True)},
            adapters={ExecutionPlatform.SERVER: adapter},
        ),
    ]


# ── present-user runs (docs/29 §7.4, Phase 2) ─────────────────────────────

_EMOTION = LexiconEmotionClassifier()
INBOX_MAX_BODY_CHARS = 16_000  # docs/29 §22.1
_UNSAFE_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def inbox_body(response: str | None) -> tuple[str, bool, bool]:
    """A run's result as inbox text: (body, withheld, truncated). Plain text
    only — control characters (terminal escapes included) are dropped — and
    bounded. A result that looks like it carries a credential is not stored
    at all (12 §2: a secret never lands in a record)."""

    text = _UNSAFE_CHARS.sub("", response or "")
    if find_secret(text) is not None:
        return "", True, False
    if len(text) > INBOX_MAX_BODY_CHARS:
        return text[:INBOX_MAX_BODY_CHARS], False, True
    return text, False, False


def _scrubbed(value: Any) -> Any:
    """What the gateway's ledger keeps of a response: every string with
    anything secret-shaped masked (docs/29 §13.3 step 11)."""

    if isinstance(value, str):
        return redact_secrets(value)[0]
    if isinstance(value, Mapping):
        return {str(k): _scrubbed(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrubbed(v) for v in value]
    return value


def _gateway_failure(code: AgentGatewayErrorCode) -> AgentFailureCode:
    return AgentFailureCode.SPEC_CHANGED if code is AgentGatewayErrorCode.SPEC_CHANGED \
        else AgentFailureCode.AGENT_UNAVAILABLE


class AgentRunCoordinator:
    """`AgentRunPort` (server/agent/agent_run.py) for one request's store
    transaction. It decides nothing about authority: every request of the run
    goes through the Agent Gateway (`check` at each step, `admit` for each
    model or tool request), which only answers whether the request belongs to
    a live run — its token, the run, and the definition the run is bound to,
    still existing, active and exactly that version and hash — read fresh from
    the store every time, never cached. The rest records what happened."""

    def __init__(self, factory: AgentFactory, session: AsyncSession, audit: AuditLogger,
                 core: SecurityCore) -> None:
        self._factory = factory
        self._service = factory.service
        self._gateway = factory.gateway
        self._session = session
        self._audit_logger = audit
        self._core = core

    async def _is_member(self, graph_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        return await self._core.graph_repository.is_active_member(self._session, graph_id=graph_id,
                                                                 user_id=user_id)

    async def _audit(self, action: AuditAction, run: AgentRunRow | None, resource: str,
                     result: AuditResult = AuditResult.SUCCESS) -> None:
        await self._audit_logger.record(actor=AuditActor.AGENT, action=action, resource=resource, result=result,
                                        user_id=run.owner_user_id if run is not None else None)

    async def started(self, binding: AgentRunBinding, *, task_id: uuid.UUID) -> None:
        run = await self._service.run_started(self._session, binding.run_id, task_id=task_id)
        await self._audit(AuditAction.AGENT_RUN_STARTED, run, f"agentrun:{binding.run_id}:task:{task_id}")

    async def _denied(self, binding: AgentRunBinding, purpose: RunTokenPurpose, denied: GatewayDenied) -> None:
        owner = await self._run_owner(binding)
        await self._audit_logger.record(
            actor=AuditActor.AGENT, action=AuditAction.AGENT_GATEWAY_DENIED,
            resource=f"agentrun:{binding.run_id}:{purpose.value}:{denied.code.value}:{denied.detail}"[:128],
            result=AuditResult.BLOCKED, user_id=owner)

    async def check(self, binding: AgentRunBinding) -> AgentFailureCode | None:
        tokens = binding.run_tokens
        if tokens is None:
            return AgentFailureCode.AGENT_UNAVAILABLE
        outcome = await self._gateway.authenticate(
            self._session, run_id=binding.run_id, agent_id=binding.agent_id, purpose=RunTokenPurpose.TOOL,
            token=tokens.tool, is_member=self._is_member)
        if isinstance(outcome, GatewayDenied):
            await self._denied(binding, RunTokenPurpose.TOOL, outcome)
            return _gateway_failure(outcome.code)
        if (outcome.version, outcome.spec_hash) != (binding.version, binding.spec_hash):
            # The runtime's own binding must be exactly the run the token is for.
            return AgentFailureCode.SPEC_CHANGED
        return None

    async def _model_screen(self, model: ModelCallFacts | None, context: GatewayContext) -> GatewayDenied | None:
        """docs/29 §12.2: the Model Gateway's own check — the alias, the
        approved profile, the owner's model policy now, the exact model."""

        not_allowed = AgentGatewayErrorCode.MODEL_NOT_ALLOWED
        if model is None:
            return GatewayDenied(not_allowed, "no_alias")
        loaded = await self._service.load(self._session, context.agent_id, fresh=True)
        if loaded is None or loaded[1] is None or loaded[1].spec_hash != context.spec_hash:
            return GatewayDenied(AgentGatewayErrorCode.AGENT_UNAVAILABLE, "spec")
        definition, spec = loaded
        owner_ref = await self._factory.primary_model_ref(self._session, user_id=definition.owner_user_id,
                                                          graph_id=definition.graph_id)
        refused = model_refusal(spec, self._factory.registries, alias=model.alias, provider=model.provider,
                                model=model.model, owner_primary_model_ref=owner_ref)
        return GatewayDenied(not_allowed, refused) if refused is not None else None

    async def admit(self, binding: AgentRunBinding, request: AgentGatewayRequest,
                    model: ModelCallFacts | None = None) -> GatewayAdmission:
        screen = None
        if request.purpose is RunTokenPurpose.MODEL:
            async def screen(context: GatewayContext) -> GatewayDenied | None:
                return await self._model_screen(model, context)
        outcome = await self._gateway.admit(self._session, request, is_member=self._is_member, screen=screen)
        if isinstance(outcome, GatewayDenied):
            await self._denied(binding, request.purpose, outcome)
            return GatewayAdmission(refused=outcome.code.value)
        if isinstance(outcome, GatewayReplayed):
            return GatewayAdmission(replayed=outcome.response)
        return GatewayAdmission(ticket=outcome)

    async def settle(self, binding: AgentRunBinding, admission: GatewayAdmission,
                     response: Mapping[str, Any]) -> None:
        if isinstance(admission.ticket, GatewayAdmitted):
            await self._gateway.settle(self._session, admission.ticket, _scrubbed(response))

    async def agent_budget(self, binding: AgentRunBinding, *, projected_cost: float) -> str | None:
        loaded = await self._service.load(self._session, binding.agent_id, fresh=True)
        if loaded is None or loaded[1] is None:
            return AgentGatewayErrorCode.AGENT_UNAVAILABLE.value
        spent = await self._service.month_usage(self._session, binding.agent_id)
        return budget_refusal(projected_cost=projected_cost, month_spent=spent,
                              month_budget=loaded[1].budget.per_month)

    async def usage_recorded(self, binding: AgentRunBinding, *, usage_id: uuid.UUID, cost: float) -> None:
        await self._service.attribute_usage(self._session, binding.run_id, usage_id)

    async def finished(self, binding: AgentRunBinding, *, task_id: uuid.UUID, status: AgentTaskStatus,
                       response: str | None, failure: AgentFailureCode | None, cost: float) -> None:
        # docs/29 §11.3: the run's tokens end with it.
        if await self._gateway.revoke_run(self._session, binding.run_id, "finished"):
            await self._audit_logger.record(actor=AuditActor.AGENT, action=AuditAction.AGENT_TOKEN_REVOKED,
                                            resource=f"agentrun:{binding.run_id}:finished",
                                            result=AuditResult.SUCCESS, user_id=await self._run_owner(binding))
        run = await self._service.run_finished(self._session, binding.run_id, status=status,
                                               failure=failure.value if failure else None, cost=cost)
        suffix = f":{failure.value}" if failure else ""
        await self._audit(AuditAction.AGENT_RUN_FINISHED, run,
                          f"agentrun:{binding.run_id}:{status.value}{suffix}",
                          AuditResult.SUCCESS if status is AgentTaskStatus.COMPLETED else AuditResult.FAILURE)
        if run is not None:
            # docs/29 §19: the result goes to its owner's inbox, as data, and
            # nowhere else. The run record's own status says how it ended (a
            # stop recorded first stays a stop).
            body, withheld, truncated = inbox_body(response)
            item = await self._service.inbox_deliver(self._session, run, body=body, withheld=withheld,
                                                     truncated=truncated)
            if item is not None:
                await self._audit(AuditAction.AGENT_INBOX_DELIVERED, run, f"agentinbox:{item.item_id}")

    async def _run_owner(self, binding: AgentRunBinding) -> uuid.UUID | None:
        run = await self._session.get(AgentRunRow, binding.run_id)
        return run.owner_user_id if run is not None and run.agent_id == binding.agent_id else None

    async def notebook_get(self, binding: AgentRunBinding, key: str) -> str | None:
        if not binding.notebook:
            return None
        return await self._service.notebook_get(self._session, binding.agent_id, key)

    async def notebook_keys(self, binding: AgentRunBinding) -> list[str]:
        if not binding.notebook:
            return []
        return await self._service.notebook_keys(self._session, binding.agent_id)

    async def notebook_put(self, binding: AgentRunBinding, key: str, value: str) -> str | None:
        """docs/29 §16.3: the agent's own note, never memory. The same gates
        as a memory write refuse it (21 §4): nothing credential-shaped, nothing
        emotional or relational — the notebook is operational state."""

        if not binding.notebook:
            return "notebook_not_enabled"
        owner = await self._run_owner(binding)
        if owner is None:
            return "agent_unavailable"
        if find_secret(f"{key}\n{value}") is not None:
            return "secret_like_content"
        if _EMOTION.flags(f"{key} {value}"):
            return "sensitive_content"
        refused = await self._service.notebook_put(self._session, agent_id=binding.agent_id, owner_user_id=owner,
                                                   run_id=binding.run_id, key=key, value=value)
        if refused is None:
            await self._audit_logger.record(actor=AuditActor.AGENT, action=AuditAction.AGENT_NOTEBOOK_WRITTEN,
                                            resource=f"agentnotebook:{binding.agent_id}:put:run:{binding.run_id}",
                                            result=AuditResult.SUCCESS, user_id=owner)
        return refused

    async def route_model(self, binding: AgentRunBinding, arguments: Mapping[str, Any]) -> RoutedModelCall | str:
        """docs/29 §12: the request names a role and a preference only — a
        provider, model, profile, endpoint or key is a validation failure.
        The profile is chosen from the stored spec's envelope, the operator's
        registries and the owner's *current* model policy; nothing the agent
        says can make a profile eligible."""

        try:
            request = ModelCallRequest.model_validate(dict(arguments))
        except ValidationError:
            return "invalid_request"
        loaded = await self._service.load(self._session, binding.agent_id, fresh=True)
        if loaded is None or loaded[1] is None or loaded[1].spec_hash != binding.spec_hash:
            return "agent_unavailable"
        definition, spec = loaded
        owner_ref = await self._factory.primary_model_ref(self._session, user_id=definition.owner_user_id,
                                                          graph_id=definition.graph_id)
        route = route_model_call(request, spec=spec, registries=self._factory.registries,
                                 owner_primary_model_ref=owner_ref)
        if isinstance(route, RouteRefused):
            return route.reason
        prompt = f"{request.system}\n\n{request.prompt}" if request.system else request.prompt
        return RoutedModelCall(tool_id=route.model_tool_id, arguments={"prompt": prompt})


class _PresentUserRun:
    """`NativeRuntimePort` bound to the request that asked for the run: its
    authenticated principal, its store transaction and the ordinary task
    path. The provider hands it only the `AgentRunContext`; the run is then an
    ordinary task of that principal, in the binding's mode, under its ceiling.
    There is no other way for a native run to start — no background path."""

    def __init__(self, *, tasks: "AgentTaskFacade", factory: AgentFactory, session: AsyncSession,
                 principal: Principal, audit: AuditLogger, binding: AgentRunBinding | None = None) -> None:
        self._tasks = tasks
        self._service = factory.service
        self._gateway = factory.gateway
        self._session = session
        self._principal = principal
        self._audit = audit
        self._binding = binding
        self.result: AgentResult | None = None

    async def start(self, ctx: AgentRunContext) -> AgentRunStatus:
        binding = self._binding
        if binding is None or (ctx.run_id, ctx.agent_id, ctx.version, ctx.spec_hash) != (
            binding.run_id, binding.agent_id, binding.version, binding.spec_hash
        ):
            raise ValueError("the run context does not match its binding")
        if not ctx.run_token or not ctx.model_run_token:
            # docs/29 §11: no run reaches a model or a tool except through the
            # Agent Gateway, and the gateway admits nothing without its token.
            raise ValueError("a native run starts only with its two run tokens")
        binding = dataclasses.replace(binding, run_tokens=RunTokens(model=ctx.model_run_token, tool=ctx.run_token))
        self.result = await self._tasks.submit(self._session, principal=self._principal, user_input=ctx.input_text,
                                               audit=self._audit, mode=binding.run_mode, agent=binding)
        return AgentRunStatus.RUNNING

    async def cancel(self, run_id: uuid.UUID, reason: CancelReason) -> None:
        """Stop the run's task through the runtime's own cancel (05 §9): a
        paused action is dropped and never performed; a running task stops at
        its next checkpoint, an in-flight tool call is aborted. The run record
        is closed here too, so the run's next re-validation fails even if its
        task were somehow still driven."""

        run = await self._service.get_run(self._session, run_id)
        if run is None or run.finished_at is not None:
            return
        # docs/29 §11.3, AGENT-T23: the run's tokens are revoked first, before
        # the runtime is told — from here on the gateway admits nothing of
        # this run, whatever the runtime is doing.
        if await self._gateway.revoke_run(self._session, run_id, reason.value):
            await self._audit.record(actor=AuditActor.USER, action=AuditAction.AGENT_TOKEN_REVOKED,
                                     resource=f"agentrun:{run_id}:{reason.value}", result=AuditResult.SUCCESS,
                                     user_id=self._principal.user_id, device_id=self._principal.device_id,
                                     session_id=self._principal.session_id)
        if run.task_id is not None:
            try:
                await self._tasks.cancel(self._session, principal=self._principal, task_id=run.task_id,
                                         audit=self._audit)
            except AppError as exc:
                if exc.code is not ErrorCode.NOT_FOUND:
                    raise
        await self._service.run_cancelled(self._session, run_id, reason=reason.value)

    async def status_of(self, run_id: uuid.UUID) -> AgentRunStatus:
        raise NotImplementedError("run status is read from the run record")

    async def purge(self, agent_id: uuid.UUID) -> None:
        await self._service.purge_runtime_state(self._session, agent_id)

    async def export(self, agent_id: uuid.UUID) -> bytes:
        """What the native runtime keeps for the agent: its notebook."""

        rows = await self._service.notebook_entries(self._session, agent_id, self._principal.user_id)
        return json.dumps({"notebook": [self._service.notebook_view(r).model_dump(mode="json") for r in rows]},
                          sort_keys=True).encode()


# ── the owner's HTTP path (docs/29 §23.2) ──────────────────────────────────


def _refusal(outcome: AuthorizationOutcome) -> AppError:
    if outcome.surface is DenialSurface.PROHIBITED:
        return AppError(ErrorCode.PROHIBITED, "this action is never allowed")
    if outcome.surface is DenialSurface.FORBIDDEN:
        return AppError(ErrorCode.UNAUTHORIZED, "not permitted")
    return AppError(ErrorCode.NOT_FOUND, "not found")


_NOT_FOUND = AppError(ErrorCode.NOT_FOUND, "not found")


class AgentFactoryFacade:
    """`AgentFactoryPort` (server/gateway/agents_port.py)."""

    def __init__(self, *, factory: AgentFactory, core: SecurityCore,
                 tasks: "AgentTaskFacade | None" = None) -> None:
        self._factory = factory
        self._core = core
        self._tasks = tasks

    async def _authorize(self, session: AsyncSession, audit: AuditLogger, request: AccessRequest) -> AuthorizationOutcome:
        return await self._core.engine.authorize(session, request, audit=audit)

    async def _confirm_or_refuse(self, session: AsyncSession, outcome: AuthorizationOutcome, *, action: str,
                                 card: dict[str, Any] | None = None) -> None:
        if outcome.needs_confirmation:
            binding = outcome.confirmation_required_for
            assert binding is not None
            issued = await self._core.confirmations.issue(session, binding=binding,
                                                          risk_category=outcome.risk_category)
            details: dict[str, Any] = {
                "confirmation_token": issued.token, "expires_at": issued.expires_at.isoformat(),
                "action": action, "risk_category": outcome.risk_category.value,
            }
            if card is not None:
                details["card"] = card
            raise AppError(ErrorCode.CONFIRMATION_REQUIRED, "this needs your confirmation", details=details)
        if not outcome.allowed:
            raise _refusal(outcome)

    async def _audit(self, audit: AuditLogger, principal: Principal, action: AuditAction, resource: str,
                     result: AuditResult = AuditResult.SUCCESS) -> None:
        await audit.record(actor=AuditActor.USER, action=action, resource=resource, result=result,
                           user_id=principal.user_id, device_id=principal.device_id, session_id=principal.session_id)

    async def _owned(self, session: AsyncSession, audit: AuditLogger, principal: Principal, agent_id: uuid.UUID,
                     operation: Operation):
        outcome = await self._authorize(session, audit, AccessRequest(
            principal=principal, operation=operation, resource_type=ResourceType.AGENTDEFINITION,
            resource_ref=str(agent_id)))
        if not outcome.allowed:
            raise _refusal(outcome)
        loaded = await self._factory.service.load(session, agent_id)
        if loaded is None or loaded[0].owner_user_id != principal.user_id:
            raise _NOT_FOUND
        return loaded

    async def compile(self, session: AsyncSession, *, principal: Principal, draft: Any, agent_id: uuid.UUID | None,
                      audit: AuditLogger) -> CompileOutcome:
        parsed, fields = parse_draft(draft)
        if parsed is None:
            await self._audit(audit, principal, AuditAction.AGENT_DRAFT_REJECTED,
                              f"agentdraft:fields:{','.join(fields)}", AuditResult.BLOCKED)
            raise AppError(ErrorCode.VALIDATION_FAILED, "the draft is not valid", details={"fields": list(fields)})
        graph_id = principal.active_graph_id
        if graph_id is not None and not await self._core.graph_repository.is_active_member(
            session, graph_id=graph_id, user_id=principal.user_id
        ):
            # 04 §9: the session's active graph is a cached claim, re-checked live.
            raise _NOT_FOUND
        agent = None
        if agent_id is not None:
            definition, spec = await self._owned(session, audit, principal, agent_id, Operation.READ)
            if spec is None:
                raise AppError(ErrorCode.CONFLICT, "that agent is revoked and cannot be updated")
            agent, graph_id = definition, definition.graph_id
        outcome = await self._factory.compile(session, user_id=principal.user_id, graph_id=graph_id,
                                              draft=parsed, task_id=None, agent=agent)
        if outcome.compile_id is not None:
            await self._audit(audit, principal, AuditAction.AGENT_COMPILED, f"agentpreview:{outcome.compile_id}")
        return outcome

    async def preview(self, session: AsyncSession, *, principal: Principal, compile_id: uuid.UUID,
                      audit: AuditLogger) -> CompiledAgentSpecView:
        row = await session.get(AgentCompilePreviewRow, compile_id)
        service = self._factory.service
        if row is None or row.owner_user_id != principal.user_id:
            raise _NOT_FOUND
        loaded = await service.load_preview(session, compile_id=compile_id, owner_user_id=principal.user_id,
                                            task_id=row.task_id,
                                            for_agent=row.agent_id if row.base_version is not None else None)
        if loaded is None:
            raise _NOT_FOUND
        try:
            spec = service.preview_spec(loaded)
        except PreviewRefused:
            raise _NOT_FOUND from None
        return spec_view(spec, self._factory.registries)

    def _reminders_unavailable(self, spec: CompiledAgentSpec) -> None:
        if spec.trigger.kind is TriggerKind.REMINDER and self._factory.reminders is None:
            # docs/29 §17.1: a reminder agent needs the scheduler; without one
            # it is refused before anything is confirmed or written.
            raise AppError(ErrorCode.CONFLICT, "this server has no scheduler for the agent's reminder",
                           details={"reason": "reminders_unavailable"})

    async def _plan(self, session: AsyncSession, audit: AuditLogger, principal: Principal,
                    spec: CompiledAgentSpec) -> PreparedReminder | None:
        """docs/29 §17.1: every check on the reminder, before the change is written."""

        if self._factory.reminders is None:
            return None
        return await self._factory.reminders.plan(session, audit, principal=principal, spec=spec)

    async def _commit(self, session: AsyncSession, audit: AuditLogger, planned: PreparedReminder | None) -> None:
        if self._factory.reminders is not None:
            await self._factory.reminders.commit(session, audit, planned)

    async def _unschedule(self, session: AsyncSession, audit: AuditLogger, principal: Principal,
                          agent_id: uuid.UUID) -> None:
        if self._factory.reminders is not None:
            await self._factory.reminders.cancel(session, audit, agent_id=agent_id, owner_user_id=principal.user_id,
                                                 device_id=principal.device_id, session_id=principal.session_id)

    async def create(self, session: AsyncSession, *, principal: Principal, body: CreateAgentRequest,
                     confirmation_token: str | None, audit: AuditLogger) -> AgentView:
        service = self._factory.service
        preview = await service.load_preview(session, compile_id=body.compile_id, owner_user_id=principal.user_id,
                                             task_id=None, for_agent=None)
        if preview is None:
            raise _NOT_FOUND
        try:
            spec = service.preview_spec(preview)
        except PreviewRefused as exc:
            raise AppError(ErrorCode.CONFLICT, "this preview cannot be applied", details={"reason": exc.reason}) from None
        self._reminders_unavailable(spec)
        # D1 on the agent's graph, live; `consequential` → a token bound to
        # exactly this preview and its hash.
        outcome = await self._authorize(session, audit, AccessRequest(
            principal=principal, operation=Operation.CREATE, resource_type=ResourceType.AGENTDEFINITION,
            graph_id=spec.graph_id, confirmation_token=confirmation_token,
            arguments={"compile_id": str(body.compile_id), "spec_hash": spec.spec_hash},
        ))
        await self._confirm_or_refuse(session, outcome, action="create_agent",
                                      card=_card_json(spec, self._factory.registries))
        planned = await self._plan(session, audit, principal, spec)
        try:
            definition, spec = await service.create_from_preview(
                session, preview, created_by_device_id=principal.device_id, created_from_task_id=None)
        except PreviewRefused as exc:
            raise AppError(ErrorCode.CONFLICT, "this preview cannot be applied", details={"reason": exc.reason}) from None
        await self._commit(session, audit, planned)
        await self._audit(audit, principal, AuditAction.AGENT_CREATED, f"agentdefinition:{definition.agent_id}")
        return service.view(definition, spec)

    async def list(self, session: AsyncSession, *, principal: Principal, audit: AuditLogger) -> AgentListResponse:
        service = self._factory.service
        rows = await service.list_for_owner(session, principal.user_id)
        # Re-checked whatever the query returned: private means the owner only.
        return AgentListResponse(items=tuple(
            service.view(d, s) for d, s in rows if d.owner_user_id == principal.user_id
        ))

    async def get(self, session: AsyncSession, *, principal: Principal, agent_id: uuid.UUID,
                  audit: AuditLogger) -> AgentDetail:
        definition, spec = await self._owned(session, audit, principal, agent_id, Operation.READ)
        service = self._factory.service
        return AgentDetail(agent=service.view(definition, spec),
                           spec=spec_view(spec, self._factory.registries) if spec is not None else None)

    async def update(self, session: AsyncSession, *, principal: Principal, agent_id: uuid.UUID,
                     body: CreateAgentRequest, confirmation_token: str | None, audit: AuditLogger) -> AgentView:
        service = self._factory.service
        # Owner-bound lookup first: another user's agent id finds no preview
        # of theirs, so nothing about the agent is revealed.
        preview = await service.load_preview(session, compile_id=body.compile_id, owner_user_id=principal.user_id,
                                             task_id=None, for_agent=agent_id)
        if preview is None:
            raise _NOT_FOUND
        try:
            spec = service.preview_spec(preview)
        except PreviewRefused as exc:
            raise AppError(ErrorCode.CONFLICT, "this preview cannot be applied", details={"reason": exc.reason}) from None
        self._reminders_unavailable(spec)
        outcome = await self._authorize(session, audit, AccessRequest(
            principal=principal, operation=Operation.WRITE, resource_type=ResourceType.AGENTDEFINITION,
            resource_ref=str(agent_id), confirmation_token=confirmation_token,
            arguments={"compile_id": str(body.compile_id), "spec_hash": spec.spec_hash},
        ))
        await self._confirm_or_refuse(session, outcome, action="update_agent",
                                      card=_card_json(spec, self._factory.registries))
        loaded = await service.load(session, agent_id)
        if loaded is None or loaded[0].owner_user_id != principal.user_id:
            raise _NOT_FOUND
        planned = (await self._plan(session, audit, principal, spec)
                   if loaded[0].status in _REMINDED_AFTER_UPDATE else None)
        try:
            definition, new = await service.update_from_preview(session, loaded[0], preview)
        except PreviewRefused as exc:
            raise AppError(ErrorCode.CONFLICT, "this preview cannot be applied", details={"reason": exc.reason}) from None
        # docs/29 §17.1: the reminder follows the new version (or goes).
        await self._unschedule(session, audit, principal, agent_id)
        if definition.status == AgentStatus.ACTIVE.value:
            await self._commit(session, audit, planned)
        await self._audit(audit, principal, AuditAction.AGENT_UPDATED,
                          f"agentdefinition:{definition.agent_id}:v{new.version}")
        return service.view(definition, new)

    async def delete(self, session: AsyncSession, *, principal: Principal, agent_id: uuid.UUID,
                     confirmation_token: str | None, audit: AuditLogger) -> None:
        outcome = await self._authorize(session, audit, AccessRequest(
            principal=principal, operation=Operation.DELETE, resource_type=ResourceType.AGENTDEFINITION,
            resource_ref=str(agent_id), confirmation_token=confirmation_token,
        ))
        await self._confirm_or_refuse(session, outcome, action="delete_agent")
        loaded = await self._factory.service.load(session, agent_id)
        if loaded is None or loaded[0].owner_user_id != principal.user_id:
            raise _NOT_FOUND
        if self._tasks is not None:
            # docs/29 §14.4: no run of a deleted agent continues, and no paused
            # action of one can still be confirmed.
            await self._stop_runs(session, audit, principal, agent_id, CancelReason.DELETED)
            # The runtime keeps nothing past the agent (notebook, inbox).
            port = _PresentUserRun(tasks=self._tasks, factory=self._factory, session=session,
                                   principal=principal, audit=audit)
            await NativeRuntimeProvider(port).deprovision(
                RuntimeRef(runtime_id=NATIVE_RUNTIME_ID, agent_id=agent_id, version=loaded[0].current_version))
        await self._unschedule(session, audit, principal, agent_id)
        await self._factory.service.delete(session, loaded[0])
        await self._audit(audit, principal, AuditAction.AGENT_DELETED, f"agentdefinition:{agent_id}")


    # ── runs ────────────────────────────────────────────────────────────

    async def _refuse_run(self, audit: AuditLogger, principal: Principal, agent_id: uuid.UUID, reason: str,
                          message: str, code: ErrorCode = ErrorCode.CONFLICT) -> AppError:
        await self._audit(audit, principal, AuditAction.AGENT_RUN_REFUSED, f"agentdefinition:{agent_id}:{reason}",
                          AuditResult.BLOCKED)
        return AppError(code, message, details={"reason": reason})

    async def _tapped(self, session: AsyncSession, audit: AuditLogger, principal: Principal,
                      agent_id: uuid.UUID, delivery_id: uuid.UUID) -> None:
        """docs/29 §17.1: a reminder tap names the delivery it came from. It is
        accepted only if this very device of this very owner was sent that
        reminder, and the reminder is this agent's — otherwise it does not
        exist for the caller (`404`, as anyone else's would). Read only; it
        authorizes nothing (the run is decided like any on-demand run)."""

        delivery = await session.get(ReminderDelivery, delivery_id)
        job = await session.get(ScheduledJob, delivery.job_id) if delivery is not None else None
        if (delivery is None or job is None or delivery.status not in _RECEIVED
                or delivery.user_id != principal.user_id or delivery.device_id != principal.device_id
                or job.owner_user_id != principal.user_id or job.agent_id != agent_id):
            await self._audit(audit, principal, AuditAction.AGENT_RUN_REFUSED,
                              f"agentdefinition:{agent_id}:reminder_tap_unknown", AuditResult.BLOCKED)
            raise _NOT_FOUND

    async def run(self, session: AsyncSession, *, principal: Principal, agent_id: uuid.UUID,
                  audit: AuditLogger, reminder_delivery_id: uuid.UUID | None = None) -> AgentRunView:
        """docs/29 §7.4 run_now: an on-demand run by the present owner — or,
        Phase 4, the same run tapped from one of the agent's reminders.

        Nothing about the run comes from the request. The engine authorizes
        the owner on the definition; the stored spec is hash-verified; the
        definition must be active, in the session's own (live) graph, on its
        current template, profile and runtime. The run is then an ordinary task
        of this principal — so every tool call in it still passes activation,
        the mode ceiling, 04 and confirmation, and the definition is
        re-validated at every step."""

        tasks = self._run_tasks()
        definition, spec = await self._owned(session, audit, principal, agent_id, Operation.READ)
        service = self._factory.service
        if reminder_delivery_id is not None:
            await self._tapped(session, audit, principal, agent_id, reminder_delivery_id)
            tapped = await service.run_for_delivery(session, reminder_delivery_id)
            if tapped is not None:
                # The same tap again (a double tap, a retry after no answer):
                # the run it already started, never a second one.
                return await self._tap_replayed(session, audit, principal, tapped)
        if spec is None:
            raise await self._refuse_run(audit, principal, agent_id, "agent_revoked",
                                         "this agent is revoked and cannot run")
        if definition.status != AgentStatus.ACTIVE.value:
            raise await self._refuse_run(audit, principal, agent_id, definition.status,
                                         "this agent is not active")
        graph_id = principal.active_graph_id
        if graph_id is not None and not await self._core.graph_repository.is_active_member(
            session, graph_id=graph_id, user_id=principal.user_id
        ):
            raise _NOT_FOUND
        if spec.graph_id != graph_id:
            raise await self._refuse_run(audit, principal, agent_id, "graph_mismatch",
                                         "this agent belongs to another graph than this session's")
        profile = await self._still_runnable(session, audit, principal, definition, spec)
        # docs/29 §17: the month's budget. A spent month refuses the run; the
        # run's own ceiling is never more than what is left of the month (a
        # zero monthly budget leaves only free — local — model calls).
        spent = await service.month_spend(session, agent_id)
        if spec.budget.per_month > 0 and spent >= spec.budget.per_month:
            raise await self._refuse_run(audit, principal, agent_id, AgentFailureCode.AGENT_BUDGET_EXHAUSTED.value,
                                         "this agent's monthly budget is spent", code=ErrorCode.RATE_LIMITED)
        run_budget = min(spec.budget.per_run, max(0.0, spec.budget.per_month - spent))
        run_id = uuid.uuid4()
        try:
            async with session.begin_nested():
                run = await service.create_run(session, spec=spec, run_id=run_id,
                                               reminder_delivery_id=reminder_delivery_id)
        except IntegrityError:
            # Two taps raced past the lookup: the store's uniqueness decides,
            # and this one gets the run the other started.
            tapped = (await service.run_for_delivery(session, reminder_delivery_id)
                      if reminder_delivery_id is not None else None)
            if tapped is None:
                raise
            return await self._tap_replayed(session, audit, principal, tapped)
        deadline = self._factory.run_deadline(spec)
        # docs/29 §11.3: the run's two gateway tokens — the only credential
        # its runtime holds, and only for this run.
        issued = await self._factory.gateway.issue(session, run, deadline=deadline)
        await self._audit(audit, principal, AuditAction.AGENT_TOKEN_ISSUED, f"agentrun:{run_id}:model,tool")
        binding = AgentRunBinding.from_spec(spec, run_id=run_id, model_ref=profile.profile.model_ref,
                                            budget_per_run=run_budget,
                                            input_text=self._factory.run_input(spec, run_id))
        provider_port = _PresentUserRun(tasks=tasks, factory=self._factory, session=session, principal=principal,
                                        audit=audit, binding=binding)
        provider = NativeRuntimeProvider(provider_port)
        await provider.provision(spec)
        try:
            await provider.start_run(AgentRunContext(
                run_id=run_id, agent_id=spec.agent_id, version=spec.version, spec_hash=spec.spec_hash,
                input_text=binding.input_text, deadline=deadline,
                run_token=issued.tool, model_run_token=issued.model,
            ))
        except AppError:
            # No task was created (a limit, the supervisor latch): the run
            # ends here, failed, and says so.
            await service.run_finished(session, run_id, status=AgentTaskStatus.FAILED,
                                       failure="not_started", cost=0.0)
            await session.commit()
            raise
        result = provider_port.result
        if result is not None and result.status is AgentTaskStatus.AWAITING_CONFIRMATION:
            await service.run_waiting(session, run_id)
        run = await service.get_run(session, run_id) or run
        return service.run_view(run, task=result, inbox_item_id=await service.inbox_item_for_run(session, run_id))

    async def _tap_replayed(self, session: AsyncSession, audit: AuditLogger, principal: Principal,
                            run: AgentRunRow) -> AgentRunView:
        service = self._factory.service
        task = (await self._run_tasks().get(session, principal=principal, task_id=run.task_id, audit=audit)
                if run.task_id is not None else None)
        return service.run_view(run, task=task, inbox_item_id=await service.inbox_item_for_run(session, run.run_id))

    def _run_tasks(self) -> "AgentTaskFacade":
        if self._tasks is None:
            raise AppError(ErrorCode.DEPENDENCY_UNAVAILABLE, "agent runs are not available",
                           details={"dependency": "agents"})
        return self._tasks

    async def _still_runnable(self, session: AsyncSession, audit: AuditLogger, principal: Principal,
                              definition: AgentDefinitionRow, spec: CompiledAgentSpec):
        """What a new run — and a resume — must still satisfy against the
        operator's *current* registries and the owner's current model policy:
        the spec's template version, its model profile and its runtime. A
        template that moved on sends the agent back for re-approval."""

        registries = self._factory.registries
        agent_id = definition.agent_id
        if revalidation_required(spec, registries.enabled_templates):
            await self._factory.service.mark_needs_reapproval(session, definition, "template_changed")
            # docs/29 §14.1: an agent awaiting re-approval is not reminded.
            await self._unschedule(session, audit, principal, agent_id)
            raise await self._refuse_run(audit, principal, agent_id, "needs_reapproval",
                                         "this agent must be recompiled and re-approved")
        selection = spec.selection
        profile = registries.model_profiles.get(selection.model_profile_id)
        owner = await self._factory.owner_context(session, user_id=principal.user_id, graph_id=spec.graph_id)
        if (profile is None or not profile.profile.enabled
                or profile.profile.version != selection.model_profile_version
                or (profile.profile_id not in registries.open_to_all
                    and profile.profile.model_ref != owner.owner_primary_model_ref)):
            raise await self._refuse_run(audit, principal, agent_id, "model_profile_unavailable",
                                         "this agent's model profile is not available to you")
        runtime = registries.runtimes.get(selection.runtime_id)
        if runtime is None or not runtime.enabled or selection.runtime_id != "native":
            raise await self._refuse_run(audit, principal, agent_id, "runtime_unavailable",
                                         "this agent's runtime is not available")
        return profile

    async def _stop_runs(self, session: AsyncSession, audit: AuditLogger, principal: Principal,
                         agent_id: uuid.UUID, reason: CancelReason) -> None:
        """Stop every live run of the agent — the safe direction, never refused
        to its owner and never confirmed."""

        service = self._factory.service
        live = await service.live_runs(session, agent_id)
        if not live:
            return
        port = _PresentUserRun(tasks=self._run_tasks(), factory=self._factory, session=session, principal=principal,
                               audit=audit)
        provider = NativeRuntimeProvider(port)
        for run in live:
            if run.owner_user_id != principal.user_id:
                continue
            await provider.cancel_run(RunHandle(runtime_id=NATIVE_RUNTIME_ID, run_id=run.run_id), reason)
            await self._audit(audit, principal, AuditAction.AGENT_RUN_CANCELLED,
                              f"agentrun:{run.run_id}:{reason.value}")

    async def cancel_run(self, session: AsyncSession, *, principal: Principal, agent_id: uuid.UUID,
                         run_id: uuid.UUID, audit: AuditLogger) -> AgentRunView:
        run = await self._owned_run(session, audit, principal, agent_id, run_id)
        service = self._factory.service
        if run.finished_at is None:
            port = _PresentUserRun(tasks=self._run_tasks(), factory=self._factory, session=session,
                                   principal=principal, audit=audit)
            await NativeRuntimeProvider(port).cancel_run(
                RunHandle(runtime_id=NATIVE_RUNTIME_ID, run_id=run_id), CancelReason.OWNER_STOP)
            await self._audit(audit, principal, AuditAction.AGENT_RUN_CANCELLED,
                              f"agentrun:{run_id}:{CancelReason.OWNER_STOP.value}")
        run = await service.get_run(session, run_id) or run
        return service.run_view(run)

    async def pause(self, session: AsyncSession, *, principal: Principal, agent_id: uuid.UUID,
                    audit: AuditLogger) -> AgentView:
        """docs/29 §14.1: no confirmation — it only removes. Live runs stop."""

        definition, spec = await self._owned(session, audit, principal, agent_id, Operation.READ)
        service = self._factory.service
        if await service.pause(session, definition):
            await self._audit(audit, principal, AuditAction.AGENT_PAUSED, f"agentdefinition:{agent_id}")
        # docs/29 §14.1 / §17.1: a paused agent's reminder is suppressed.
        await self._unschedule(session, audit, principal, agent_id)
        await self._stop_runs(session, audit, principal, agent_id, CancelReason.OWNER_STOP)
        return service.view(definition, spec)

    async def resume(self, session: AsyncSession, *, principal: Principal, agent_id: uuid.UUID,
                     confirmation_token: str | None, audit: AuditLogger) -> AgentView:
        """docs/29 §14.1: the direction that gives authority back. Every check
        a new run faces is re-run, then the engine decides (`write` on the
        definition is consequential: the owner confirms, with a token bound to
        this agent's current spec hash)."""

        definition, spec = await self._owned(session, audit, principal, agent_id, Operation.READ)
        service = self._factory.service
        if spec is None:
            raise await self._refuse_run(audit, principal, agent_id, "agent_revoked",
                                         "this agent is revoked and cannot be resumed")
        if definition.status_reason == OPERATOR_PAUSED:
            # docs/29 §14.3: the operator's pause holds until the operator
            # releases it.
            raise await self._refuse_run(audit, principal, agent_id, OPERATOR_PAUSED,
                                         "this agent was paused by the server operator")
        if definition.status == AgentStatus.ACTIVE.value:
            return service.view(definition, spec)
        if definition.status != AgentStatus.PAUSED.value:
            raise await self._refuse_run(audit, principal, agent_id, definition.status,
                                         "only a paused agent can be resumed")
        await self._still_runnable(session, audit, principal, definition, spec)
        outcome = await self._authorize(session, audit, AccessRequest(
            principal=principal, operation=Operation.WRITE, resource_type=ResourceType.AGENTDEFINITION,
            resource_ref=str(agent_id), confirmation_token=confirmation_token,
            arguments={"action": "resume", "spec_hash": spec.spec_hash},
        ))
        await self._confirm_or_refuse(session, outcome, action="resume_agent",
                                      card=_card_json(spec, self._factory.registries))
        loaded = await service.load(session, agent_id, fresh=True)
        if loaded is None or loaded[0].owner_user_id != principal.user_id:
            raise _NOT_FOUND
        definition, current = loaded
        if definition.status_reason == OPERATOR_PAUSED:
            # Re-read after the engine's decision: an operator's pause that
            # landed meanwhile still holds.
            raise await self._refuse_run(audit, principal, agent_id, OPERATOR_PAUSED,
                                         "this agent was paused by the server operator")
        if current is None or current.spec_hash != spec.spec_hash or definition.status != AgentStatus.PAUSED.value:
            raise await self._refuse_run(audit, principal, agent_id, "spec_changed",
                                         "the agent changed while the resume was being confirmed")
        planned = await self._plan(session, audit, principal, current)
        await service.resume(session, definition)
        await self._commit(session, audit, planned)
        await self._audit(audit, principal, AuditAction.AGENT_RESUMED, f"agentdefinition:{agent_id}")
        return service.view(definition, current)

    async def inbox(self, session: AsyncSession, *, principal: Principal, agent_id: uuid.UUID | None,
                    unread: bool, audit: AuditLogger) -> AgentInboxResponse:
        """The caller's own inbox — the query is scoped to the authenticated
        owner, and each item is re-checked against it."""

        service = self._factory.service
        if agent_id is not None:
            await self._owned(session, audit, principal, agent_id, Operation.READ)
        items = await service.inbox_list(session, owner_user_id=principal.user_id, agent_id=agent_id, unread=unread)
        return AgentInboxResponse(items=tuple([
            await service.inbox_view(session, i) for i in items if i.owner_user_id == principal.user_id
        ]))

    async def _owned_item(self, session: AsyncSession, audit: AuditLogger, principal: Principal,
                          item_id: uuid.UUID):
        item = await self._factory.service.inbox_item(session, item_id)
        if item is None or item.owner_user_id != principal.user_id:
            raise _NOT_FOUND
        # The engine still decides: the item is reachable only through the
        # owner's own (live, undeleted) agent.
        await self._owned(session, audit, principal, item.agent_id, Operation.READ)
        return item

    async def mark_inbox_read(self, session: AsyncSession, *, principal: Principal, item_id: uuid.UUID,
                              audit: AuditLogger) -> AgentInboxItemView:
        item = await self._owned_item(session, audit, principal, item_id)
        await self._factory.service.inbox_mark_read(session, item)
        return await self._factory.service.inbox_view(session, item)

    async def delete_inbox_item(self, session: AsyncSession, *, principal: Principal, item_id: uuid.UUID,
                                audit: AuditLogger) -> None:
        item = await self._owned_item(session, audit, principal, item_id)
        await self._factory.service.inbox_delete(session, item)
        await self._audit(audit, principal, AuditAction.AGENT_INBOX_DELETED, f"agentinbox:{item_id}")

    async def export(self, session: AsyncSession, *, principal: Principal, agent_id: uuid.UUID,
                     audit: AuditLogger) -> AgentExport:
        """docs/29 §23.2: owner-only (engine READ, then the owner check); the
        runtime's own state comes from the provider's `export_state`."""

        definition, spec = await self._owned(session, audit, principal, agent_id, Operation.READ)
        service = self._factory.service
        port = _PresentUserRun(tasks=self._run_tasks(), factory=self._factory, session=session, principal=principal,
                               audit=audit)
        state = await NativeRuntimeProvider(port).export_state(
            RuntimeRef(runtime_id=NATIVE_RUNTIME_ID, agent_id=agent_id, version=definition.current_version))
        runtime_state = json.loads(state or b"{}")
        runs = await service.list_runs(session, agent_id=agent_id, owner_user_id=principal.user_id, limit=1000)
        inbox = await service.inbox_list(session, owner_user_id=principal.user_id, agent_id=agent_id, limit=1000)
        exported = AgentExport(
            exported_at=datetime.now(timezone.utc),
            agent=service.view(definition, spec),
            spec_versions=tuple(
                AgentSpecVersionExport(version=row.version, spec_hash=row.spec_hash,
                                       created_at=s.created_at, spec=s)
                for row, s in await service.verified_spec_versions(session, agent_id)
            ),
            runs=tuple([service.run_view(r, inbox_item_id=await service.inbox_item_for_run(session, r.run_id))
                        for r in runs if r.owner_user_id == principal.user_id]),
            notebook=tuple(NotebookEntryView.model_validate(n) for n in runtime_state.get("notebook", [])),
            inbox=tuple([await service.inbox_view(session, i) for i in inbox if i.owner_user_id == principal.user_id]),
        )
        await self._audit(audit, principal, AuditAction.AGENT_EXPORTED, f"agentdefinition:{agent_id}")
        return exported

    async def notebook(self, session: AsyncSession, *, principal: Principal, agent_id: uuid.UUID,
                       audit: AuditLogger) -> NotebookResponse:
        await self._owned(session, audit, principal, agent_id, Operation.READ)
        rows = await self._factory.service.notebook_entries(session, agent_id, principal.user_id)
        return NotebookResponse(items=tuple(
            self._factory.service.notebook_view(r) for r in rows if r.owner_user_id == principal.user_id
        ))

    async def clear_notebook(self, session: AsyncSession, *, principal: Principal, agent_id: uuid.UUID,
                             audit: AuditLogger) -> None:
        """Removing the agent's own notes only removes: no confirmation."""

        await self._owned(session, audit, principal, agent_id, Operation.READ)
        await self._factory.service.notebook_clear(session, agent_id)
        await self._audit(audit, principal, AuditAction.AGENT_NOTEBOOK_CLEARED, f"agentnotebook:{agent_id}")

    # ── the Judge's purpose suggestions (docs/29 §18) ──────────────────

    async def _candidate_view(self, session: AsyncSession, row) -> AgentPurposeCandidateView:
        run = await self._factory.service.run_for_task(session, row.task_id)
        return AgentPurposeCandidateView(
            candidate_id=row.candidate_id, agent_id=row.agent_id, run_id=run.run_id if run is not None else None,
            status=row.status, proposed_purpose=redact_secrets(row.proposed_value)[0],
            expected_effect=redact_secrets(row.expected_effect or "")[0], created_at=row.created_at,
            decided_at=row.decided_at,
        )

    async def candidates(self, session: AsyncSession, *, principal: Principal, agent_id: uuid.UUID,
                         audit: AuditLogger) -> AgentPurposeCandidateList:
        """Only the agent's owner sees the Judge's suggestions for it."""

        await self._owned(session, audit, principal, agent_id, Operation.READ)
        rows = await self._factory.service.purpose_candidates(session, agent_id, principal.user_id)
        return AgentPurposeCandidateList(items=tuple([
            await self._candidate_view(session, r) for r in rows if r.source_user_id == principal.user_id
        ]))

    async def _owned_candidate(self, session: AsyncSession, audit: AuditLogger, principal: Principal,
                               agent_id: uuid.UUID, candidate_id: uuid.UUID):
        definition, spec = await self._owned(session, audit, principal, agent_id, Operation.READ)
        row = await self._factory.service.purpose_candidate(session, candidate_id)
        if row is None or row.agent_id != agent_id or row.source_user_id != principal.user_id:
            raise _NOT_FOUND
        if row.status != "pending":
            raise AppError(ErrorCode.CONFLICT, "this suggestion was already decided",
                           details={"reason": "candidate_decided"})
        return definition, spec, row

    async def compile_candidate(self, session: AsyncSession, *, principal: Principal, agent_id: uuid.UUID,
                                candidate_id: uuid.UUID, audit: AuditLogger) -> CompileOutcome:
        """docs/29 §18: the owner turns a purpose suggestion into an update
        preview — the agent's own spec, recompiled with only the new purpose.
        It applies nothing: the update is then the ordinary, confirmed
        `PATCH /agents/{id}`. A recompile that would change anything but the
        purpose (the envelope, a ceiling, the budget, the trigger, the runtime
        or the model) is refused."""

        definition, spec, row = await self._owned_candidate(session, audit, principal, agent_id, candidate_id)
        service = self._factory.service
        if spec is None:
            raise AppError(ErrorCode.CONFLICT, "that agent is revoked", details={"reason": "agent_revoked"})
        try:
            draft = redraft_with_purpose(spec, row.proposed_value, self._factory.registries)
        except ValidationError:
            raise AppError(ErrorCode.CONFLICT, "this suggestion is not a valid purpose",
                           details={"reason": "candidate_invalid"}) from None
        outcome = await self._factory.compile(session, user_id=principal.user_id, graph_id=definition.graph_id,
                                              draft=draft, task_id=None, agent=definition)
        if outcome.kind != "compiled" or outcome.compile_id is None:
            raise AppError(ErrorCode.CONFLICT, "this agent cannot be recompiled as it is",
                           details={"reason": "not_compilable", "reason_codes": list(outcome.reason_codes)})
        preview = await service.load_preview(session, compile_id=outcome.compile_id, owner_user_id=principal.user_id,
                                             task_id=None, for_agent=agent_id)
        if preview is None or authority_of(service.preview_spec(preview)) != authority_of(spec):
            await service.discard_preview(session, outcome.compile_id)
            raise AppError(ErrorCode.CONFLICT, "applying this suggestion would change more than the purpose",
                           details={"reason": "changes_more_than_purpose"})
        await service.decide_candidate(session, row, approved=True, reason=f"compiled:{outcome.compile_id}")
        await self._audit(audit, principal, AuditAction.AGENT_CANDIDATE_COMPILED,
                          f"candidate:{candidate_id}:agentpreview:{outcome.compile_id}")
        return outcome

    async def dismiss_candidate(self, session: AsyncSession, *, principal: Principal, agent_id: uuid.UUID,
                                candidate_id: uuid.UUID, audit: AuditLogger) -> AgentPurposeCandidateView:
        _, _, row = await self._owned_candidate(session, audit, principal, agent_id, candidate_id)
        await self._factory.service.decide_candidate(session, row, approved=False, reason="owner_dismissed")
        await self._audit(audit, principal, AuditAction.AGENT_CANDIDATE_DISMISSED, f"candidate:{candidate_id}")
        return await self._candidate_view(session, row)

    async def _owned_run(self, session: AsyncSession, audit: AuditLogger, principal: Principal,
                         agent_id: uuid.UUID, run_id: uuid.UUID) -> AgentRunRow:
        await self._owned(session, audit, principal, agent_id, Operation.READ)
        run = await self._factory.service.get_run(session, run_id)
        if run is None or run.agent_id != agent_id or run.owner_user_id != principal.user_id:
            raise _NOT_FOUND
        return run

    async def list_runs(self, session: AsyncSession, *, principal: Principal, agent_id: uuid.UUID,
                        audit: AuditLogger) -> AgentRunListResponse:
        await self._owned(session, audit, principal, agent_id, Operation.READ)
        service = self._factory.service
        runs = await service.list_runs(session, agent_id=agent_id, owner_user_id=principal.user_id)
        return AgentRunListResponse(items=tuple([
            service.run_view(r, inbox_item_id=await service.inbox_item_for_run(session, r.run_id))
            for r in runs if r.owner_user_id == principal.user_id
        ]))

    async def get_run(self, session: AsyncSession, *, principal: Principal, agent_id: uuid.UUID,
                      run_id: uuid.UUID, audit: AuditLogger) -> AgentRunView:
        run = await self._owned_run(session, audit, principal, agent_id, run_id)
        service = self._factory.service
        return service.run_view(run, inbox_item_id=await service.inbox_item_for_run(session, run_id))


__all__ = [
    "CURRENT_AGENT_FACTORY_SCOPE",
    "AgentDefinitionLoader",
    "AgentFactory",
    "AgentFactoryFacade",
    "AgentReminders",
    "AgentRunCoordinator",
    "AgentToolAdapter",
    "agent_factory_scope",
    "agent_tool_definitions",
    "model_entry_facts",
    "registries_from_config",
]
