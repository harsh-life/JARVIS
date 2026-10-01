"""The Agent Gateway's request check (docs/29 §11, §13.3 steps 1–3) — the one
choke point every request of an agent run passes, whichever runtime sent it.

`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]` (docs/29). Phase 3 uses it
in-process, for the native runtime; Phase 6 will put the same object behind
an authenticated network surface for external runtimes. One code path, so
native and external runs are checked identically.

    request ──▶ token: well-formed, known, this gateway's, bound to exactly
                this run + agent + the run's spec hash
            ──▶ the run: still open
            ──▶ the agent: exists, active, the run's owner's, the owner still
                in its graph, and still on exactly the run's version and hash
                (the Phase 2 re-validation, `service.check_run`, read fresh)
            ──▶ token: not expired, not revoked
            ──▶ request: fresh `sent_at`, well-formed nonce
            ──▶ ledger: a new nonce is recorded; the same nonce for the same
                request returns the stored response (never executed twice);
                for anything else it is a replay

Every read is fresh from the store; nothing is cached, so a revocation, a
pause, a change or a delete made by any request applies to the next one.

**The gateway authorizes nothing.** A request it admits has only shown that
it belongs to a live run. What the request asks is then decided where it
always is: the envelope gate, activation, the mode and risk ceilings and the
one engine (04) as the run's owner, confirmation where required — and, for a
model call, the run's selected profile and budgets (slice 3B). A run token
holds no identity and no secret.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from server.agents.gateway import tokens as run_tokens
from server.agents.service import AgentDefinitionService
from server.storage.models import AgentRunRow
from shared.schemas.agent import AgentFailureCode
from shared.schemas.agent_factory import AgentGatewayErrorCode, AgentGatewayRequest, RunTokenPurpose

IsMember = Callable[[uuid.UUID, uuid.UUID], Awaitable[bool]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class IssuedRunTokens:
    """A run's two tokens, in the clear — exactly once, for its context."""

    model: str = field(repr=False)
    tool: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class GatewayContext:
    """The live run a request belongs to. Facts, not authority."""

    run_id: uuid.UUID
    agent_id: uuid.UUID
    version: int
    spec_hash: str
    owner_user_id: uuid.UUID
    purpose: RunTokenPurpose
    token_id: uuid.UUID


@dataclass(frozen=True)
class GatewayDenied:
    code: AgentGatewayErrorCode
    # An identifier for the audit trail: `unknown`, `purpose`, `binding`,
    # `expired`, `revoked`, `nonce_reused`, `in_flight`, ... never a value.
    detail: str


@dataclass(frozen=True)
class GatewayAdmitted:
    context: GatewayContext
    nonce: str


@dataclass(frozen=True)
class GatewayReplayed:
    """The stored response of an identical earlier request: nothing runs."""

    context: GatewayContext
    response: Mapping[str, Any]


Authenticated = GatewayContext | GatewayDenied
Admission = GatewayAdmitted | GatewayReplayed | GatewayDenied


class AgentGateway:
    def __init__(self, service: AgentDefinitionService, *, clock: Callable[[], datetime] | None = None) -> None:
        self._service = service
        self._clock = clock or service.now

    def now(self) -> datetime:
        return _utc(self._clock())

    # ── tokens ──────────────────────────────────────────────────────────

    async def issue(self, session: AsyncSession, run: AgentRunRow, *, deadline: datetime) -> IssuedRunTokens:
        """Two tokens for one run, stored as hashes. Expired tokens of past
        runs are pruned here, a day after they expired."""

        await self._service.prune_gateway(session)
        expires_at = run_tokens.token_expiry(_utc(deadline))
        model, tool = run_tokens.new_token(), run_tokens.new_token()
        for token, purpose in ((model, RunTokenPurpose.MODEL), (tool, RunTokenPurpose.TOOL)):
            await self._service.store_run_token(session, run=run, token_hash=run_tokens.token_digest(token),
                                                purpose=purpose.value, expires_at=expires_at)
        return IssuedRunTokens(model=model, tool=tool, expires_at=expires_at)

    async def revoke_run(self, session: AsyncSession, run_id: uuid.UUID, reason: str) -> int:
        return await self._service.revoke_run_tokens(session, run_id, reason)

    async def revoke_agent(self, session: AsyncSession, agent_id: uuid.UUID, reason: str) -> int:
        return await self._service.revoke_agent_tokens(session, agent_id, reason)

    async def prune(self, session: AsyncSession) -> int:
        return await self._service.prune_gateway(session)

    # ── the request check ───────────────────────────────────────────────

    async def authenticate(self, session: AsyncSession, *, run_id: uuid.UUID, agent_id: uuid.UUID,
                           purpose: RunTokenPurpose, token: str, is_member: IsMember) -> Authenticated:
        """Steps 1 and 3 of §13.3: the token, the run, the agent — no nonce.
        What a runtime's per-step liveness check needs."""

        invalid = AgentGatewayErrorCode.INVALID_RUN_TOKEN
        if not run_tokens.well_formed_token(token):
            return GatewayDenied(invalid, "malformed")
        row = await self._service.run_token(session, run_tokens.token_digest(token))
        facts = self._service.token_facts(row) if row is not None else None
        run = await session.get(AgentRunRow, row.run_id, populate_existing=True) if row is not None else None
        refused = run_tokens.binding_refusal(
            facts, presented=token, purpose=purpose, run_id=run_id, agent_id=agent_id,
            run_spec_hash=run.spec_hash if run is not None else None)
        if refused is not None:
            return GatewayDenied(invalid, refused)
        assert row is not None and run is not None and facts is not None
        if run.finished_at is not None:
            return GatewayDenied(AgentGatewayErrorCode.RUN_NOT_RUNNING, "finished")
        # The definition, read fresh — Phase 2's one re-validation.
        code = await self._service.check_run(session, run_id=run.run_id, agent_id=run.agent_id,
                                             version=run.version, spec_hash=run.spec_hash, is_member=is_member)
        if code is AgentFailureCode.SPEC_CHANGED:
            return GatewayDenied(AgentGatewayErrorCode.SPEC_CHANGED, "newer_version")
        if code is not None:
            return GatewayDenied(AgentGatewayErrorCode.AGENT_UNAVAILABLE, code.value)
        live = run_tokens.liveness_refusal(facts, now=self.now())
        if live is not None:
            return GatewayDenied(invalid, live)
        return GatewayContext(run_id=run.run_id, agent_id=run.agent_id, version=run.version,
                              spec_hash=run.spec_hash, owner_user_id=run.owner_user_id, purpose=purpose,
                              token_id=row.token_id)

    async def admit(self, session: AsyncSession, request: AgentGatewayRequest, *, is_member: IsMember) -> Admission:
        """§13.3 steps 1–3 for one request: the token, the run, the agent,
        then freshness, the nonce, and the replay/idempotency ledger."""

        context = await self.authenticate(session, run_id=request.run_id, agent_id=request.agent_id,
                                          purpose=request.purpose, token=request.run_token, is_member=is_member)
        if isinstance(context, GatewayDenied):
            return context
        if not run_tokens.is_fresh(_utc(request.sent_at), self.now()):
            return GatewayDenied(AgentGatewayErrorCode.STALE_REQUEST, "sent_at")
        if not run_tokens.well_formed_nonce(request.request_nonce):
            return GatewayDenied(AgentGatewayErrorCode.SCHEMA_INVALID, "nonce")
        seen = await self._service.nonce_entry(session, context.token_id, request.request_nonce)
        if seen is not None:
            if seen.request_digest != request.request_digest:
                return GatewayDenied(AgentGatewayErrorCode.REPLAY, "nonce_reused")
            if seen.status != "done" or seen.response is None:
                return GatewayDenied(AgentGatewayErrorCode.REPLAY, "in_flight")
            return GatewayReplayed(context=context, response=dict(seen.response))
        await self._service.record_nonce(session, token_id=context.token_id, nonce=request.request_nonce,
                                         digest=request.request_digest)
        return GatewayAdmitted(context=context, nonce=request.request_nonce)

    async def settle(self, session: AsyncSession, admitted: GatewayAdmitted, response: Mapping[str, Any]) -> None:
        """Store what the admitted request was answered with: a retry with the
        same nonce gets exactly this, and nothing runs again."""

        await self._service.settle_nonce(session, token_id=admitted.context.token_id, nonce=admitted.nonce,
                                         response=response)


__all__ = [
    "Admission",
    "AgentGateway",
    "GatewayAdmitted",
    "GatewayContext",
    "GatewayDenied",
    "GatewayReplayed",
    "IssuedRunTokens",
]
