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
import json
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.agents.compiler import OwnerContext, parse_draft
from server.agents.registry import AgentRegistries, ModelEntryFacts, build_registries
from server.agents.rendering import render_card, spec_view
from server.agents.service import AgentDefinitionService, PreviewRefused
from server.capabilities.registry import (
    AGENT_DEFINE_CAPABILITY,
    AGENT_DELETE_CAPABILITY,
    AGENT_INSPECT_CAPABILITY,
)
from server.config.schema import LOCAL_MODEL_PROVIDERS, AppConfig
from server.gateway.errors import AppError
from server.gateway.security import SecurityCore
from server.graph.authorization import AccessRequest, AuthorizationOutcome
from server.graph.ports import ResourceDescriptor
from server.models.factory import IMPLEMENTED_PROVIDERS
from server.net.destinations import hostname_allowed
from server.scheduler.schedule import parse_schedule
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.storage.models import AgentCompilePreviewRow, AgentConfiguration, AgentDefinitionRow, AgentTask
from server.tools.registry import ToolDefinition
from shared.schemas.agent import ExecutionPlatform, OperationSpec, ToolInvocation, ToolOutput
from shared.schemas.agent_config import ToolContract
from shared.schemas.agent_factory import (
    AgentDetail,
    AgentDraft,
    AgentListResponse,
    AgentStatus,
    AgentView,
    CompiledAgentSpec,
    CompiledAgentSpecView,
    CompileOutcome,
    CreateAgentRequest,
)
from shared.schemas.authorization import DenialSurface, Operation, Principal, ResourceType
from shared.schemas.enums import AgentConfigScopeType, AuditActor, AuditResult, RiskCategory, Visibility
from shared.schemas.errors import ErrorCode

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


class AgentFactory:
    """What both paths share: the service, and the owner context the
    compiler needs — built from live server state, never from a draft."""

    def __init__(self, *, config: AppConfig, service: AgentDefinitionService, core: SecurityCore) -> None:
        self._config = config
        self._service = service
        self._core = core

    @property
    def service(self) -> AgentDefinitionService:
        return self._service

    @property
    def registries(self) -> AgentRegistries:
        return self._service.registries

    async def _primary_model_ref(self, session: AsyncSession, *, user_id: uuid.UUID,
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
            owner_primary_model_ref=await self._primary_model_ref(session, user_id=user_id, graph_id=graph_id),
            budget_per_run_policy=agents.default_budget_per_run,
            budget_per_month_policy=agents.default_budget_per_month,
            active_agent_count=await self._service.active_count(session, user_id),
            max_agents_per_user=agents.max_agents_per_user,
            runtime_max_seconds=bounds.wall_clock_timeout_seconds,
            runtime_max_model_calls=bounds.max_model_calls,
            runtime_max_tool_calls=bounds.max_tool_calls,
            url_allowed=self.url_allowed,
        )

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
            definition, spec = await service.create_from_preview(
                scope.session, preview, created_by_device_id=invocation.device_id,
                created_from_task_id=invocation.task_id)
        except PreviewRefused as exc:
            await self._audit(scope, invocation, AuditAction.AGENT_REFUSED, f"agentpreview:{compile_id}:{exc.reason}",
                              AuditResult.BLOCKED)
            return ToolOutput(ok=False, error=exc.reason)
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
            definition, new = await service.update_from_preview(scope.session, definition, preview)
        except PreviewRefused as exc:
            return ToolOutput(ok=False, error=exc.reason)
        await self._audit(scope, invocation, AuditAction.AGENT_UPDATED,
                          f"agentdefinition:{definition.agent_id}:v{new.version}")
        return ToolOutput(ok=True, content=_dumps({
            "agent_id": str(definition.agent_id), "version": new.version, **_card_json(new, self._factory.registries),
        }))

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

    def __init__(self, *, factory: AgentFactory, core: SecurityCore) -> None:
        self._factory = factory
        self._core = core

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
        # D1 on the agent's graph, live; `consequential` → a token bound to
        # exactly this preview and its hash.
        outcome = await self._authorize(session, audit, AccessRequest(
            principal=principal, operation=Operation.CREATE, resource_type=ResourceType.AGENTDEFINITION,
            graph_id=spec.graph_id, confirmation_token=confirmation_token,
            arguments={"compile_id": str(body.compile_id), "spec_hash": spec.spec_hash},
        ))
        await self._confirm_or_refuse(session, outcome, action="create_agent",
                                      card=_card_json(spec, self._factory.registries))
        try:
            definition, spec = await service.create_from_preview(
                session, preview, created_by_device_id=principal.device_id, created_from_task_id=None)
        except PreviewRefused as exc:
            raise AppError(ErrorCode.CONFLICT, "this preview cannot be applied", details={"reason": exc.reason}) from None
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
        try:
            definition, new = await service.update_from_preview(session, loaded[0], preview)
        except PreviewRefused as exc:
            raise AppError(ErrorCode.CONFLICT, "this preview cannot be applied", details={"reason": exc.reason}) from None
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
        await self._factory.service.delete(session, loaded[0])
        await self._audit(audit, principal, AuditAction.AGENT_DELETED, f"agentdefinition:{agent_id}")


__all__ = [
    "CURRENT_AGENT_FACTORY_SCOPE",
    "AgentDefinitionLoader",
    "AgentFactory",
    "AgentFactoryFacade",
    "AgentToolAdapter",
    "agent_factory_scope",
    "agent_tool_definitions",
    "model_entry_facts",
    "registries_from_config",
]
