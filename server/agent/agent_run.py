"""What the runtime knows about an agent run (docs/29 §7.4, §10.3, §11).

`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]` (docs/29). An agent run is an
ordinary task of the present user. The runtime receives one extra, immutable
value — an `AgentRunBinding` — built by the composition root from the stored,
hash-verified spec. It carries the compiled **ceiling** (envelope, bounds,
per-run budget, hydration switches) and the ids that bind the run to exactly
one agent version. It carries no identity: the principal is the task's own
authenticated principal, as for any task. Since Phase 3 it also carries the
run's two Agent Gateway tokens (`RunTokens`) — run-scoped, short-lived and
revocable, never a session, a device credential or a provider key.

Everything agent-specific the runtime cannot decide itself goes through one
port (`AgentRunPort`), satisfied by the composition root: the Agent Gateway's
check of every request the run makes (token, nonce, freshness, replay),
re-validating the definition at every step, recording attribution, writing
the owner's inbox, the agent's own notebook, and routing a model-as-tool
request to a permitted model tool. `server.agent` never imports
`server.agents` (independent application-band siblings).
"""

from __future__ import annotations

import base64
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from server.agent.envelope import Envelope
from shared.schemas.agent import AgentFailureCode, AgentTaskStatus, TaskMode
from shared.schemas.agent_factory import AgentGatewayRequest, CompiledAgentSpec, RunTokenPurpose

# Runtime-owned pseudo-tools (docs/29 §16.3, §12). They are not capabilities
# and not registered tools: the runtime handles them itself, only for an agent
# run whose spec enables them, bound to the run's own agent.
NOTEBOOK_TOOL = "agent.notebook"
MODEL_ROUTE_TOOL = "agent.model"


@dataclass(frozen=True)
class RunTokens:
    """The run's two Agent Gateway tokens (docs/29 §11.3), handed over in its
    `AgentRunContext`. Each is short-lived, bound to this run, agent, spec
    hash and gateway, and revocable. They hold no identity and no secret, are
    never put in front of the model, and never logged."""

    model: str = field(repr=False)
    tool: str = field(repr=False)


@dataclass(frozen=True)
class AgentRunBinding:
    agent_id: uuid.UUID
    run_id: uuid.UUID
    version: int
    spec_hash: str
    run_mode: TaskMode
    envelope: Envelope
    input_text: str
    user_memory: bool
    vault: bool
    notebook: bool
    # The configured model entry the selected profile references
    # (`agent.primary`, `agent.fallback`, `models_as_tools.<id>`): the worker.
    model_ref: str
    max_model_calls: int
    max_tool_calls: int
    max_run_seconds: float
    # The tighter of the spec's per-run budget and what is left of its month.
    budget_per_run: float
    # docs/29 §11 (Phase 3): every request this run makes of the Agent
    # Gateway presents one of these. Set from the run's context at start.
    run_tokens: RunTokens | None = field(default=None, repr=False, compare=False)

    @property
    def model_tools(self) -> bool:
        return any(e.capability == "model.invoke" and "invoke" in e.operations for e in self.envelope.entries)

    @classmethod
    def from_spec(cls, spec: CompiledAgentSpec, *, run_id: uuid.UUID, model_ref: str,
                  budget_per_run: float, input_text: str = "") -> "AgentRunBinding":
        return cls(
            agent_id=spec.agent_id, run_id=run_id, version=spec.version, spec_hash=spec.spec_hash,
            run_mode=spec.run_mode, envelope=Envelope.from_spec(spec), input_text=input_text,
            user_memory=spec.hydration.user_memory, vault=spec.hydration.vault, notebook=spec.notebook_enabled,
            model_ref=model_ref, max_model_calls=spec.bounds.max_model_calls,
            max_tool_calls=spec.bounds.max_tool_calls, max_run_seconds=float(spec.bounds.max_run_seconds),
            budget_per_run=min(budget_per_run, spec.budget.per_run),
        )


@dataclass(frozen=True)
class RoutedModelCall:
    """A model-as-tool request resolved by JARVIS to one configured model
    tool: the call the runtime then authorizes like any tool call."""

    tool_id: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class GatewayAdmission:
    """The Agent Gateway's answer to one request of the run: go ahead
    (`ticket`, handed back to `settle` with the response), the stored response
    of an identical earlier request (`replayed` — nothing runs again), or a
    refusal (`refused`, an `AgentGatewayErrorCode` value)."""

    ticket: Any = field(default=None, repr=False)
    replayed: Mapping[str, Any] | None = None
    refused: str | None = None


def new_request_nonce() -> str:
    """128 random bits, base64url (docs/29 §11.3)."""

    return base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode("ascii")


def gateway_request(binding: AgentRunBinding, purpose: RunTokenPurpose, digest: str) -> AgentGatewayRequest:
    """One request of this run to the Agent Gateway: the run and agent it
    claims, that gateway's token, a fresh nonce, when it was sent, and the
    digest of exactly what it asks. No token, no request: an empty one is
    simply refused."""

    tokens = binding.run_tokens
    token = "" if tokens is None else (tokens.model if purpose is RunTokenPurpose.MODEL else tokens.tool)
    return AgentGatewayRequest(
        run_id=binding.run_id, agent_id=binding.agent_id, purpose=purpose, run_token=token,
        request_nonce=new_request_nonce(), sent_at=datetime.now(timezone.utc), request_digest=digest,
    )


class AgentRunPort(Protocol):
    async def started(self, binding: AgentRunBinding, *, task_id: uuid.UUID) -> None: ...

    async def check(self, binding: AgentRunBinding) -> AgentFailureCode | None:
        """`None` if the run may take its next step: its tool-gateway token is
        still valid for exactly this run, and the definition exists, is
        active, and its current version and hash are still exactly the
        binding's. Anything else is the reason the run must stop."""
        ...

    async def admit(self, binding: AgentRunBinding, request: AgentGatewayRequest) -> GatewayAdmission:
        """docs/29 §11, §13.3 steps 1–3: one request of the run, through the
        Agent Gateway — token, run, agent, freshness, nonce, replay."""
        ...

    async def settle(self, binding: AgentRunBinding, admission: GatewayAdmission,
                     response: Mapping[str, Any]) -> None:
        """Record what an admitted request was answered with (idempotent
        retries are answered from it)."""
        ...

    async def usage_recorded(self, binding: AgentRunBinding, *, usage_id: uuid.UUID, cost: float) -> None: ...

    async def finished(self, binding: AgentRunBinding, *, task_id: uuid.UUID, status: AgentTaskStatus,
                       response: str | None, failure: AgentFailureCode | None, cost: float) -> None: ...

    async def notebook_get(self, binding: AgentRunBinding, key: str) -> str | None: ...

    async def notebook_keys(self, binding: AgentRunBinding) -> list[str]: ...

    async def notebook_put(self, binding: AgentRunBinding, key: str, value: str) -> str | None:
        """Store the entry; `None` on success, otherwise the refusal reason."""
        ...

    async def route_model(self, binding: AgentRunBinding, arguments: Mapping[str, Any]) -> RoutedModelCall | str:
        """The permitted model tool for this request, or the refusal reason."""
        ...


__all__ = [
    "MODEL_ROUTE_TOOL",
    "NOTEBOOK_TOOL",
    "AgentRunBinding",
    "AgentRunPort",
    "GatewayAdmission",
    "RoutedModelCall",
    "RunTokens",
    "gateway_request",
    "new_request_nonce",
]
