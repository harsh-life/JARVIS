"""The agent runtime loop — 05_AGENT_RUNTIME.md §1-§11, implemented as
infrastructure around a probabilistic core it does not trust.

`[LOCKED]` (05 §0): "The agent proposes; deterministic infrastructure decides
and executes; the human confirms where the risk model requires it." Every
method here is deterministic Python control flow. The only place a model's
output enters is `ModelInvoker.invoke()`'s return value, and the only thing
ever done with that value is: parse it (`server.agent.proposal`, deterministic
or a parse failure), and if it parses, ask the injected `Authorizer` — never
this class itself — whether the proposed action may happen.

See `server/agent/ports.py`'s module docstring for why every dependency here
is a `Protocol`: this module may import nothing from `server.graph`,
`server.capabilities`, `server.secrets`, `server.storage`, or `server.gateway`,
and pyproject's import-linter contracts enforce that mechanically, not just by
convention.
"""

from __future__ import annotations

import dataclasses
import logging

from server.agent.context import append_observation, build_initial_messages
from server.agent.ports import (
    Authorizer,
    EventRecorder,
    MemoryHydrator,
    ModelInvoker,
    RuntimeAuthorizationResult,
    ToolCatalog,
    ToolDispatcher,
)
from server.agent.proposal import parse_proposal
from server.agent.tasks import LoopState, PendingAction, TaskConflict, TaskStore, UnknownTask
from shared.schemas.authorization import AccessRequest, Operation, Principal, ResourceType
from shared.schemas.runtime import (
    AgentEvent,
    AgentProposal,
    AgentResult,
    FailureReason,
    GenerationPolicy,
    ModelUnavailable,
    ProposalKind,
    ProposalParseError,
    RuntimeBounds,
    RuntimeEventKind,
    TaskStatus,
    ToolExecutionFailed,
    ToolInvocationRequest,
    ToolResult,
)

logger = logging.getLogger("hypermind.agent.orchestrator")

# 06 §3's model-tool family shares one registered capability (this branch's
# addition to server/capabilities/registry.py); *which* model-tool is
# selected is the grant's `resource_scope`, not a different capability per
# model-tool.
MODEL_TOOL_CAPABILITY = "model_tool.invoke"
MODEL_TOOL_OPERATION = "invoke"


class AgentOrchestrator:
    """One orchestrator is constructed per HTTP request (it closes over the
    caller's `Principal` and per-request adapters), but all instances for one
    process share the same `TaskStore` — that is what lets `/confirm` and
    `/cancel`, arriving on a *different* request, resume or interrupt a task
    `/agent/tasks` created on an earlier one.
    """

    def __init__(
        self,
        *,
        principal: Principal,
        bounds: RuntimeBounds,
        authorizer: Authorizer,
        model: ModelInvoker,
        tools: ToolDispatcher,
        memory: MemoryHydrator,
        events: EventRecorder,
        tool_catalog: ToolCatalog,
        task_store: TaskStore,
    ) -> None:
        self._principal = principal
        self._bounds = bounds
        self._authorizer = authorizer
        self._model = model
        self._tools = tools
        self._memory = memory
        self._events = events
        self._tool_catalog = tool_catalog
        self._task_store = task_store

    # ── public entry points (02 §5) ──────────────────────────────────────

    async def start(self, input_text: str) -> AgentResult:
        state = await self._task_store.create(principal=self._principal)
        state.status = TaskStatus.RUNNING

        await self._events.record(
            AgentEvent(kind=RuntimeEventKind.TASK_CREATED, task_id=state.task_id)
        )

        try:
            tool_contracts = await self._tool_catalog.list_enabled()
            memory_items = await self._memory.hydrate(input_text, limit=8)
        except Exception:  # noqa: BLE001 — 05 §6 FAIL-008/009: degrade, don't crash
            logger.exception("context assembly failed for task %s", state.task_id)
            return await self._fail(state, FailureReason.CONTEXT_ASSEMBLY_FAILED)

        state.messages = build_initial_messages(
            input_text=input_text,
            tool_contracts=tool_contracts,
            memory_items=memory_items,
        )
        await self._task_store.save(state)
        return await self._run_loop(state)

    async def resume(
        self, task_id: str, *, confirmation_token: str, approve: bool
    ) -> AgentResult:
        state = await self._get_owned(task_id)
        if state.status is not TaskStatus.AWAITING_CONFIRMATION or state.pending is None:
            raise TaskConflict(f"task {task_id} is not awaiting confirmation")

        pending = state.pending
        state.pending = None

        if not approve:
            await self._events.record(
                AgentEvent(kind=RuntimeEventKind.CONFIRMATION_REJECTED, task_id=state.task_id)
            )
            append_observation(
                state.messages,
                label=_proposal_label(pending.proposal),
                text="The human explicitly rejected this action. It was not performed.",
            )
            state.status = TaskStatus.RUNNING
            await self._task_store.save(state)
            return await self._run_loop(state)

        # `[LOCKED]` 05 §4: the *same* AccessRequest, now carrying the token —
        # the engine, not this class, decides whether it matches and is
        # unexpired (server.capabilities.confirmation._binding_matches).
        confirmed_request = dataclasses.replace(
            pending.access_request, confirmation_token=confirmation_token
        )
        result = await self._authorizer.authorize(confirmed_request)

        if not result.outcome.allowed:
            await self._events.record(
                AgentEvent(kind=RuntimeEventKind.CONFIRMATION_REJECTED, task_id=state.task_id)
            )
            append_observation(
                state.messages,
                label=_proposal_label(pending.proposal),
                text="The confirmation could not be validated (invalid, expired, or "
                "already used). This action was not performed.",
            )
            state.status = TaskStatus.RUNNING
            await self._task_store.save(state)
            return await self._run_loop(state)

        await self._events.record(
            AgentEvent(kind=RuntimeEventKind.CONFIRMATION_ACCEPTED, task_id=state.task_id)
        )
        await self._execute_and_observe(state, pending.proposal)
        state.status = TaskStatus.RUNNING
        await self._task_store.save(state)
        return await self._run_loop(state)

    async def cancel(self, task_id: str) -> None:
        state = await self._task_store.request_cancellation(
            task_id, requested_by=self._principal
        )
        await self._events.record(
            AgentEvent(kind=RuntimeEventKind.TASK_CANCELLED, task_id=state.task_id)
        )

    async def get_status(self, task_id: str) -> AgentResult:
        state = await self._get_owned(task_id)
        if state.result is not None:
            return state.result
        return _snapshot(state)

    # ── the loop itself (05 §1/§2) ───────────────────────────────────────

    async def _run_loop(self, state: LoopState) -> AgentResult:
        while True:
            if state.cancel_requested:
                return await self._finish(state, TaskStatus.CANCELLED, FailureReason.CANCELLED)

            if state.elapsed_seconds() >= self._bounds.wall_clock_timeout_seconds:
                return await self._fail(state, FailureReason.TIMEOUT)
            if self._bounds.max_cost > 0 and state.cost_accrued > self._bounds.max_cost:
                return await self._fail(state, FailureReason.BUDGET_EXCEEDED)
            if state.iterations >= self._bounds.max_iterations:
                return await self._fail(state, FailureReason.RUNAWAY_ITERATIONS)
            if state.model_calls >= self._bounds.max_model_calls:
                return await self._fail(state, FailureReason.RUNAWAY_MODEL_CALLS)

            state.iterations += 1

            try:
                model_result = await self._model.invoke(
                    state.messages, GenerationPolicy(), self._bounds.model_call_timeout_seconds
                )
            except ModelUnavailable:
                return await self._fail(state, FailureReason.MODEL_UNAVAILABLE)

            state.model_calls += 1
            await self._events.record(
                AgentEvent(
                    kind=RuntimeEventKind.MODEL_PROPOSAL,
                    task_id=state.task_id,
                    data={"tokens_used": model_result.tokens_used},
                )
            )

            try:
                proposal = parse_proposal(model_result.content)
            except ProposalParseError:
                state.parse_retries += 1
                if state.parse_retries > self._bounds.max_parse_retries:
                    return await self._fail(state, FailureReason.MALFORMED_PROPOSAL)
                append_observation(
                    state.messages,
                    label="runtime",
                    text=(
                        "Your last output could not be parsed. Respond with exactly "
                        "one JSON object matching the proposal schema — no other text."
                    ),
                )
                await self._task_store.save(state)
                continue

            if proposal.kind is ProposalKind.FINAL_ANSWER:
                return await self._finish_with_answer(state, proposal.final_text or "")

            outcome = await self._authorize_proposal(state, proposal)
            if outcome is None:
                # Nesting cap hit — a bounded denial, not a task failure; the
                # model may still finish or propose something else (05 §6).
                await self._task_store.save(state)
                continue

            access_request, result = outcome

            if result.outcome.needs_confirmation:
                return await self._pause_for_confirmation(state, proposal, access_request, result)

            if not result.outcome.allowed:
                # Covers both an ordinary deny AND an absolute-floor
                # `prohibited` outcome uniformly — RT-T4 holds by construction:
                # `needs_confirmation` is already False for a floor result, so
                # a prohibited proposal never reaches the branch above.
                append_observation(
                    state.messages,
                    label=_proposal_label(proposal),
                    text=f"This action was not permitted ({result.outcome.reason}).",
                )
                await self._task_store.save(state)
                continue

            if (
                state.tool_calls >= self._bounds.max_tool_calls
                and proposal.kind in (ProposalKind.TOOL_CALL, ProposalKind.MODEL_TOOL_CALL)
            ):
                return await self._fail(state, FailureReason.RUNAWAY_TOOL_CALLS)

            await self._execute_and_observe(state, proposal)
            await self._task_store.save(state)

    # ── authorization + dispatch helpers ─────────────────────────────────

    async def _authorize_proposal(
        self, state: LoopState, proposal: AgentProposal
    ) -> tuple[AccessRequest, RuntimeAuthorizationResult] | None:
        """Returns `(AccessRequest, RuntimeAuthorizationResult)`, or `None` if
        a model-tool proposal was denied purely on the nesting bound before
        authorization was even attempted (05 §3's nesting cap is the
        runtime's own ceiling, not a `04` decision)."""

        if (
            proposal.kind is ProposalKind.MODEL_TOOL_CALL
            and state.nesting_depth >= self._bounds.max_model_tool_nesting_depth
        ):
            append_observation(
                state.messages,
                label=_proposal_label(proposal),
                text="Model-tool nesting depth limit reached; this call was not made.",
            )
            return None

        access_request = self._build_access_request(state, proposal)
        await self._events.record(
            AgentEvent(
                kind=RuntimeEventKind.AUTHORIZATION_REQUESTED,
                task_id=state.task_id,
                data={"capability": access_request.required_capability or ""},
            )
        )
        result = await self._authorizer.authorize(access_request)
        await self._events.record(
            AgentEvent(
                kind=RuntimeEventKind.AUTHORIZATION_DECIDED,
                task_id=state.task_id,
                data={"decision": result.outcome.decision.value},
            )
        )
        return access_request, result

    def _build_access_request(self, state: LoopState, proposal: AgentProposal) -> AccessRequest:
        """05 §8 confused-deputy prevention, made structural: `principal`
        always comes from `self._principal` (the authenticated caller this
        orchestrator was constructed for) — never from `proposal`, which has
        no field capable of carrying one (see `AgentProposal`'s docstring).
        `graph_id` is the one field taken from the proposal, and 04 §0 is
        explicit that a request body's `graph_id` is "a claim to check, never
        a grant" — `04` re-derives membership itself regardless of what is
        claimed here.
        """

        if proposal.kind is ProposalKind.MODEL_TOOL_CALL:
            resource_scope = dict(proposal.resource_scope or {})
            resource_scope["model_tool_id"] = proposal.model_tool_id or ""
            arguments = dict(proposal.arguments or {})
            arguments["prompt"] = proposal.prompt or ""
            return AccessRequest(
                principal=self._principal,
                operation=Operation.CREATE,
                resource_type=ResourceType.TOOL_INVOCATION,
                resource_ref=proposal.model_tool_id,
                graph_id=proposal.graph_id,
                required_capability=MODEL_TOOL_CAPABILITY,
                capability_operation=MODEL_TOOL_OPERATION,
                task_id=state.task_id,
                arguments=arguments,
                resource_scope=resource_scope,
            )

        return AccessRequest(
            principal=self._principal,
            operation=Operation.CREATE,
            resource_type=ResourceType.TOOL_INVOCATION,
            resource_ref=proposal.tool_id,
            graph_id=proposal.graph_id,
            required_capability=proposal.capability,
            capability_operation=proposal.operation,
            task_id=state.task_id,
            arguments=proposal.arguments,
            resource_scope=proposal.resource_scope,
        )

    async def _pause_for_confirmation(
        self,
        state: LoopState,
        proposal: AgentProposal,
        access_request: AccessRequest,
        result: RuntimeAuthorizationResult,
    ) -> AgentResult:
        binding = result.outcome.confirmation_required_for
        assert binding is not None  # engine invariant: set whenever needs_confirmation
        state.pending = PendingAction(
            proposal=proposal, access_request=access_request, binding=binding
        )
        state.status = TaskStatus.AWAITING_CONFIRMATION
        state.result = AgentResult(
            task_id=state.task_id,
            status=TaskStatus.AWAITING_CONFIRMATION,
            iterations_used=state.iterations,
            tool_calls_used=state.tool_calls,
            model_calls_used=state.model_calls,
            confirmation_token=result.issued_confirmation_token,
        )
        await self._task_store.save(state)
        await self._events.record(
            AgentEvent(kind=RuntimeEventKind.CONFIRMATION_REQUESTED, task_id=state.task_id)
        )
        return state.result

    async def _execute_and_observe(self, state: LoopState, proposal: AgentProposal) -> None:
        tool_id = (
            proposal.model_tool_id
            if proposal.kind is ProposalKind.MODEL_TOOL_CALL
            else proposal.tool_id
        ) or ""
        operation = (
            MODEL_TOOL_OPERATION if proposal.kind is ProposalKind.MODEL_TOOL_CALL else proposal.operation
        ) or ""

        request = ToolInvocationRequest(
            task_id=state.task_id,
            tool_id=tool_id,
            operation=operation,
            arguments=(
                {**(proposal.arguments or {}), "prompt": proposal.prompt or ""}
                if proposal.kind is ProposalKind.MODEL_TOOL_CALL
                else dict(proposal.arguments or {})
            ),
            resource_scope=proposal.resource_scope,
        )

        if proposal.kind is ProposalKind.MODEL_TOOL_CALL:
            state.nesting_depth += 1

        await self._events.record(
            AgentEvent(
                kind=RuntimeEventKind.TOOL_INVOCATION, task_id=state.task_id, data={"tool_id": tool_id}
            )
        )
        try:
            result: ToolResult = await self._tools.dispatch(request)
        except ToolExecutionFailed as exc:
            result = ToolResult(tool_id=tool_id, success=False, error=str(exc))
        finally:
            if proposal.kind is ProposalKind.MODEL_TOOL_CALL:
                state.nesting_depth -= 1

        state.tool_calls += 1
        state.cost_accrued += result.estimated_cost
        await self._events.record(
            AgentEvent(
                kind=RuntimeEventKind.TOOL_RESULT,
                task_id=state.task_id,
                data={"tool_id": tool_id, "success": result.success},
            )
        )

        text = result.output if result.success else f"tool {tool_id!r} failed: {result.error}"
        append_observation(state.messages, label=tool_id, text=text or "")

    # ── terminal transitions ──────────────────────────────────────────────

    async def _finish_with_answer(self, state: LoopState, text: str) -> AgentResult:
        return await self._finish(state, TaskStatus.COMPLETED, output=text)

    async def _fail(self, state: LoopState, reason: FailureReason) -> AgentResult:
        return await self._finish(state, TaskStatus.FAILED, failure_reason=reason)

    async def _finish(
        self,
        state: LoopState,
        status: TaskStatus,
        failure_reason: FailureReason | None = None,
        output: str | None = None,
    ) -> AgentResult:
        state.status = status
        state.result = AgentResult(
            task_id=state.task_id,
            status=status,
            output=output,
            iterations_used=state.iterations,
            tool_calls_used=state.tool_calls,
            model_calls_used=state.model_calls,
            failure_reason=failure_reason,
        )
        await self._task_store.save(state)
        kind = {
            TaskStatus.COMPLETED: RuntimeEventKind.TASK_COMPLETED,
            TaskStatus.FAILED: RuntimeEventKind.TASK_FAILED,
            TaskStatus.CANCELLED: RuntimeEventKind.TASK_CANCELLED,
        }[status]
        await self._events.record(AgentEvent(kind=kind, task_id=state.task_id))
        return state.result

    async def _get_owned(self, task_id: str) -> LoopState:
        state = await self._task_store.get(task_id)
        if state.principal.user_id != self._principal.user_id:
            raise UnknownTask(task_id)
        return state


def _snapshot(state: LoopState) -> AgentResult:
    return AgentResult(
        task_id=state.task_id,
        status=state.status,
        iterations_used=state.iterations,
        tool_calls_used=state.tool_calls,
        model_calls_used=state.model_calls,
    )


def _proposal_label(proposal: AgentProposal) -> str:
    return proposal.tool_id or proposal.model_tool_id or "runtime"


__all__ = ["AgentOrchestrator"]
