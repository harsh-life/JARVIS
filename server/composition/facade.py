"""`AgentTaskPort` for the gateway: builds the request-scoped environment and
translates runtime outcomes into 02 §1.7's error vocabulary."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy.ext.asyncio import AsyncSession

from server.agent import (
    AgentRuntime,
    ConfirmationMismatch,
    StepUpNeeded,
    TaskNotAwaiting,
    TaskNotFound,
)
from server.agent.ports import Hydration, TaskEnvironment, UsageLimitReached
from server.auth.errors import StepUpRequired
from server.composition.models import ConfiguredModelResolver, ProviderFactory
from server.composition.secret_context import CURRENT_SECRET_RESOLVER, SecretUnavailable
from server.composition.security_port import RuntimeSecurityAdapter
from server.composition.usage_port import RuntimeUsageAdapter
from server.config.schema import AppConfig
from server.gateway.errors import AppError
from server.gateway.security import SecurityCore
from server.memory.hydration import AuthorizedContextHydrator
from server.secrets.audit_port import SecretAuditEvent
from server.secrets.requester import SecretRequester
from server.security.audit import AuditLogger
from server.storage.models import SecretReference
from shared.schemas.enums import AuditResult, SecretClass
from server.security.usage import UsagePolicy
from server.tools.registry import ToolRegistry
from shared.schemas.agent import AgentResult, ToolSummary
from shared.schemas.authorization import Principal
from shared.schemas.errors import ErrorCode


class _BoundHydrator:
    def __init__(self, hydrator: AuthorizedContextHydrator, session: AsyncSession) -> None:
        self._hydrator = hydrator
        self._session = session

    async def hydrate(self, *, principal: Principal, graph_id: uuid.UUID | None, query: str) -> Hydration:
        hydrated = await self._hydrator.hydrate(
            self._session, principal=principal, graph_id=graph_id, query=query
        )
        return Hydration(items=list(hydrated.items), notes=list(hydrated.notes))


class AgentTaskFacade:
    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        core: SecurityCore,
        config: AppConfig,
        usage_policy: UsagePolicy,
        tools: ToolRegistry,
        hydrator: AuthorizedContextHydrator,
        provider_factory: ProviderFactory,
    ) -> None:
        self._runtime = runtime
        self._core = core
        self._config = config
        self._usage = usage_policy
        self._tools = tools
        self._hydrator = hydrator
        self._provider_factory = provider_factory

    @property
    def runtime(self) -> AgentRuntime:
        return self._runtime

    def environment(self, session: AsyncSession, audit: AuditLogger) -> TaskEnvironment:
        return TaskEnvironment(
            session=session,
            security=RuntimeSecurityAdapter(core=self._core, session=session, audit=audit),
            usage=RuntimeUsageAdapter(policy=self._usage, session=session, request_id=audit.request_id),
            models=ConfiguredModelResolver(
                config=self._config,
                session=session,
                graph_repository=self._core.graph_repository,
                provider_factory=self._provider_factory,
            ),
            hydrator=_BoundHydrator(self._hydrator, session),
        )

    @contextmanager
    def _secret_scope(self, session: AsyncSession, audit: AuditLogger) -> Iterator[None]:
        """Install this request's SecretStore resolver for model adapters. A
        locked store raises inside the adapter, which fails the call closed."""

        store = self._core.secret_store

        async def resolve(handle: str, requester: SecretRequester) -> str:
            # This resolver exists only to hand a model adapter its API key
            # (06 §1). A handle of any other class — a device credential, an
            # OAuth token — is refused here even when the requester's scope
            # would otherwise match, so a model configuration can never be
            # pointed at a credential and ship it to a provider as a bearer
            # token (12 §2 "never one outside its declared need", SS-T10).
            reference = await session.get(SecretReference, handle)
            if reference is not None and reference.class_ is not SecretClass.MODEL_API_KEY:
                await audit.record_secret_event(
                    SecretAuditEvent(
                        action="secret.get",
                        secret_ref=handle,
                        requester=requester.describe(),
                        result=AuditResult.BLOCKED,
                        reason="not_a_model_api_key",
                    )
                )
                raise SecretUnavailable()
            return await store.get(session, handle, requester, audit)

        token = CURRENT_SECRET_RESOLVER.set(resolve)
        try:
            yield
        finally:
            CURRENT_SECRET_RESOLVER.reset(token)

    async def submit(
        self, session: AsyncSession, *, principal: Principal, user_input: str, audit: AuditLogger
    ) -> AgentResult:
        with self._secret_scope(session, audit):
            try:
                return await self._runtime.submit(
                    self.environment(session, audit), principal=principal, user_input=user_input
                )
            except UsageLimitReached as exc:
                raise AppError(
                    ErrorCode.RATE_LIMITED,
                    "a usage limit was reached",
                    details={"limit": exc.limit, "retry_after": exc.retry_after_seconds},
                ) from None
            except ValueError:
                raise AppError(ErrorCode.VALIDATION_FAILED, "input is empty or too long") from None

    async def confirm(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        task_id: uuid.UUID,
        confirmation_token: str,
        approve: bool,
        step_up_fresh: bool,
        audit: AuditLogger,
    ) -> AgentResult:
        with self._secret_scope(session, audit):
            try:
                return await self._runtime.confirm(
                    self.environment(session, audit),
                    caller=principal,
                    task_id=task_id,
                    confirmation_token=confirmation_token,
                    approve=approve,
                    step_up_fresh=step_up_fresh,
                )
            except TaskNotFound:
                raise AppError(ErrorCode.NOT_FOUND, "not found") from None
            except TaskNotAwaiting:
                raise AppError(ErrorCode.CONFLICT, "the task is not awaiting confirmation") from None
            except ConfirmationMismatch:
                raise AppError(ErrorCode.CONFLICT, "the confirmation token does not match") from None
            except StepUpNeeded:
                raise StepUpRequired("stale_authentication") from None
            except UsageLimitReached as exc:
                raise AppError(
                    ErrorCode.RATE_LIMITED,
                    "a usage limit was reached",
                    details={"limit": exc.limit, "retry_after": exc.retry_after_seconds},
                ) from None

    async def cancel(
        self, session: AsyncSession, *, principal: Principal, task_id: uuid.UUID, audit: AuditLogger
    ) -> AgentResult:
        try:
            return await self._runtime.cancel(
                self.environment(session, audit), caller=principal, task_id=task_id
            )
        except TaskNotFound:
            raise AppError(ErrorCode.NOT_FOUND, "not found") from None

    async def get(
        self, session: AsyncSession, *, principal: Principal, task_id: uuid.UUID, audit: AuditLogger
    ) -> AgentResult:
        try:
            return await self._runtime.get(
                self.environment(session, audit), caller=principal, task_id=task_id
            )
        except TaskNotFound:
            raise AppError(ErrorCode.NOT_FOUND, "not found") from None

    def tool_summaries(self) -> list[ToolSummary]:
        return self._tools.summaries()
