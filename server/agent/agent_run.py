"""What the runtime knows about an agent run (docs/29 §7.4, §10.3, Phase 2).

`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]` (docs/29). An agent run is an
ordinary task of the present user. The runtime receives one extra, immutable
value — an `AgentRunBinding` — built by the composition root from the stored,
hash-verified spec. It carries the compiled **ceiling** (envelope, bounds,
per-run budget, hydration switches) and the ids that bind the run to exactly
one agent version. It carries no identity and no credential: the principal is
the task's own authenticated principal, as for any task.

Everything agent-specific the runtime cannot decide itself goes through one
port (`AgentRunPort`), satisfied by the composition root: re-validating the
definition at every step, recording attribution, writing the owner's inbox,
the agent's own notebook, and routing a model-as-tool request to a permitted
model tool. `server.agent` never imports `server.agents` (independent
application-band siblings).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from server.agent.envelope import Envelope
from shared.schemas.agent import AgentFailureCode, AgentTaskStatus, TaskMode
from shared.schemas.agent_factory import CompiledAgentSpec

# Runtime-owned pseudo-tools (docs/29 §16.3, §12). They are not capabilities
# and not registered tools: the runtime handles them itself, only for an agent
# run whose spec enables them, bound to the run's own agent.
NOTEBOOK_TOOL = "agent.notebook"
MODEL_ROUTE_TOOL = "agent.model"


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


class AgentRunPort(Protocol):
    async def started(self, binding: AgentRunBinding, *, task_id: uuid.UUID) -> None: ...

    async def check(self, binding: AgentRunBinding) -> AgentFailureCode | None:
        """`None` if the run may take its next step: the definition exists, is
        active, and its current version and hash are still exactly the
        binding's. Anything else is the reason the run must stop."""
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


__all__ = ["MODEL_ROUTE_TOOL", "NOTEBOOK_TOOL", "AgentRunBinding", "AgentRunPort", "RoutedModelCall"]
