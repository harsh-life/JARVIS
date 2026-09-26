"""Memory and Knowledge Vault, joined to the deterministic security layer.

`server.memory` may not import the authorization engine (its audit path reaches
`server.secrets` — contract "Memory/vault never resolve secrets"), and the
gateway may not import `server.memory` (16 §2). This module is where the two
meet, and it adds no authorization of its own: every decision below is
`AuthorizationEngine.authorize` or the engine's one `readable()` predicate.

* `MemoryFactLoader` projects a stored fact to its authorization facts
  (owner, visibility, graph) so the engine can decide `mem0fact` operations —
  D1 membership, D3 owner-only, D4 visibility, the tier table's confirmations.
* `MemoryFacade` is `MemoryPort` (02 §7): authorize → gate → store → audit.
* `BoundFormation` is the runtime's `MemoryFormationPort` (docs/21 §3).
* `VaultFacade` is `VaultPort` (02 §8) plus bounded vault hydration.

Audit rows name fact ids and gate reasons, never fact content (MP-T5).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from server.config.schema import MemoryConfig, VaultConfig
from server.gateway.errors import AppError
from server.gateway.security import SecurityCore
from server.graph.authorization import AccessRequest, AuthorizationOutcome
from server.graph.ports import ResourceDescriptor
from server.graph.predicate import readable
from server.memory.extraction import extraction_messages, parse_extraction
from server.memory.gate import MemoryWriteGate
from server.memory.provider import MemoryProvider, MemoryProviderUnavailable
from server.models.provider import ChatMessage
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.vault.index import VaultIndex, VaultUnavailable
from shared.schemas.authorization import DenialSurface, Operation, Principal, ResourceType
from shared.schemas.enums import AuditActor, AuditResult, FactType, Visibility
from shared.schemas.errors import ErrorCode
from shared.schemas.memory import (
    Mem0Fact,
    MemoryCreateRequest,
    MemoryListResponse,
    MemoryPatchRequest,
    VaultQueryRequest,
    VaultQueryResponse,
)

logger = logging.getLogger("hypermind.composition.memory")

FAIL_009_NOTE = "answering without curated knowledge; the knowledge vault is unavailable (FAIL-009)"
_MAX_GRAPHS = 1000


def _mem0_down() -> AppError:
    return AppError(
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        "long-term memory temporarily unavailable",
        details={"dependency": "mem0"},
    )


def _refusal(outcome: AuthorizationOutcome) -> AppError:
    if outcome.surface is DenialSurface.PROHIBITED:
        return AppError(ErrorCode.PROHIBITED, "this action is never allowed")
    if outcome.surface is DenialSurface.FORBIDDEN:
        return AppError(ErrorCode.UNAUTHORIZED, "not permitted")
    # 04 §7: a fact the caller cannot see is indistinguishable from none.
    return AppError(ErrorCode.NOT_FOUND, "not found")


class MemoryFactLoader:
    """`ResourceLoader` for `mem0fact` (04 §1), registered on the Security Core's
    loader by the composition root. Projects to owner/visibility/graph only —
    the engine never sees fact content. A store failure propagates, and the
    engine's fail-closed wrapper turns it into a denial."""

    def __init__(self, provider: MemoryProvider) -> None:
        self._provider = provider

    async def load(
        self, session: AsyncSession, resource_type: ResourceType, resource_ref: str
    ) -> ResourceDescriptor | None:
        if resource_type is not ResourceType.MEM0FACT:
            return None
        try:
            fact_id = uuid.UUID(str(resource_ref))
        except (ValueError, TypeError):
            return None
        fact = await self._provider.get(fact_id)
        if fact is None:
            return None
        return ResourceDescriptor(
            resource_type=ResourceType.MEM0FACT,
            resource_ref=str(fact.fact_id),
            owner_user_id=fact.owner_user_id,
            visibility=fact.visibility,
            graph_id=fact.graph_id,
            source_user_id=fact.source_user_id,
        )


class MemoryFacade:
    def __init__(self, *, provider: MemoryProvider, core: SecurityCore, config: MemoryConfig) -> None:
        self._provider = provider
        self._core = core
        self._config = config
        self._gate = MemoryWriteGate(max_chars=config.max_fact_chars)

    @property
    def provider(self) -> MemoryProvider:
        return self._provider

    @property
    def config(self) -> MemoryConfig:
        return self._config

    # ── helpers ─────────────────────────────────────────────────────────

    async def active_graphs(self, session: AsyncSession, user_id: uuid.UUID) -> frozenset[uuid.UUID]:
        graphs = await self._core.graph_repository.graphs_for_user(session, user_id=user_id, limit=_MAX_GRAPHS)
        return frozenset(g.graph_id for g in graphs)

    def _visible(self, principal: Principal, fact: Mem0Fact, member_of: frozenset[uuid.UUID]) -> bool:
        return readable(
            user_id=principal.user_id,
            resource=ResourceDescriptor(
                resource_type=ResourceType.MEM0FACT, resource_ref=str(fact.fact_id),
                owner_user_id=fact.owner_user_id, visibility=fact.visibility, graph_id=fact.graph_id,
                source_user_id=fact.source_user_id,
            ),
            is_active_member_of_resource_graph=fact.graph_id in member_of,
        )

    async def _authorize(self, session: AsyncSession, request: AccessRequest, audit: AuditLogger) -> AuthorizationOutcome:
        return await self._core.engine.authorize(session, request, audit=audit)

    async def _require_available(self) -> None:
        if not await self._provider.health():
            raise _mem0_down()

    async def _audit(self, audit: AuditLogger, principal: Principal, action: AuditAction, resource: str,
                     result: AuditResult, graph_id: uuid.UUID | None = None,
                     actor: AuditActor = AuditActor.USER) -> None:
        await audit.record(
            actor=actor, action=action, resource=resource, result=result,
            user_id=principal.user_id, device_id=principal.device_id, session_id=principal.session_id,
            graph_id=graph_id,
        )

    async def _confirmation(self, session: AsyncSession, outcome: AuthorizationOutcome, action: str) -> AppError:
        binding = outcome.confirmation_required_for
        assert binding is not None
        issued = await self._core.confirmations.issue(session, binding=binding, risk_category=outcome.risk_category)
        return AppError(
            ErrorCode.CONFIRMATION_REQUIRED,
            "this memory change needs your confirmation before it runs",
            details={
                "confirmation_token": issued.token,
                "expires_at": issued.expires_at.isoformat(),
                "action": action,
                "risk_category": outcome.risk_category.value,
            },
        )

    async def store_candidate(
        self, session: AsyncSession, *, principal: Principal, graph_id: uuid.UUID, fact_type: object,
        content: object, audit: AuditLogger, actor: AuditActor,
    ) -> tuple[Mem0Fact | None, str | None]:
        """Authorize (D1) → gate → store → audit. Returns (fact, refusal reason)."""

        outcome = await self._authorize(session, AccessRequest(
            principal=principal, operation=Operation.CREATE, resource_type=ResourceType.MEM0FACT,
            graph_id=graph_id,
        ), audit)
        if not outcome.allowed:
            return None, "not_authorized"
        verdict = self._gate.check(fact_type=fact_type, content=content)
        if not verdict.accepted:
            assert verdict.reason is not None
            await self._audit(audit, principal, AuditAction.MEMORY_WRITE_BLOCKED,
                              f"mem0fact:blocked:{verdict.reason}", AuditResult.BLOCKED, graph_id, actor)
            return None, verdict.reason
        assert verdict.fact_type is not None and verdict.content is not None
        fact = Mem0Fact(
            owner_user_id=principal.user_id, source_user_id=principal.user_id, graph_id=graph_id,
            visibility=Visibility.PRIVATE, fact_type=verdict.fact_type, content=verdict.content,
            source_session_id=principal.session_id,
        )
        fact_id = await self._provider.add(fact)
        stored = await self._provider.get(fact_id)
        if stored is None:
            raise MemoryProviderUnavailable("add")
        await self._audit(audit, principal, AuditAction.MEMORY_WRITTEN, f"mem0fact:{fact_id}",
                          AuditResult.SUCCESS, graph_id, actor)
        return stored, None

    # ── MemoryPort (02 §7) ──────────────────────────────────────────────

    async def list(
        self, session: AsyncSession, *, principal: Principal, query: str | None, limit: int, audit: AuditLogger
    ) -> MemoryListResponse:
        member_of = await self.active_graphs(session, principal.user_id)
        try:
            if query and query.strip():
                candidates = await self._provider.search(
                    query=query, owner_user_id=principal.user_id, readable_graph_ids=member_of, limit=limit
                )
                facts = []
                for candidate in candidates:
                    fact = await self._provider.get(uuid.UUID(candidate.fact_id))
                    if fact is not None:
                        facts.append(fact)
            else:
                facts = list(await self._provider.list_facts(
                    owner_user_id=principal.user_id, readable_graph_ids=member_of, limit=limit
                ))
        except MemoryProviderUnavailable:
            raise _mem0_down() from None
        # RAUTH-004 re-check with live membership, whatever the store returned.
        visible = [f for f in facts if self._visible(principal, f, member_of)]
        if len(visible) != len(facts):
            logger.error("memory store returned %d fact(s) the principal may not read; dropped",
                         len(facts) - len(visible))
        return MemoryListResponse(items=visible[:limit])

    async def add(
        self, session: AsyncSession, *, principal: Principal, body: MemoryCreateRequest, audit: AuditLogger
    ) -> Mem0Fact:
        if not self._config.writes_enabled:
            raise AppError(ErrorCode.CONFLICT, "memory formation is turned off on this server")
        graph_id = body.graph_id or principal.active_graph_id
        if graph_id is None:
            raise AppError(ErrorCode.VALIDATION_FAILED, "a graph context is required to form a memory")
        await self._require_available()
        try:
            fact, reason = await self.store_candidate(
                session, principal=principal, graph_id=graph_id, fact_type=body.fact_type,
                content=body.content, audit=audit, actor=AuditActor.USER,
            )
        except MemoryProviderUnavailable:
            raise _mem0_down() from None
        if fact is None:
            if reason == "not_authorized":
                raise AppError(ErrorCode.NOT_FOUND, "not found")
            raise AppError(ErrorCode.VALIDATION_FAILED, "this cannot be stored as a memory",
                           details={"reason": reason})
        return fact

    async def recall(
        self, session: AsyncSession, *, principal: Principal, fact_id: uuid.UUID, audit: AuditLogger
    ) -> Mem0Fact:
        await self._require_available()
        outcome = await self._authorize(session, AccessRequest(
            principal=principal, operation=Operation.READ, resource_type=ResourceType.MEM0FACT,
            resource_ref=str(fact_id),
        ), audit)
        if not outcome.allowed:
            raise _refusal(outcome)
        return await self._get_or_down(fact_id)

    async def patch(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        fact_id: uuid.UUID,
        body: MemoryPatchRequest,
        confirmation_token: str | None,
        audit: AuditLogger,
    ) -> Mem0Fact:
        await self._require_available()
        ref = str(fact_id)
        content: str | None = None
        if body.content is not None:
            outcome = await self._authorize(session, AccessRequest(
                principal=principal, operation=Operation.WRITE, resource_type=ResourceType.MEM0FACT,
                resource_ref=ref,
            ), audit)
            if not outcome.allowed:
                raise _refusal(outcome)
            current = await self._get_or_down(fact_id)
            verdict = self._gate.check(fact_type=current.fact_type, content=body.content)
            if not verdict.accepted:
                await self._audit(audit, principal, AuditAction.MEMORY_WRITE_BLOCKED,
                                  f"mem0fact:blocked:{verdict.reason}", AuditResult.BLOCKED, current.graph_id)
                raise AppError(ErrorCode.VALIDATION_FAILED, "this cannot be stored as a memory",
                               details={"reason": verdict.reason})
            content = verdict.content

        if body.visibility is not None:
            # RAUTH V2: owner-only, explicit, audited — and `consequential` in the
            # tier table, so the engine asks for a token bound to this exact change.
            arguments = {"visibility": body.visibility.value}
            if content is not None:
                arguments["content_sha256"] = hashlib.sha256(content.encode()).hexdigest()
            outcome = await self._authorize(session, AccessRequest(
                principal=principal, operation=Operation.SHARE, resource_type=ResourceType.MEM0FACT,
                resource_ref=ref, arguments=arguments, confirmation_token=confirmation_token,
            ), audit)
            if outcome.needs_confirmation:
                raise await self._confirmation(session, outcome, "memory.change_visibility")
            if not outcome.allowed:
                raise _refusal(outcome)
            current = await self._get_or_down(fact_id)
            if body.visibility is Visibility.GRAPH:
                role = await self._core.graph_repository.active_role(
                    session, graph_id=current.graph_id, user_id=principal.user_id
                )
                if role is None:
                    # Sharing into a graph the owner has left would publish to an
                    # audience they are no longer part of.
                    raise AppError(ErrorCode.CONFLICT, "you are not a member of this memory's graph")

        try:
            if content is not None:
                await self._provider.update_content(fact_id, content)
                await self._audit(audit, principal, AuditAction.MEMORY_CORRECTED, f"mem0fact:{fact_id}",
                                  AuditResult.SUCCESS)
            if body.visibility is not None:
                await self._provider.set_visibility(fact_id, body.visibility)
                final = await self._get_or_down(fact_id)
                await self._audit(
                    audit, principal,
                    AuditAction.RESOURCE_SHARED if body.visibility is Visibility.GRAPH
                    else AuditAction.RESOURCE_UNSHARED,
                    f"mem0fact:{fact_id}", AuditResult.SUCCESS, final.graph_id,
                )
            return await self._get_or_down(fact_id)
        except MemoryProviderUnavailable:
            raise _mem0_down() from None

    async def delete(
        self,
        session: AsyncSession,
        *,
        principal: Principal,
        fact_id: uuid.UUID,
        confirmation_token: str | None,
        audit: AuditLogger,
    ) -> None:
        await self._require_available()
        outcome = await self._authorize(session, AccessRequest(
            principal=principal, operation=Operation.DELETE, resource_type=ResourceType.MEM0FACT,
            resource_ref=str(fact_id), confirmation_token=confirmation_token,
        ), audit)
        if outcome.needs_confirmation:
            raise await self._confirmation(session, outcome, "memory.delete")
        if not outcome.allowed:
            raise _refusal(outcome)
        try:
            await self._provider.delete(fact_id)
        except MemoryProviderUnavailable:
            raise _mem0_down() from None
        await self._audit(audit, principal, AuditAction.MEMORY_DELETED, f"mem0fact:{fact_id}", AuditResult.SUCCESS)

    async def status(self) -> dict:
        return {
            "enabled": True,
            "available": await self._provider.health(),
            "provider": "mem0",
            "writes_enabled": self._config.writes_enabled,
            "auto_extract": self._config.auto_extract,
        }

    async def _get_or_down(self, fact_id: uuid.UUID) -> Mem0Fact:
        try:
            fact = await self._provider.get(fact_id)
        except MemoryProviderUnavailable:
            raise _mem0_down() from None
        if fact is None:
            raise AppError(ErrorCode.NOT_FOUND, "not found")
        return fact

    # ── lifecycle (LIFE-003, 11 §5) ─────────────────────────────────────

    async def delete_all_for_user(self, *, user_id: uuid.UUID, audit: AuditLogger) -> int:
        """Account deletion: every fact the user owns, private or shared, and
        every mirror of it. Called by the (future) account-deletion flow."""

        removed = await self._provider.delete_all_for_user(user_id)
        await audit.record(actor=AuditActor.SYSTEM, action=AuditAction.MEMORY_USER_PURGED,
                           resource=f"mem0fact:owner:{user_id}", result=AuditResult.SUCCESS, user_id=user_id)
        return removed

    async def delete_graph_shared(self, *, graph_id: uuid.UUID, audit: AuditLogger) -> int:
        """Graph deletion: graph-shared facts only; members' private facts stay
        with their owners (MEM-T9). Called by the (future) graph-deletion flow."""

        removed = await self._provider.delete_graph_shared(graph_id)
        await audit.record(actor=AuditActor.SYSTEM, action=AuditAction.MEMORY_GRAPH_PURGED,
                           resource=f"mem0fact:graph:{graph_id}", result=AuditResult.SUCCESS)
        return removed

    def bound_formation(self, session: AsyncSession, audit: AuditLogger) -> "BoundFormation":
        return BoundFormation(self, session, audit)


class BoundFormation:
    """The runtime's `MemoryFormationPort`, bound to one request's transaction."""

    def __init__(self, facade: MemoryFacade, session: AsyncSession, audit: AuditLogger) -> None:
        self._facade = facade
        self._session = session
        self._audit = audit

    def plan(
        self, *, principal: Principal, graph_id: uuid.UUID | None, user_request: str, final_answer: str
    ) -> Sequence[ChatMessage] | None:
        config = self._facade.config
        if not (config.writes_enabled and config.auto_extract) or graph_id is None:
            return None
        return extraction_messages(user_request=user_request, final_answer=final_answer)

    async def commit(
        self, *, principal: Principal, graph_id: uuid.UUID | None, task_id: uuid.UUID, model_output: str
    ) -> list[str]:
        if graph_id is None:
            return []
        candidates = parse_extraction(model_output, max_facts=self._facade.config.max_facts_per_task)
        stored = refused = 0
        for candidate in candidates:
            try:
                fact, _reason = await self._facade.store_candidate(
                    self._session, principal=principal, graph_id=graph_id,
                    fact_type=candidate.fact_type, content=candidate.content,
                    audit=self._audit, actor=AuditActor.AGENT,
                )
            except MemoryProviderUnavailable:
                return ["long-term memory was not updated for this task"]
            if fact is None:
                refused += 1
            else:
                stored += 1
        notes = []
        if stored:
            notes.append(f"remembered {stored} fact(s) from this task (private to you)")
        if refused:
            notes.append(f"{refused} proposed fact(s) were not stored under the memory policy")
        return notes


class VaultFacade:
    """`VaultPort` (02 §8) and bounded vault hydration. Reaches the vault index
    only — never the memory provider (VAULT-003)."""

    def __init__(self, *, index: VaultIndex, config: VaultConfig) -> None:
        self._index = index
        self._config = config

    @property
    def index(self) -> VaultIndex:
        return self._index

    async def query(
        self, session: AsyncSession, *, principal: Principal, request: VaultQueryRequest
    ) -> VaultQueryResponse:
        try:
            results = await self._index.query(
                question=request.question, domain=request.domain, top_k=request.top_k
            )
        except VaultUnavailable:
            raise AppError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "answering without curated knowledge",
                details={"dependency": "vault"},
            ) from None
        return VaultQueryResponse(results=results)

    async def hydrate(self, question: str) -> tuple[list[str], list[str]]:
        """Relevant chunks within `hydration_top_k` / `hydration_max_chars`."""

        if self._config.hydration_top_k == 0:
            return [], []
        try:
            results = await self._index.query(question=question, domain=None, top_k=self._config.hydration_top_k)
        except VaultUnavailable:
            return [], [FAIL_009_NOTE]
        budget = self._config.hydration_max_chars
        chunks: list[str] = []
        for item in results:
            text = f"[{item.source_file}] {item.chunk.strip()}"
            if len(text) > budget:
                continue
            chunks.append(text)
            budget -= len(text)
        return chunks, []

    async def status(self) -> dict:
        status = await self._index.status()
        return {"enabled": True, "available": status.available, "indexed_commit": status.indexed_commit}


__all__ = ["BoundFormation", "MemoryFacade", "MemoryFactLoader", "VaultFacade"]
