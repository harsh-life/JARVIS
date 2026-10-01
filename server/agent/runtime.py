"""The agent runtime — 05_AGENT_RUNTIME.md.

    AGENT PROPOSES
        ↓
    DETERMINISTIC INFRASTRUCTURE AUTHORIZES          (AuthorizationEngine, via SecurityPort)
        ↓
    CONFIRMATION WHEN REQUIRED                       (ConfirmationService, action-bound token)
        ↓
    TOOL / ADAPTER EXECUTES                          (ToolCatalog, bounded)
        ↓
    RESULT / EVIDENCE RETURNS                        (an untrusted observation)
        ↓
    AGENT MAY CONTINUE WITHIN AUTHORIZED BOUNDARY    (every bound re-checked)

This module is deterministic plumbing around a probabilistic core (05 §0). The
model's output is parsed into a typed proposal and nothing else; there is no
path by which model text becomes an authorization decision, a risk tier, a
confirmation, or a grant. What the runtime owns (05 §11) — hydration, parsing,
authorization requests, dispatch, bounds, confirmation gating, fallback, usage
and audit emission — is all here or in the ports it calls; none of it is
delegated to the model.

The runtime constructs every authorization request **as the principal**
(AGENT-004): the task's own user/device/session, taken from the authenticated
request that submitted it. The agent can therefore never do, on the principal's
behalf, anything the principal could not do directly (05 §8).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Mapping

from server.agent import context as ctx
from server.agent.bounds import ConcurrencyGate, RuntimeBounds
from server.agent.breaker import BreakerLimits, BreakerScope, CircuitBreaker, Trip, TripSource
from server.agent.events import AgentEvent
from server.agent.recovery import RecoveryPolicy, SwitchReason, operation_key
from server.agent import envelope as agent_envelope
from server.agent import modes
from server.agent.agent_run import (
    MODEL_ROUTE_TOOL,
    NOTEBOOK_TOOL,
    AgentRunBinding,
    GatewayAdmission,
    ModelCallFacts,
    gateway_request,
)
from server.agent.ports import (
    ActionRequest,
    ActivationRequest,
    CapabilityStatus,
    ResolvedModels,
    TaskEnvironment,
    TaskObserver,
    TaskSnapshot,
    ToolCatalog,
    TraceEvent,
    UsageLimitReached,
)
from server.agent.proposals import (
    FinalAnswer,
    ProposalError,
    RequestCapabilities,
    ToolCall,
    parse_proposal,
)
from server.agent.records import (
    close_if_live,
    create_task_row,
    load_task_row,
    non_terminal_task_ids,
    update_task_row,
)
from server.agent.state import Activation, PendingStep, PlatformWaitStep, TaskState, TaskStateRegistry
from server.models.provider import ModelProvider, ModelUnavailable
from shared.schemas.agent import (
    AgentFailure,
    AgentFailureCode,
    AgentResult,
    AgentTaskStatus,
    ExecutionPlatform,
    PendingAction,
    PlatformWait,
    TaskCounters,
    TaskMode,
    TERMINAL_STATUSES,
    ToolHandle,
    ToolInvocation,
    ToolOutput,
)
from shared.schemas.agent_factory import RunTokenPurpose
from shared.schemas.authorization import Operation, Principal, ResourceType
from shared.schemas.enums import AuditResult, PermissionDecisionValue, RiskCategory, UsageKind

logger = logging.getLogger("hypermind.agent.runtime")

_MEMORY_SKIPPED_NOTE = "long-term memory was not updated for this task"

_FAILURE_MESSAGES: dict[AgentFailureCode, str] = {
    AgentFailureCode.MAX_ITERATIONS: "The task stopped: it reached its maximum number of steps.",
    AgentFailureCode.MAX_MODEL_CALLS: "The task stopped: it reached its maximum number of model calls.",
    AgentFailureCode.MAX_TOOL_CALLS: "The task stopped: it reached its maximum number of tool calls.",
    AgentFailureCode.TIMEOUT: "The task stopped: it ran out of time.",
    AgentFailureCode.BUDGET_EXCEEDED: "The task stopped: the next call would exceed the budget.",
    AgentFailureCode.RATE_LIMITED: "The task stopped: a usage rate limit was reached.",
    AgentFailureCode.MODEL_UNAVAILABLE: "Couldn't get a model response.",
    AgentFailureCode.UNPARSEABLE_PROPOSAL: "The task stopped: the model did not produce a valid step.",
    AgentFailureCode.CONFIRMATION_EXPIRED: "The confirmation expired; the action was not performed.",
    AgentFailureCode.CONFIRMATION_STATE_LOST: "The paused action is no longer available; it was not performed.",
    AgentFailureCode.PRINCIPAL_REVOKED: "The task stopped: the session or device that started it is no longer valid.",
    AgentFailureCode.INTERNAL_ERROR: "The task stopped because of an internal error.",
    AgentFailureCode.AGENT_UNAVAILABLE: "The agent run stopped: the agent was deleted, paused or is no longer "
                                        "approved to run.",
    AgentFailureCode.SPEC_CHANGED: "The agent run stopped: the agent was changed while it was running.",
    AgentFailureCode.AGENT_BUDGET_EXHAUSTED: "The agent run did not start: this agent's monthly budget is spent.",
    AgentFailureCode.STALLED: (
        "The task stopped: it was making no progress (or repeating the same step), and no other "
        "worker was available. Nothing further was performed."
    ),
    AgentFailureCode.WORKER_CHAIN_EXHAUSTED: (
        "The task stopped: every available worker failed or could not resolve it. Nothing further "
        "was performed."
    ),
    AgentFailureCode.PLATFORM_UNAVAILABLE: (
        "The task stopped: it needed something on your phone (such as Shizuku) that did not "
        "become available in time. Nothing further was performed."
    ),
    AgentFailureCode.EMERGENCY_STOP: (
        "The task was stopped by a safety control; nothing further was performed and it will "
        "not be resumed. Start a new task if you still want this done."
    ),
}

# 18 §5.3 step 7: the user is told, in plain words, which control stopped the task.
# Identifiers only in the audit trail; this prose goes to the task's owner.
_TRIP_NOTES: dict[str, str] = {
    TripSource.DENIAL_LIMIT.value: (
        "Stopped by the safety breaker: too many actions in this task were refused by authorization."
    ),
    TripSource.VIOLATION_LIMIT.value: (
        "Stopped by the safety breaker: too many actions in this task were blocked at a sandbox, "
        "network, or executable boundary."
    ),
    TripSource.REJECTION_LIMIT.value: (
        "Stopped by the safety breaker: you declined too many proposed actions in this task."
    ),
    TripSource.OPERATOR.value: "Stopped by the server operator.",
    TripSource.EVALUATOR.value: (
        "Stopped by the safety breaker: an automated evaluator flagged this task's behaviour as "
        "anomalous, and the breaker stopped it."
    ),
    TripSource.GLOBAL_LATCH.value: "Stopped: the server operator has suspended all tasks.",
}
_GENERIC_TRIP_NOTE = "Stopped by the safety breaker."

# docs/29 §11–§13: how an agent run meets an Agent Gateway refusal. A request
# that was malformed, stale or a replay is refused and not performed — for a
# tool call the model is told so; anything about the run itself (its token,
# the run, the agent, its version, its budget) stops the run.
_GATEWAY_REQUEST_REFUSALS = frozenset({"replay", "stale_request", "schema_invalid"})
_GATEWAY_STOPS: dict[str, AgentFailureCode] = {
    "spec_changed": AgentFailureCode.SPEC_CHANGED,
    "budget_exceeded": AgentFailureCode.BUDGET_EXCEEDED,
    "agent_budget_exhausted": AgentFailureCode.AGENT_BUDGET_EXHAUSTED,
}


# docs/29 §11.2: the only model name an agent run asks the Model Gateway for.
AGENT_MODEL_ALIAS = "agent-model"


def _gateway_stop(code: str) -> AgentFailureCode:
    return _GATEWAY_STOPS.get(code, AgentFailureCode.AGENT_UNAVAILABLE)


def _model_digest(provider: ModelProvider, messages) -> str:
    """The identity of one model request: which model, and exactly what it is
    asked (docs/29 §11.3 idempotency)."""

    canonical = json.dumps(
        {"provider": provider.spec.provider, "model": provider.spec.model,
         "messages": [[m.role, m.content] for m in messages]},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TaskNotFound(Exception):
    """Absent, or not the caller's — indistinguishable on purpose (04 §7)."""


class TaskNotAwaiting(Exception):
    """The task is not paused for a confirmation."""


class ConfirmationMismatch(Exception):
    """The presented token is not the one for this task's pending action. The
    pending action is left exactly as it was."""


class StepUpNeeded(Exception):
    """A `high_irreversible` action needs a freshly re-attested session before it
    can be approved (OD-F1 tier 4, SESSION-003). The action stays pending."""


class SubmissionsSuspended(Exception):
    """The global emergency latch is set, or cannot be read (18 §5.4, fail
    closed). No task is created."""


class StopOutcome(str, Enum):
    """What an operator stop did to one task (18 §5.4)."""

    STOPPED = "stopped"                    # enforced by this call
    SIGNALLED = "signalled"                # running in another request; enforced at its next checkpoint
    ALREADY_TERMINAL = "already_terminal"  # nothing to stop — idempotent
    NOT_FOUND = "not_found"


class _Interrupted(Exception):
    """The task's cancel event fired during a model call (a stop or a cancel);
    the loop's checkpoint decides which."""


class _Stop(Exception):
    def __init__(self, code: AgentFailureCode) -> None:
        super().__init__(code.value)
        self.code = code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _release_store(env: TaskEnvironment) -> None:
    """H-1: end the store transaction before a long wait (a model call, a tool
    run, a memory search), so no transaction or lock is held across it. Every
    decision that wait depends on — the task row, the authorization, the
    audit — is already written, and is now durable."""

    if env.release_store is not None:
        await env.release_store()


def _merge_scope(*scopes: Any) -> dict[str, str] | None:
    merged: dict[str, str] = {}
    for scope in scopes:
        if scope:
            merged.update({str(k): str(v) for k, v in dict(scope).items()})
    return merged or None


def _worker_id(provider: ModelProvider) -> str:
    """A worker's configured identity, as an audit-safe identifier."""

    raw = f"{provider.spec.provider}:{provider.spec.model}"
    return "".join(c if c.isalnum() or c in "._:-" else "_" for c in raw)[:24]


def _worker_resource(task_id: uuid.UUID, workers: str, reason: SwitchReason) -> str:
    """`agent.worker.*` / `agent.recovery.*` audit resource: identifiers only."""

    return f"worker:{task_id}:{workers}:{reason.value}"[:128]


def _trip_resource(scope: BreakerScope, task_id: uuid.UUID, source: str, reason: str) -> str:
    """The `breaker.tripped` audit resource: identifiers only (≤ 128 chars)."""

    resource = f"breaker:{scope.value}:{task_id}:{source}"
    return resource if reason == source else f"{resource}:{reason}"


def _trip_failure(trip: Trip) -> AgentFailure:
    return AgentFailure(
        code=AgentFailureCode.EMERGENCY_STOP,
        message=f"{_TRIP_NOTES.get(trip.source, _GENERIC_TRIP_NOTE)} "
                f"{_FAILURE_MESSAGES[AgentFailureCode.EMERGENCY_STOP]}",
    )


class AgentRuntime:
    def __init__(
        self,
        *,
        bounds: RuntimeBounds,
        concurrency: ConcurrencyGate,
        tools: ToolCatalog,
        states: TaskStateRegistry | None = None,
        breaker_limits: BreakerLimits | None = None,
        recovery: RecoveryPolicy | None = None,
        observer: TaskObserver | None = None,
    ) -> None:
        self._bounds = bounds
        self._concurrency = concurrency
        self._tools = tools
        self._states = states or TaskStateRegistry()
        # 18 §5: the deterministic circuit breaker. Owned here because a stop is
        # task lifecycle; other components will reach it only through `trip()`.
        self._breaker = CircuitBreaker(breaker_limits or BreakerLimits(), self._states)
        # 18 §4: `None` keeps today's behaviour (per-step primary → fallback,
        # no switching, no stall detection).
        self._recovery = recovery
        # 19: the Judge, if configured — notified, never waited on, never read
        # back. `None` (evaluation disabled) changes nothing here.
        self._observer = observer

    @property
    def bounds(self) -> RuntimeBounds:
        return self._bounds

    @property
    def states(self) -> TaskStateRegistry:
        return self._states

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    def attach_observer(self, observer: TaskObserver) -> None:
        """Wire the Judge's observer (19) after construction — its breaker port
        needs this runtime. Once only; with none attached (evaluation off)
        nothing is recorded or copied for observation at all."""

        if self._observer is not None:
            raise RuntimeError("a task observer is already attached")
        self._observer = observer

    # ── public API (02 §5) ──────────────────────────────────────────────

    async def submit(
        self, env: TaskEnvironment, *, principal: Principal, user_input: str,
        mode: TaskMode = TaskMode.EXECUTE, agent: AgentRunBinding | None = None,
    ) -> AgentResult:
        """Run a new task until it finishes, pauses for a human, or fails.

        `principal` is the authenticated caller (03 §8). Its `active_graph_id` is
        the task's graph context and is re-checked by the engine (D1) on every
        action, never trusted from the session row (GRAPH-006).
        """

        user_input = user_input.strip()
        if not user_input or len(user_input) > self._bounds.max_input_chars:
            raise ValueError("input is empty or too long")
        mode = TaskMode(mode)  # 18 §3: one of the four, chosen by the caller
        if agent is not None:
            # docs/29 §7.4: an agent run is an ordinary task of the present
            # user, in its spec's mode. Without the factory's port there is no
            # way to re-validate it, so it does not start at all.
            if env.agent_runs is None:
                raise RuntimeError("agent runs are not available")
            mode = agent.run_mode
        # 18 §5.4: while the global latch is set — or cannot be read — no task
        # is created at all.
        if not await env.supervisor.submissions_open():
            raise SubmissionsSuspended()

        self._states.prune_expired()
        async with self._concurrency.slot(principal):
            task_id = uuid.uuid4()
            graph_id = principal.active_graph_id
            await create_task_row(
                env.session,
                task_id=task_id,
                user_id=principal.user_id,
                device_id=principal.device_id,
                session_id=principal.session_id,
                graph_id=graph_id,
                mode=mode.value,
            )
            state = TaskState(task_id=task_id, principal=principal, graph_id=graph_id, mode=mode,
                              user_input=user_input, agent=agent)
            self._states.put(state)
            # Re-checked synchronously, with no `await` since `put`: a global
            # stop sets the in-process latch and then sweeps the registry
            # without yielding, so a task created while that happened is either
            # swept or caught here — never neither.
            if env.supervisor.latched_now():
                self._breaker.trip(BreakerScope.TASK, task_id, reason=TripSource.GLOBAL_LATCH.value,
                                   source=TripSource.GLOBAL_LATCH.value)
            try:
                await self._event(env, state, AgentEvent.TASK_SUBMITTED, AuditResult.SUCCESS)
                if agent is not None:
                    assert env.agent_runs is not None
                    await env.agent_runs.started(agent, task_id=task_id)
                if state.tripped is not None:
                    return await self._emergency_stop(env, state)

                items, notes, knowledge = [], [], []
                # docs/29 §16.4: an agent run hydrates only what its spec
                # enables (user memory and/or the vault), through the same
                # authorized hydrator as any task.
                if agent is None or agent.user_memory or agent.vault:
                    await _release_store(env)  # H-1: hydration searches the memory store
                    hydration = await env.hydrator.hydrate(
                        principal=principal, graph_id=graph_id, query=user_input
                    )
                    notes = list(hydration.notes)
                    items = list(hydration.items) if agent is None or agent.user_memory else []
                    knowledge = list(hydration.knowledge) if agent is None or agent.vault else []
                state.notes.extend(notes)
                state.messages = [
                    ctx.ChatMessage("system", ""),  # regenerated every step
                    ctx.context_message(items, notes, user_input, knowledge),
                ]
                return await self._drive(env, state)
            except Exception as exc:  # noqa: BLE001 — see _internal_failure
                return await self._internal_failure(env, state, exc)

    async def confirm(
        self,
        env: TaskEnvironment,
        *,
        caller: Principal,
        task_id: uuid.UUID,
        confirmation_token: str,
        approve: bool,
        step_up_fresh: bool,
    ) -> AgentResult:
        """Resolve the one pending action (02 §5 `/confirm`).

        Approval is the *user's*: `caller` must be the task's owner (any of their
        devices — same-user continuity). The action then runs as the task's own
        principal, re-authorized by the engine with the token, which the engine
        spends. Expiry never approves (PERM-004).
        """

        row = await load_task_row(env.session, task_id)
        if row is None or row.user_id != caller.user_id:
            raise TaskNotFound()
        if row.status != AgentTaskStatus.AWAITING_CONFIRMATION.value:
            raise TaskNotAwaiting()

        state = self._states.get(task_id)
        if state is None:
            return await self._fail_row(env, row, AgentFailureCode.CONFIRMATION_STATE_LOST, caller)
        if state.pending is None:
            # Live, and another request already claimed the pending action (a
            # double tap, a retried request): it is running, not awaiting. The
            # row still reads "awaiting" only because that request has not
            # committed yet — it must never be closed from here.
            raise TaskNotAwaiting()
        if state.tripped is not None:
            # 18 §5.2/§6: a stopped task is terminal. No approval, whatever the
            # token, runs or resumes anything once the breaker has tripped.
            return await self._emergency_stop(env, state)

        pending = state.pending
        if not hmac.compare_digest(pending.token.encode(), (confirmation_token or "").encode()):
            raise ConfirmationMismatch()

        if pending.expired():
            state.pending = None
            await self._event(env, state, AgentEvent.CONFIRMATION_REJECTED, AuditResult.BLOCKED,
                              resource=self._pending_resource(pending))
            return await self._fail(env, state, AgentFailureCode.CONFIRMATION_EXPIRED)

        if not approve:
            state.pending = None
            await self._event(env, state, AgentEvent.CONFIRMATION_REJECTED, AuditResult.BLOCKED,
                              resource=self._pending_resource(pending))
            self._breaker.record_rejection(state)
            if state.tripped is not None:
                return await self._emergency_stop(env, state)
            state.messages.append(
                ctx.observation(
                    f"The user declined the proposed {pending.kind.replace('_', ' ')} "
                    f"({self._pending_resource(pending)}). It was not performed.",
                    limit=self._bounds.max_observation_chars,
                )
            )
            await self._set_status(env, state, AgentTaskStatus.RUNNING)
            return await self._resume(env, state)

        if pending.risk_category is RiskCategory.HIGH_IRREVERSIBLE and not step_up_fresh:
            raise StepUpNeeded()

        async with self._concurrency.slot(state.principal):
            # Claim the pending action before the first `await`: until it is
            # cleared, a concurrent `/cancel` treats the task as paused and may
            # finish it — and this approval must then not go on to execute.
            state.pending = None
            try:
                await self._set_status(env, state, AgentTaskStatus.RUNNING)
            except Exception:
                # The store could not take the write (e.g. busy): nothing ran,
                # so the action goes back to pending, exactly as it was, for a
                # retry — rather than being lost with the failed request.
                state.pending = pending
                raise
            try:
                if state.tripped is not None:
                    return await self._emergency_stop(env, state)
                if state.cancelled:
                    return await self._finish(env, state, AgentTaskStatus.CANCELLED)
                if not await env.security.principal_active(state.principal):
                    raise _Stop(AgentFailureCode.PRINCIPAL_REVOKED)
                # docs/29: an approval resumes an agent run only if its
                # definition is still exactly the one the run started with.
                await self._check_agent(env, state)
                if pending.kind == "capability_activation":
                    await self._approve_activation(env, state, pending)
                else:
                    await self._approve_tool_operation(env, state, pending)
            except _Stop as stop:
                return await self._fail(env, state, stop.code)
            except ConfirmationMismatch:
                state.pending = pending
                await self._set_status(env, state, AgentTaskStatus.AWAITING_CONFIRMATION)
                raise
            except Exception as exc:  # noqa: BLE001 — see _internal_failure
                return await self._internal_failure(env, state, exc)
            if state.platform_wait is not None:
                return await self._pause_for_platform(env, state)
            return await self._drive(env, state)

    async def resume_after_platform(
        self, env: TaskEnvironment, *, device_id: uuid.UUID, dependency: str
    ) -> list[AgentResult]:
        """docs/23 §5.3: the device reports `dependency` available again. Every
        task waiting for exactly that dependency on exactly that device
        proposes its call again — through the same checks and the same engine
        as any new proposal, so a grant revoked, a membership left or a mode
        ceiling in the meantime is honoured, and a consequential action asks
        for a *new* confirmation. The operation then sent is a fresh one."""

        results: list[AgentResult] = []
        for state in self._states.live():
            wait = state.platform_wait
            if wait is None or wait.device_id != device_id or wait.dependency != dependency:
                continue
            results.append(await self._resume_platform_wait(env, state))
        return results

    async def _resume_platform_wait(self, env: TaskEnvironment, state: TaskState) -> AgentResult:
        wait = state.platform_wait
        assert wait is not None
        if state.tripped is not None:
            return await self._emergency_stop(env, state)
        if wait.expired():
            state.platform_wait = None
            return await self._fail(env, state, AgentFailureCode.PLATFORM_UNAVAILABLE)
        async with self._concurrency.slot(state.principal):
            # Claimed before the first `await`: a concurrent cancel then sees a
            # running task, and a second availability report finds nothing.
            state.platform_wait = None
            try:
                await self._set_status(env, state, AgentTaskStatus.RUNNING)
            except Exception:
                state.platform_wait = wait
                raise
            try:
                if state.tripped is not None:
                    return await self._emergency_stop(env, state)
                if state.cancelled:
                    return await self._finish(env, state, AgentTaskStatus.CANCELLED)
                if not await env.security.principal_active(state.principal):
                    raise _Stop(AgentFailureCode.PRINCIPAL_REVOKED)
                await self._check_agent(env, state)
                await self._event(env, state, AgentEvent.TASK_RESUMED, AuditResult.SUCCESS,
                                  resource=f"platform_wait:{wait.dependency}")
                state.messages.append(ctx.observation(
                    f"The device reports '{wait.dependency}' is available again. The earlier "
                    f"{wait.tool}.{wait.operation} call is being authorized again and retried as a new "
                    "operation.",
                    limit=self._bounds.max_observation_chars,
                ))
                remaining_seconds = self._wall_clock(state) - state.run_seconds_used
                call = ToolCall(type="tool_call", tool=wait.tool, operation=wait.operation,
                                arguments=wait.arguments, resource_ref=wait.resource_ref,
                                platform=wait.platform, scope=wait.scope)
                paused = await self._tool_call(env, state, call, lambda: remaining_seconds)
                if paused is not None:
                    return paused
                if state.platform_wait is not None:
                    return await self._pause_for_platform(env, state)
            except _Stop as stop:
                return await self._fail(env, state, stop.code)
            except Exception as exc:  # noqa: BLE001 — see _internal_failure
                return await self._internal_failure(env, state, exc)
            return await self._drive(env, state)

    async def _end_platform_wait(self, env: TaskEnvironment, row, state: TaskState | None) -> AgentResult:
        """A wait that ran out (or whose live state is gone): the task fails
        `platform_unavailable`; the call is never retried."""

        if state is not None:
            state.platform_wait = None
            return await self._fail(env, state, AgentFailureCode.PLATFORM_UNAVAILABLE)
        updated, _ = await self._close_from_row(env, row, AgentTaskStatus.FAILED,
                                                AgentFailureCode.PLATFORM_UNAVAILABLE,
                                                event=AgentEvent.TASK_ABANDONED)
        return self._result_from_row(updated)

    async def cancel(
        self, env: TaskEnvironment, *, caller: Principal, task_id: uuid.UUID
    ) -> AgentResult:
        row = await load_task_row(env.session, task_id)
        if row is None or row.user_id != caller.user_id:
            raise TaskNotFound()
        if AgentTaskStatus(row.status) in TERMINAL_STATUSES:
            return self._result_from_row(row)

        state = self._states.get(task_id)
        if state is not None and state.pending is None and state.platform_wait is None:
            # Live in another request. Decided from the in-process state, not
            # the row: the request driving the task holds its status change in
            # an uncommitted transaction, so the row can still read "awaiting"
            # while the approved action is running. Flag it and signal any
            # in-flight tool call, which is aborted rather than left to its own
            # timeout (05 §9).
            state.cancelled = True
            state.cancel_event.set()
            return self._result_from_state(state, AgentTaskStatus.RUNNING)

        if state is None:
            updated, _ = await self._close_from_row(env, row, AgentTaskStatus.CANCELLED, None,
                                                    event=AgentEvent.TASK_CANCELLED)
            return self._result_from_row(updated)

        state.pending = None  # the paused action is dropped, never performed
        state.platform_wait = None  # and a waited-for call is never retried
        state.cancelled = True
        state.cancel_event.set()
        return await self._finish(env, state, AgentTaskStatus.CANCELLED)

    def signal_stop(self, task_id: uuid.UUID, *, reason: str, source: str) -> bool:
        """Trip a live task, synchronously and without touching the database
        (18 §5.3 steps 1–2 begin here). Returns whether a live task existed.

        The superuser control path calls this for every target **before** any
        database work: the request driving a running task holds the store's
        write lock, so a stop that first waited on the database would wait on
        the very task it is trying to stop.
        """

        if self._states.get(task_id) is None:
            return False
        self._breaker.trip(BreakerScope.TASK, task_id, reason=reason, source=source)
        return True

    async def enforce_trip(self, env: TaskEnvironment, task_id: uuid.UUID) -> StopOutcome:
        """Enforce a trip that is **already recorded** on a task nobody is
        driving (paused for a confirmation or for its phone). It trips nothing
        itself: a task that was not tripped is left exactly as it is.

        A running task needs none of this — its own request enforces the stop
        at the next checkpoint (18 §5.3)."""

        state = self._states.get(task_id)
        if state is None or state.tripped is None:
            return StopOutcome.NOT_FOUND
        if (state.pending is not None or state.platform_wait is not None) and not state.stop_enforced:
            await self._emergency_stop(env, state)
            return StopOutcome.STOPPED
        return StopOutcome.SIGNALLED

    async def operator_stop(
        self, env: TaskEnvironment, task_id: uuid.UUID, *, reason: str, source: str
    ) -> StopOutcome:
        """Stop one task on the operator's authority (18 §5.4) — never on a
        user's, a model's, or a tool's. Reached only from the superuser control
        path (`server/composition/supervisor.py`); authorization happened
        there. Idempotent.

        * running in another request → tripped; that request enforces the stop
          at its next checkpoint (an in-flight model or tool call is aborted);
        * paused for a confirmation → nobody is driving it, so it is enforced
          here, and its pending action is never performed;
        * no live state but a non-terminal row (e.g. after a restart) → closed
          here from the row;
        * already terminal → nothing to do.
        """

        state = self._states.get(task_id)
        if state is not None:
            self._breaker.trip(BreakerScope.TASK, task_id, reason=reason, source=source)
            if (state.pending is not None or state.platform_wait is not None) and not state.stop_enforced:
                await self._emergency_stop(env, state)
                return StopOutcome.STOPPED
            return StopOutcome.SIGNALLED

        row = await load_task_row(env.session, task_id)
        if row is None:
            return StopOutcome.NOT_FOUND
        if AgentTaskStatus(row.status) in TERMINAL_STATUSES:
            return StopOutcome.ALREADY_TERMINAL
        _, closed = await self._close_from_row(env, row, AgentTaskStatus.FAILED,
                                               AgentFailureCode.EMERGENCY_STOP, event=AgentEvent.TASK_FAILED)
        if not closed:
            return StopOutcome.ALREADY_TERMINAL
        principal = Principal(user_id=row.user_id, device_id=row.device_id,
                              session_id=row.session_id, active_graph_id=row.graph_id)
        await env.security.record(
            AgentEvent.BREAKER_TRIPPED, principal=principal, graph_id=row.graph_id,
            resource=_trip_resource(BreakerScope.TASK, task_id, source, reason),
            result=AuditResult.BLOCKED,
        )
        return StopOutcome.STOPPED

    async def get(
        self, env: TaskEnvironment, *, caller: Principal, task_id: uuid.UUID
    ) -> AgentResult:
        row = await load_task_row(env.session, task_id)
        if row is None or row.user_id != caller.user_id:
            raise TaskNotFound()
        state = self._states.get(task_id)
        if state is not None and row.status == AgentTaskStatus.AWAITING_CONFIRMATION.value:
            return self._result_from_state(state, AgentTaskStatus.AWAITING_CONFIRMATION)
        if row.status == AgentTaskStatus.WAITING_FOR_PLATFORM.value:
            if state is not None and state.platform_wait is not None and not state.platform_wait.expired():
                return self._result_from_state(state, AgentTaskStatus.WAITING_FOR_PLATFORM)
            return await self._end_platform_wait(env, row, state)
        result = self._result_from_row(row)
        if row.status == AgentTaskStatus.AWAITING_CONFIRMATION.value and state is None:
            result.notes.append("The paused action is no longer available and cannot be confirmed.")
        return result

    # ── the loop (05 §1) ────────────────────────────────────────────────

    async def _resume(self, env: TaskEnvironment, state: TaskState) -> AgentResult:
        async with self._concurrency.slot(state.principal):
            return await self._drive(env, state)

    async def _drive(self, env: TaskEnvironment, state: TaskState) -> AgentResult:
        segment_start = time.monotonic()

        def remaining() -> float:
            used = state.run_seconds_used + (time.monotonic() - segment_start)
            return self._wall_clock(state) - used

        try:
            try:
                models = await self._resolve_models(env, state)
            except ModelUnavailable:
                raise _Stop(AgentFailureCode.MODEL_UNAVAILABLE) from None
            state.allowed_tool_ids = models.allowed_tool_ids
            state.tuning = await self._load_tuning(env)

            while True:
                # 18 §5.3: the breaker's checkpoint comes first — a tripped task
                # is an emergency stop even if it was also cancelled.
                if state.tripped is not None:
                    return await self._emergency_stop(env, state)
                if state.cancelled:
                    return await self._finish(env, state, AgentTaskStatus.CANCELLED)
                if remaining() <= 0:
                    raise _Stop(AgentFailureCode.TIMEOUT)
                if state.iterations >= self._bounds.max_iterations:
                    raise _Stop(AgentFailureCode.MAX_ITERATIONS)
                state.iterations += 1

                # Revocation is immediate (SESSION-002): re-checked every step,
                # not only at submission.
                if not await env.security.principal_active(state.principal):
                    raise _Stop(AgentFailureCode.PRINCIPAL_REVOKED)
                # docs/29 §10.3 / §14.3: so is an agent run's definition —
                # deleted, paused, revoked or changed stops the run here.
                await self._check_agent(env, state)

                try:
                    text = await self._model_step(env, state, models, remaining)
                except _Interrupted:
                    continue  # the checkpoint above decides: stop or cancel
                # A stop or cancel that landed while the model was answering is
                # enforced before its proposal is acted on — even a final answer:
                # a stopped task never ends as "completed".
                if state.tripped is not None:
                    return await self._emergency_stop(env, state)
                if state.cancelled:
                    return await self._finish(env, state, AgentTaskStatus.CANCELLED)

                try:
                    proposal = parse_proposal(text)
                except ProposalError as exc:
                    state.consecutive_parse_failures += 1
                    if state.malformed_from is None:
                        state.malformed_from = len(state.messages) - 1  # the malformed output itself
                    await self._event(env, state, AgentEvent.PROPOSAL_REJECTED, AuditResult.BLOCKED,
                                      resource="proposal:unparseable")
                    if state.consecutive_parse_failures > self._bounds.max_parse_retries:
                        if self._recovery is None:
                            raise _Stop(AgentFailureCode.UNPARSEABLE_PROPOSAL) from None
                        await self._switch(env, state, models, SwitchReason.MALFORMED,
                                           stuck=AgentFailureCode.UNPARSEABLE_PROPOSAL)
                        continue
                    state.messages.append(ctx.observation(
                        f"Your last output was not a valid proposal ({exc}). Reply with exactly "
                        "one JSON object in one of the documented forms.",
                        limit=self._bounds.max_observation_chars,
                    ))
                    continue
                state.consecutive_parse_failures = 0
                state.malformed_from = None

                if isinstance(proposal, FinalAnswer):
                    if proposal.unresolved and self._recovery is not None \
                            and self._recovery.escalate_on_unresolved:
                        # 18 §4.1: escalate to the next eligible worker. The
                        # unresolved answer is dropped from the transcript; it
                        # is never reported as the task's result.
                        del state.messages[-1]
                        await self._switch(env, state, models, SwitchReason.UNRESOLVED,
                                           stuck=AgentFailureCode.WORKER_CHAIN_EXHAUSTED, allow_paid=False)
                        continue
                    return await self._finish(env, state, AgentTaskStatus.COMPLETED,
                                              response=proposal.content, unresolved=proposal.unresolved)
                if isinstance(proposal, RequestCapabilities):
                    paused = await self._request_capabilities(env, state, proposal)
                else:
                    paused = await self._tool_call(env, state, proposal, remaining, models)
                if paused is not None:
                    return paused
                if state.platform_wait is not None:
                    return await self._pause_for_platform(env, state)
                if self._recovery is not None and state.tripped is None:
                    await self._check_progress(env, state, models)
        except _Stop as stop:
            return await self._fail(env, state, stop.code)
        except Exception as exc:  # noqa: BLE001 — see _internal_failure
            return await self._internal_failure(env, state, exc)
        finally:
            state.run_seconds_used += time.monotonic() - segment_start

    async def _model_step(
        self, env: TaskEnvironment, state: TaskState, models: ResolvedModels, remaining
    ) -> str:
        """One model call, with deterministic fallback (05 §5). Every attempt is
        bounded, budget-checked, and metered."""

        messages = list(state.messages)
        tuning = state.tuning
        messages[0] = ctx.ChatMessage(
            "system", ctx.system_prompt(
                self._visible_tools(state), state.active_capability_names(), state.mode,
                guidance=tuning.system_prompt if tuning else None,
                descriptions=tuning.tool_descriptions if tuning else None,
                runtime_tools=self._runtime_tools(state),
            )
        )
        messages = ctx.compact(messages, max_chars=self._bounds.max_context_chars)
        prompt_chars = sum(len(m.content) for m in messages)
        if state.cancel_event.is_set():
            raise _Interrupted()

        if self._recovery is None:
            # Today's behaviour, unchanged (05 §5): this step tries the primary,
            # then `agent.fallback`, and the next step starts at the primary.
            for index, provider in enumerate(models.chain):
                text = await self._attempt(env, state, index, provider, messages, prompt_chars, remaining)
                if text is not None:
                    return text
            # FAIL-CORE-002: never a fabricated answer.
            raise _Stop(AgentFailureCode.MODEL_UNAVAILABLE)

        # 18 §4.2: the active worker answers; an unavailable one is replaced
        # for the rest of the task, within `max_worker_switches`.
        while True:
            state.worker_index = min(state.worker_index, len(models.chain) - 1)
            provider = models.chain[state.worker_index]
            text = await self._attempt(env, state, state.worker_index, provider, messages, prompt_chars, remaining)
            if text is not None:
                return text
            await self._switch(env, state, models, SwitchReason.UNAVAILABLE,
                               stuck=AgentFailureCode.MODEL_UNAVAILABLE)

    async def _attempt(self, env: TaskEnvironment, state: TaskState, index: int, provider: ModelProvider,
                       messages, prompt_chars: int, remaining) -> str | None:
        """One worker call — bounded, budget-checked, metered, and raced
        against the task's cancel event. `None` means the worker was
        unavailable (recorded, never retried silently)."""

        if state.cancel_event.is_set():
            # A stop or cancel landed (e.g. while the previous worker failed and
            # the next was being chosen): no new call is started at all.
            raise _Interrupted()
        if state.model_calls >= self._max_model_calls(state):
            raise _Stop(AgentFailureCode.MAX_MODEL_CALLS)
        if remaining() <= 0:
            raise _Stop(AgentFailureCode.TIMEOUT)

        spec = provider.spec
        projected = spec.projected_cost(prompt_chars=prompt_chars)
        admission = None
        if state.agent is not None:
            # docs/29 §12: an agent run's model call is a request to the Model
            # Gateway — its model token, a fresh nonce — before anything else.
            admission = await self._admit(env, state, RunTokenPurpose.MODEL, _model_digest(provider, messages),
                                          model=ModelCallFacts(alias=AGENT_MODEL_ALIAS, provider=spec.provider,
                                                               model=spec.model))
            if admission.refused is not None:
                raise _Stop(_gateway_stop(admission.refused))
            if admission.replayed is not None:
                # The same request was answered before: its answer, not a
                # second call (and not a second charge).
                content = str(admission.replayed.get("content", ""))
                state.messages.append(ctx.ChatMessage("assistant", content[: self._bounds.max_observation_chars]))
                return content
        await self._precheck(env, state, projected)

        state.model_calls += 1
        await _release_store(env)
        try:
            result = await self._invoke_cancellable(state, provider, messages, remaining)
        except _Interrupted:
            await self._meter_model(env, state, provider, units=0, cost=0.0)
            raise
        except (ModelUnavailable, asyncio.TimeoutError):
            await self._meter_model(env, state, provider, units=0, cost=0.0)
            if remaining() <= 0:
                # The task's own wall clock cut the call off: report the
                # bound that was hit, not a provider outage (05 §3).
                raise _Stop(AgentFailureCode.TIMEOUT) from None
            await self._event(env, state, AgentEvent.WORKER_FAILED, AuditResult.FAILURE,
                              resource=_worker_resource(state.task_id, f"{index}:{_worker_id(provider)}",
                                                        SwitchReason.UNAVAILABLE))
            return None

        cost = spec.pricing.cost(
            prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens
        )
        await self._meter_model(env, state, provider, units=result.total_tokens, cost=cost)
        state.messages.append(ctx.ChatMessage("assistant", result.content[: self._bounds.max_observation_chars]))
        if admission is not None:
            await self._settle(env, state, admission,
                               {"content": result.content[: self._bounds.max_observation_chars]})
        return result.content

    # ── supervisory recovery (18 §4) ────────────────────────────────────

    async def _switch(self, env: TaskEnvironment, state: TaskState, models: ResolvedModels,
                      reason: SwitchReason, *, stuck: AgentFailureCode, allow_paid: bool = True) -> None:
        """Replace the active worker with the next eligible one, or fail the
        task honestly. Only `worker_index` changes: principal, graph, mode,
        activations, pending confirmation, counters and bounds all carry over,
        and the new worker's proposals meet exactly the same checks.

        `stuck` is the failure when the chain never had an alternative; once it
        had one, exhausting it is `worker_chain_exhausted` (18 §4.4) — except an
        outage, which stays `model_unavailable` (a dependency is down).
        """

        policy = self._recovery_for(state)
        chain = models.chain
        current = state.worker_index
        nxt = next(
            (i for i in range(current + 1, len(chain)) if allow_paid or not chain[i].spec.pricing.is_paid),
            None,
        )
        over_limit = policy is not None and state.worker_switches >= policy.max_worker_switches
        if policy is None or nxt is None or over_limit:
            code = stuck
            if policy is not None and (over_limit and nxt is not None
                                       or len(chain) > 1 and reason is not SwitchReason.UNAVAILABLE):
                code = AgentFailureCode.WORKER_CHAIN_EXHAUSTED
            await self._event(env, state, AgentEvent.RECOVERY_EXHAUSTED, AuditResult.FAILURE,
                              resource=_worker_resource(state.task_id, str(current), reason))
            raise _Stop(code)

        await self._event(env, state, AgentEvent.WORKER_SWITCHED, AuditResult.SUCCESS,
                          resource=_worker_resource(
                              state.task_id,
                              f"{current}>{nxt}:{_worker_id(chain[current])}>{_worker_id(chain[nxt])}",
                              reason))
        if reason is SwitchReason.MALFORMED and state.malformed_from is not None:
            # A failed worker's malformed output is not shown to the next one.
            del state.messages[state.malformed_from:]
        state.worker_index = nxt
        state.worker_switches += 1
        state.consecutive_parse_failures = 0
        state.malformed_from = None
        state.no_progress_steps = 0
        state.progressed = False
        state.operation_counts.clear()

    async def _check_progress(self, env: TaskEnvironment, state: TaskState, models: ResolvedModels) -> None:
        """18 §4.1 no-progress stall: `stall_window` consecutive steps without
        a successful tool execution or capability activation."""

        policy = self._recovery_for(state)
        assert policy is not None
        state.no_progress_steps = 0 if state.progressed else state.no_progress_steps + 1
        state.progressed = False
        if state.no_progress_steps >= policy.stall_window:
            await self._event(env, state, AgentEvent.STALL_DETECTED, AuditResult.BLOCKED,
                              resource=f"stall:{state.task_id}:no_progress")
            await self._switch(env, state, models, SwitchReason.STALL, stuck=AgentFailureCode.STALLED)

    @staticmethod
    async def _invoke_cancellable(state: TaskState, provider: ModelProvider, messages, remaining):
        """One provider call, raced against the task's cancel event — the same
        race `_run_cancellable` gives a tool call. Without it a stop (or a
        cancel) would wait out the model's own timeout; with it, the call is
        abandoned and the loop's checkpoint takes over at once."""

        call = asyncio.ensure_future(asyncio.wait_for(
            provider.invoke(messages, timeout=max(0.001, min(provider.spec.timeout_seconds, remaining()))),
            timeout=max(0.001, remaining()),
        ))
        waiter = asyncio.ensure_future(state.cancel_event.wait())
        try:
            await asyncio.wait({call, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if call.done():
                return call.result()
            call.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await call
            raise _Interrupted()
        finally:
            waiter.cancel()
            if not call.done():
                call.cancel()

    # ── capability activation (owner decision §10, docs/CAPABILITY_MATRIX.md §4) ──

    async def _request_capabilities(
        self, env: TaskEnvironment, state: TaskState, proposal: RequestCapabilities
    ) -> AgentResult | None:
        await self._check_agent(env, state)  # as for a tool call (docs/29 §10.3)
        lines: list[str] = []
        for index, ask in enumerate(proposal.capabilities):
            if state.tripped is not None:
                # Nothing more is processed — above all, no confirmation is
                # issued — once the breaker has tripped (18 §5.2).
                break
            capability = ask.capability.strip()
            scope = dict(ask.resource_scope) if ask.resource_scope else None
            # docs/29 §10.3: an agent run's envelope comes first and can only
            # remove — outside it, nothing is activated, checked or offered.
            if not agent_envelope.activation_within_envelope(self._envelope(state), capability, scope):
                await self._envelope_denied(env, state, f"capability:{capability}")
                lines.append(agent_envelope.not_in_envelope(capability))
                continue
            info = env.security.describe_capability(capability)

            if info.status is CapabilityStatus.PROHIBITED:
                # PERM-006 / RT-T4: never offered as confirmable.
                await self._event(env, state, AgentEvent.CAPABILITY_ACTIVATION_REFUSED,
                                  AuditResult.BLOCKED, resource=f"capability:{capability}",
                                  decision=PermissionDecisionValue.DENY)
                lines.append(f"{capability}: prohibited — no such capability can ever be granted.")
                self._breaker.record_denial(state)
                continue
            if info.status is CapabilityStatus.UNKNOWN:
                lines.append(f"{capability}: unknown capability.")
                continue
            if scope and not set(scope) <= info.scope_keys:
                lines.append(f"{capability}: cannot be narrowed by {sorted(set(scope) - info.scope_keys)}.")
                continue
            if not modes.capability_usable(state.mode, info.operation_tiers):
                # 18 §3: refused, never put to the user as a confirmation that
                # could not lead to anything this task may do.
                lines.append(f"{capability}: " + modes.not_activated(state.mode))
                self._breaker.record_denial(state)
                continue
            if state.has_activation(capability, scope):
                lines.append(f"{capability}: already active.")
                continue

            if await env.security.holds_standing_grant(
                principal=state.principal, graph_id=state.graph_id,
                capability=capability, resource_scope=scope,
            ):
                state.activations.append(Activation(capability, scope, "standing"))
                state.progressed = True
                await self._event(env, state, AgentEvent.CAPABILITY_ACTIVATED, AuditResult.SUCCESS,
                                  resource=f"capability:{capability}")
                lines.append(f"{capability}: active for this task (your existing grant).")
                continue

            verdict = await env.security.authorize_activation(ActivationRequest(
                principal=state.principal, graph_id=state.graph_id, task_id=state.task_id,
                capability=capability, resource_scope=scope,
            ))
            if not verdict.needs_confirmation or verdict.binding is None:
                # Activating a capability the user has not granted is never
                # automatic: anything but "ask the human" is a refusal.
                await self._event(env, state, AgentEvent.CAPABILITY_ACTIVATION_REFUSED,
                                  AuditResult.BLOCKED, resource=f"capability:{capability}",
                                  decision=verdict.decision)
                lines.append(f"{capability}: not permitted.")
                self._breaker.record_denial(state)
                continue

            issued = await env.security.issue_confirmation(verdict.binding, verdict.risk_category)
            state.pending = PendingStep(
                kind="capability_activation", capability=capability,
                risk_category=verdict.risk_category, token=issued.token,
                expires_at=issued.expires_at, resource_scope=scope,
            )
            skipped = [a.capability for a in proposal.capabilities[index + 1 :]]
            if skipped:
                state.notes.append(
                    "Not yet processed (ask again after this confirmation): " + ", ".join(skipped)
                )
            if lines:
                state.messages.append(ctx.observation("\n".join(lines), limit=self._bounds.max_observation_chars))
            return await self._pause(env, state)

        state.messages.append(ctx.observation("\n".join(lines) or "nothing to activate",
                                              limit=self._bounds.max_observation_chars))
        return None

    async def _approve_activation(
        self, env: TaskEnvironment, state: TaskState, pending: PendingStep
    ) -> None:
        if not agent_envelope.activation_within_envelope(self._envelope(state), pending.capability,
                                                         pending.resource_scope):
            # An approval never carries an activation past the ceiling that
            # applies now (docs/29 §10.3).
            await self._envelope_denied(env, state, f"capability:{pending.capability}")
            state.messages.append(ctx.observation(agent_envelope.not_in_envelope(pending.capability),
                                                  limit=self._bounds.max_observation_chars))
            return
        verdict = await env.security.authorize_activation(ActivationRequest(
            principal=state.principal, graph_id=state.graph_id, task_id=state.task_id,
            capability=pending.capability, resource_scope=pending.resource_scope,
            confirmation_token=pending.token,
        ))
        self._trace(state, "authz.decision", resource=self._pending_resource(pending),
                    decision=verdict.decision.value, risk=verdict.risk_category.value)
        if verdict.needs_confirmation:
            raise ConfirmationMismatch()
        if not verdict.allowed:
            await self._event(env, state, AgentEvent.CONFIRMATION_REJECTED, AuditResult.BLOCKED,
                              resource=f"capability:{pending.capability}", decision=verdict.decision)
            state.messages.append(ctx.observation(
                f"Activating {pending.capability} is no longer permitted.",
                limit=self._bounds.max_observation_chars,
            ))
            self._breaker.record_denial(state)
            return

        await env.security.activate_for_task(
            principal=state.principal, task_id=state.task_id, capability=pending.capability,
            resource_scope=pending.resource_scope,
            expires_at=_utcnow() + timedelta(seconds=self._bounds.task_grant_ttl_seconds),
        )
        state.activations.append(Activation(pending.capability, pending.resource_scope, "task_grant"))
        state.progressed = True
        await self._event(env, state, AgentEvent.CONFIRMATION_ACCEPTED, AuditResult.SUCCESS,
                          resource=f"capability:{pending.capability}", decision=verdict.decision)
        await self._event(env, state, AgentEvent.CAPABILITY_ACTIVATED, AuditResult.SUCCESS,
                          resource=f"capability:{pending.capability}")
        state.messages.append(ctx.observation(
            f"{pending.capability}: approved by the user and active for this task.",
            limit=self._bounds.max_observation_chars,
        ))

    # ── tool operations (07 §8) ─────────────────────────────────────────

    @staticmethod
    def _within_mode(env: TaskEnvironment, state: TaskState, capability: str, capability_operation: str,
                     resource_type: ResourceType, operation: Operation) -> bool:
        if modes.MODE_CEILING[state.mode] is None:
            return True
        tier = env.security.operation_tier(
            capability=capability, capability_operation=capability_operation,
            resource_type=resource_type, operation=operation,
        )
        return modes.within_ceiling(state.mode, tier)

    def _visible_tools(self, state: TaskState) -> list[ToolHandle]:
        handles = self._tools.enabled_handles()
        if state.allowed_tool_ids is not None:
            handles = [h for h in handles if h.tool_id in state.allowed_tool_ids]
        if self._bounds.max_model_tool_nesting_depth < 1:
            handles = [h for h in handles if not h.is_model_tool]
        envelope = self._envelope(state)
        if envelope is not None:
            # docs/29 §10.3: an agent's worker is shown only what its envelope
            # could ever reach (the gate still refuses anything else).
            reachable = {e.capability for e in envelope.entries}
            # A model tool is reached only through `agent.model` routing
            # (docs/29 §12), never by naming it.
            handles = [h for h in handles if h.required_capability in reachable and not h.is_model_tool]
        return handles

    @staticmethod
    def _runtime_tools(state: TaskState) -> list[str]:
        agent = state.agent
        if agent is None:
            return []
        lines = []
        if agent.notebook:
            lines.append(
                f"{NOTEBOOK_TOOL}: this agent's own notes between runs (not a capability; nothing to request). "
                "operations: get {key}, put {key, value}, list {}. Keys are short slugs; notes are data."
            )
        if agent.model_tools:
            lines.append(
                f"{MODEL_ROUTE_TOOL}: ask JARVIS for a specialized model (capability model.invoke; "
                "operations: invoke [low_read]; arguments: prompt, optional role "
                "(writing, coding_suitable, long_context, vision, ...), optional preference "
                "(faster, cheaper, thorough)). JARVIS chooses the model; request model.invoke first."
            )
        return lines

    _NOTEBOOK_ARGS = {"get": {"key"}, "put": {"key", "value"}, "list": set()}

    async def _notebook_call(self, env: TaskEnvironment, state: TaskState, call: ToolCall, resource: str) -> None:
        """docs/29 §16.3: the agent's own notebook, served by the runtime for a
        run whose spec enables it — bound to the run's own agent (no argument
        can name another), bounded and gated by the composition root, counted
        as a tool call. Reads come back as untrusted data."""

        agent = state.agent
        assert agent is not None
        if not agent.notebook:
            await self._envelope_denied(env, state, resource)
            state.messages.append(ctx.observation(agent_envelope.not_in_envelope(NOTEBOOK_TOOL),
                                                  limit=self._bounds.max_observation_chars))
            return
        expected = self._NOTEBOOK_ARGS.get(call.operation)
        if expected is None:
            await self._reject(env, state, f"'{call.operation}' is not an operation of '{NOTEBOOK_TOOL}'.", resource)
            return
        arguments = dict(call.arguments)
        if set(arguments) != expected or not all(isinstance(v, str) for v in arguments.values()):
            await self._reject(env, state, f"{NOTEBOOK_TOOL}.{call.operation}: invalid_arguments "
                                           f"(expected exactly: {', '.join(sorted(expected)) or 'none'}).", resource)
            return
        if state.tool_calls >= self._max_tool_calls(state):
            raise _Stop(AgentFailureCode.MAX_TOOL_CALLS)
        if env.agent_runs is None:
            raise _Stop(AgentFailureCode.AGENT_UNAVAILABLE)
        state.tool_calls += 1
        limit = self._bounds.max_observation_chars
        if call.operation == "get":
            value = await env.agent_runs.notebook_get(agent, arguments["key"])
            state.messages.append(ctx.tool_observation(
                NOTEBOOK_TOOL, "get", ok=value is not None, content=value or "", error="no_such_note", limit=limit))
        elif call.operation == "list":
            keys = await env.agent_runs.notebook_keys(agent)
            state.messages.append(ctx.tool_observation(
                NOTEBOOK_TOOL, "list", ok=True, content="\n".join(keys) or "(empty)", error=None, limit=limit))
        else:
            refused = await env.agent_runs.notebook_put(agent, arguments["key"], arguments["value"])
            state.messages.append(ctx.tool_observation(
                NOTEBOOK_TOOL, "put", ok=refused is None, content="stored", error=refused, limit=limit))
        state.progressed = True

    async def _route_model_call(self, env: TaskEnvironment, state: TaskState, call: ToolCall,
                                resource: str) -> ToolCall | None:
        """docs/29 §12: an agent's model-as-tool request, resolved by JARVIS to
        one permitted, configured model tool. The result is an ordinary
        `model.invoke` call that then passes the envelope gate, activation and
        the engine like any other — routing chooses, it never authorizes."""

        agent = state.agent
        assert agent is not None
        if not agent.model_tools:
            await self._envelope_denied(env, state, resource)
            state.messages.append(ctx.observation(agent_envelope.not_in_envelope("model.invoke"),
                                                  limit=self._bounds.max_observation_chars))
            return None
        if call.operation != "invoke":
            await self._reject(env, state, f"'{call.operation}' is not an operation of '{MODEL_ROUTE_TOOL}'.",
                               resource)
            return None
        if env.agent_runs is None:
            raise _Stop(AgentFailureCode.AGENT_UNAVAILABLE)
        routed = await env.agent_runs.route_model(agent, dict(call.arguments))
        if isinstance(routed, str):
            await self._reject(env, state, f"No model was routed for this request ({routed}).", resource)
            return None
        self._trace(state, "agent.model.routed", resource=f"tool:{routed.tool_id}.invoke")
        return ToolCall(type="tool_call", tool=routed.tool_id, operation="invoke", arguments=dict(routed.arguments))

    async def _reject(self, env: TaskEnvironment, state: TaskState, message: str, resource: str,
                      decision: PermissionDecisionValue | None = None) -> None:
        await self._event(env, state, AgentEvent.PROPOSAL_REJECTED, AuditResult.BLOCKED,
                          resource=resource, decision=decision)
        state.messages.append(ctx.observation(message, limit=self._bounds.max_observation_chars))

    async def _tool_call(
        self, env: TaskEnvironment, state: TaskState, call: ToolCall, remaining,
        models: ResolvedModels | None = None,
    ) -> AgentResult | None:
        if state.agent is None:
            return await self._tool_request(env, state, call, remaining, models)
        # docs/29 §13.3 steps 1–3: an agent run's tool call is a request to the
        # Tool Gateway — its tool token, a fresh nonce, the call's digest —
        # before anything about the call itself is looked at. The gateway
        # re-validates the run, and the definition and run record (§10.3),
        # after the model proposed the call and before anything is decided, so
        # a stop, delete or change made while the model was thinking applies
        # to the call it proposed. What the call asks is then decided exactly
        # as before (steps 4–11, `_tool_request`).
        resource = f"tool:{call.tool}.{call.operation}"
        digest = operation_key(call.tool, call.operation, (call.platform or ExecutionPlatform.SERVER).value,
                               call.arguments, call.resource_ref, call.scope)
        admission = await self._admit(env, state, RunTokenPurpose.TOOL, digest)
        if admission.refused is not None:
            if admission.refused not in _GATEWAY_REQUEST_REFUSALS:
                raise _Stop(_gateway_stop(admission.refused))
            await self._reject(env, state, f"The Agent Gateway refused this request ({admission.refused}); "
                                           "nothing was performed.", resource)
            self._breaker.record_denial(state)
            return None
        if admission.replayed is not None:
            # A retry of a request already answered: the stored answer, and
            # nothing is performed again.
            for role, content in admission.replayed.get("observations", []):
                state.messages.append(ctx.ChatMessage(str(role), str(content)))
            return None
        before = len(state.messages)
        result = await self._tool_request(env, state, call, remaining, models)
        status = ("confirmation_pending" if state.pending is not None
                  else "platform_wait" if state.platform_wait is not None else "done")
        await self._settle(env, state, admission, {
            "status": status, "observations": [[m.role, m.content] for m in state.messages[before:]],
        })
        return result

    async def _tool_request(
        self, env: TaskEnvironment, state: TaskState, call: ToolCall, remaining,
        models: ResolvedModels | None = None,
    ) -> AgentResult | None:
        resource = f"tool:{call.tool}.{call.operation}"
        if self._recovery is not None and models is not None:
            # 18 §4.1 loop: the same operation, byte-for-byte, over and over.
            key = operation_key(call.tool, call.operation, (call.platform or ExecutionPlatform.SERVER).value,
                                call.arguments, call.resource_ref, call.scope)
            state.operation_counts[key] = state.operation_counts.get(key, 0) + 1
            if state.operation_counts[key] >= self._recovery_for(state).loop_repeat_limit:
                await self._event(env, state, AgentEvent.STALL_DETECTED, AuditResult.BLOCKED,
                                  resource=f"stall:{state.task_id}:loop")
                await self._switch(env, state, models, SwitchReason.LOOP, stuck=AgentFailureCode.STALLED)
                return None
        if state.agent is not None and call.tool == NOTEBOOK_TOOL:
            await self._notebook_call(env, state, call, resource)
            return None
        routed = False
        if state.agent is not None and call.tool == MODEL_ROUTE_TOOL:
            rewritten = await self._route_model_call(env, state, call, resource)
            if rewritten is None:
                return None
            call, routed = rewritten, True
            resource = f"tool:{call.tool}.{call.operation}"
        handle = self._tools.resolve(call.tool)
        if handle is None or (
            state.allowed_tool_ids is not None and call.tool not in state.allowed_tool_ids
        ):
            await self._reject(env, state, f"Tool '{call.tool}' is not available.", resource)
            return None
        if state.agent is not None and handle.is_model_tool and not routed:
            await self._reject(env, state, f"In an agent run a model is reached only through "
                                           f"'{MODEL_ROUTE_TOOL}'; JARVIS chooses which.", resource)
            self._breaker.record_denial(state)
            return None

        spec = handle.operations.get(call.operation)
        if spec is None:
            # 07 §3 / TL-T8: outside the enumerated mapping → not executable.
            await self._reject(env, state,
                               f"'{call.operation}' is not an operation of '{call.tool}'.", resource)
            return None

        platform = call.platform or ExecutionPlatform.SERVER
        if platform not in handle.platforms:
            await self._reject(env, state,
                               f"'{call.tool}' has no adapter on platform '{platform.value}'.", resource)
            return None

        activation = state.find_activation(handle.required_capability, call.scope)
        if activation is None:
            await self._reject(env, state,
                               f"Capability '{handle.required_capability}' is not active for this "
                               "task (or the scope is ambiguous). Request it first.", resource)
            return None

        resource_type = ResourceType(spec.resource_type)
        operation = Operation(spec.resource_operation)
        if spec.requires_resource_ref and not call.resource_ref:
            await self._reject(env, state, f"'{call.operation}' needs a resource_ref.", resource)
            return None
        resource_ref = call.resource_ref if spec.requires_resource_ref else None

        # 18 §3: the mode ceiling, enforced here — before the engine is asked,
        # so an over-ceiling operation is refused outright and never becomes a
        # confirmation prompt.
        if not self._within_mode(env, state, handle.required_capability, call.operation,
                                 resource_type, operation):
            await self._reject(env, state, modes.refusal(state.mode, f"'{call.tool}.{call.operation}'"),
                               resource)
            self._breaker.record_denial(state)
            return None

        scope = _merge_scope(activation.resource_scope, handle.natural_scope)
        # docs/29 §10.3: the envelope gate, before the engine is asked — the
        # capability, the operation, the effective scope and the tier must all
        # be inside the agent's compiled ceiling. Re-checked at every call.
        if not self._within_envelope(env, state, handle.required_capability, call.operation,
                                     resource_type, operation, scope):
            await self._envelope_denied(env, state, resource)
            state.messages.append(ctx.observation(agent_envelope.not_in_envelope(handle.required_capability),
                                                  limit=self._bounds.max_observation_chars))
            return None

        if handle.is_model_tool and self._bounds.max_model_tool_nesting_depth < 1:
            # RT-T7 / OD-RT-1: the nesting bound, enforced by the runtime.
            await self._reject(env, state, "Model-tool nesting limit reached.", resource)
            return None

        if state.tool_calls >= self._max_tool_calls(state):
            raise _Stop(AgentFailureCode.MAX_TOOL_CALLS)
        request = ActionRequest(
            principal=state.principal, graph_id=state.graph_id, task_id=state.task_id,
            capability=handle.required_capability, capability_operation=call.operation,
            resource_type=resource_type, operation=operation, resource_ref=resource_ref,
            arguments=self._bound_arguments(handle.tool_id, call.operation, platform, call.arguments, scope),
            resource_scope=scope,
        )
        verdict = await env.security.authorize_action(request)
        self._trace(state, "authz.decision", resource=resource, decision=verdict.decision.value,
                    risk=verdict.risk_category.value)

        if verdict.prohibited:
            await self._reject(env, state, "That action is prohibited and can never be performed.",
                               resource, decision=verdict.decision)
            self._breaker.record_denial(state)
            return None
        if verdict.needs_confirmation and verdict.binding is not None:
            issued = await env.security.issue_confirmation(verdict.binding, verdict.risk_category)
            state.pending = PendingStep(
                kind="tool_operation", capability=handle.required_capability,
                risk_category=verdict.risk_category, token=issued.token, expires_at=issued.expires_at,
                tool_id=handle.tool_id, operation=call.operation, arguments=dict(call.arguments),
                resource_ref=resource_ref, platform=platform, resource_scope=scope,
                resource_type=resource_type, resource_operation=operation, requested_scope=call.scope,
            )
            return await self._pause(env, state)
        if not verdict.allowed:
            # Denial-as-observation (05 §1). Deliberately generic: which
            # dimension failed is not an oracle the model (or a prompt injector)
            # can use to probe other users' resources (04 §7).
            message = (
                "Not permitted: the capability is not granted for this scope."
                if verdict.reason == "capability_missing"
                else "Not found or not permitted."
            )
            await self._reject(env, state, message, resource, decision=verdict.decision)
            self._breaker.record_denial(state)
            return None

        admission = None
        if routed:
            # docs/29 §12: a model reached as a tool is a Model Gateway request
            # too — the run's model token, the alias of that model tool, the
            # spec's `model.invoke`, the profile and the owner's policy, now —
            # after the envelope gate and the engine decided the call itself.
            admission = await self._admit(
                env, state, RunTokenPurpose.MODEL,
                operation_key(handle.tool_id, call.operation, platform.value, call.arguments, resource_ref, scope),
                model=ModelCallFacts(alias=f"model-tool:{handle.tool_id}"))
            if admission.refused is not None:
                raise _Stop(_gateway_stop(admission.refused))
            if admission.replayed is not None:
                for role, content in admission.replayed.get("observations", []):
                    state.messages.append(ctx.ChatMessage(str(role), str(content)))
                return None
        before = len(state.messages)
        output = await self._execute(env, state, handle, call.operation, platform, dict(call.arguments),
                                     resource_ref, scope, remaining)
        if admission is not None:
            await self._settle(env, state, admission,
                               {"observations": [[m.role, m.content] for m in state.messages[before:]]})
        self._maybe_wait_for_platform(state, output, tool=handle.tool_id, operation=call.operation,
                                      arguments=dict(call.arguments), platform=platform,
                                      resource_ref=call.resource_ref, scope=call.scope)
        return None

    async def _approve_tool_operation(
        self, env: TaskEnvironment, state: TaskState, pending: PendingStep
    ) -> None:
        handle = self._tools.resolve(pending.tool_id or "")
        if handle is None:
            state.messages.append(ctx.observation(
                f"Tool '{pending.tool_id}' is no longer available; nothing was performed.",
                limit=self._bounds.max_observation_chars,
            ))
            return
        if state.tool_calls >= self._max_tool_calls(state):
            raise _Stop(AgentFailureCode.MAX_TOOL_CALLS)
        if not self._within_mode(env, state, pending.capability, pending.operation or "",
                                 pending.resource_type or ResourceType.TOOL_ACTION,
                                 pending.resource_operation or Operation.CREATE):
            state.messages.append(ctx.observation(
                modes.refusal(state.mode, f"'{pending.tool_id}.{pending.operation}'"),
                limit=self._bounds.max_observation_chars,
            ))
            self._breaker.record_denial(state)
            return
        if not self._within_envelope(env, state, pending.capability, pending.operation or "",
                                     pending.resource_type or ResourceType.TOOL_ACTION,
                                     pending.resource_operation or Operation.CREATE, pending.resource_scope):
            await self._envelope_denied(env, state, self._pending_resource(pending))
            state.messages.append(ctx.observation(agent_envelope.not_in_envelope(pending.capability),
                                                  limit=self._bounds.max_observation_chars))
            return

        verdict = await env.security.authorize_action(ActionRequest(
            principal=state.principal, graph_id=state.graph_id, task_id=state.task_id,
            capability=pending.capability, capability_operation=pending.operation or "",
            resource_type=pending.resource_type or ResourceType.TOOL_ACTION,
            operation=pending.resource_operation or Operation.CREATE,
            resource_ref=pending.resource_ref,
            arguments=self._bound_arguments(
                pending.tool_id or "", pending.operation or "", pending.platform or ExecutionPlatform.SERVER,
                pending.arguments, pending.resource_scope,
            ),
            resource_scope=pending.resource_scope,
            confirmation_token=pending.token,
        ))
        if verdict.needs_confirmation:
            raise ConfirmationMismatch()
        if not verdict.allowed:
            # Something changed since the pause (a grant revoked, membership
            # left). The approval does not override that.
            await self._event(env, state, AgentEvent.CONFIRMATION_REJECTED, AuditResult.BLOCKED,
                              resource=self._pending_resource(pending), decision=verdict.decision)
            state.messages.append(ctx.observation(
                "The approved action is no longer permitted; it was not performed.",
                limit=self._bounds.max_observation_chars,
            ))
            self._breaker.record_denial(state)
            return

        await self._event(env, state, AgentEvent.CONFIRMATION_ACCEPTED, AuditResult.SUCCESS,
                          resource=self._pending_resource(pending), decision=verdict.decision)
        remaining_seconds = self._wall_clock(state) - state.run_seconds_used
        platform = pending.platform or ExecutionPlatform.SERVER
        output = await self._execute(env, state, handle, pending.operation or "", platform,
                                     pending.arguments, pending.resource_ref, pending.resource_scope,
                                     lambda: remaining_seconds)
        self._maybe_wait_for_platform(state, output, tool=handle.tool_id, operation=pending.operation or "",
                                      arguments=dict(pending.arguments), platform=platform,
                                      resource_ref=pending.resource_ref, scope=pending.requested_scope)

    async def _execute(
        self, env: TaskEnvironment, state: TaskState, handle: ToolHandle, operation: str,
        platform: ExecutionPlatform, arguments: dict, resource_ref: str | None,
        scope: dict[str, str] | None, remaining,
    ) -> ToolOutput:
        if remaining() <= 0:
            raise _Stop(AgentFailureCode.TIMEOUT)
        await self._precheck(env, state, handle.projected_cost_per_call)

        state.tool_calls += 1
        await _release_store(env)
        output = await self._run_cancellable(
            state, handle.tool_id, platform,
            ToolInvocation(
                tool_id=handle.tool_id, operation=operation, arguments=arguments,
                user_id=state.principal.user_id, task_id=state.task_id, platform=platform,
                resource_ref=resource_ref, resource_scope=scope,
                device_id=state.principal.device_id,
                # docs/22 §1: only a tool registered to bind it receives the
                # user's own instruction — from the task, never the proposal.
                task_input=state.user_input if handle.binds_task_input else None,
            ),
            timeout=max(0.001, min(handle.timeout_seconds, remaining())),
        )
        # 20 §2.4: if that ran under a break-glass record, its invocation (and,
        # once spent, the record's end) is audited now, with the task.
        await env.security.settle_break_glass(principal=state.principal, graph_id=state.graph_id,
                                              task_id=state.task_id)
        state.cost += output.estimated_cost
        # USAGE-001: exactly one UsageEvent per execution, success or failure.
        usage_id = await env.usage.record(
            principal=state.principal, graph_id=state.graph_id, kind=output.usage_kind,
            units=output.units, estimated_cost=output.estimated_cost,
            provider=output.provider, model=output.model, tool_id=handle.tool_id,
        )
        await self._attribute(env, state, usage_id, output.estimated_cost)
        self._trace(state, f"usage.{output.usage_kind.value}", resource=f"tool:{handle.tool_id}",
                    units=output.units, cost=output.estimated_cost)
        # Execution-layer failures (sandbox_violation, egress_denied,
        # platform_unsupported, timeout, ...) already arrive as a structured
        # `ExecutionErrorCode` in `output.error` (server/execution/contracts.py).
        # Folding it into the audited resource — rather than adding a parallel
        # audit path for the execution boundary — keeps AGENT_TOOL_FAILED
        # queryable by *which* boundary refused the call without letting
        # `server/tools`/`server/fs`/`server/net` write audit events of their
        # own (16 §5: only the runtime's SecurityPort records).
        resource = f"tool:{handle.tool_id}.{operation}"
        if not output.ok and output.error:
            resource = f"{resource}:{output.error}"
        await self._event(
            env, state, AgentEvent.TOOL_EXECUTED if output.ok else AgentEvent.TOOL_FAILED,
            AuditResult.SUCCESS if output.ok else AuditResult.FAILURE,
            resource=resource, decision=PermissionDecisionValue.ALLOW,
        )
        state.messages.append(ctx.tool_observation(
            handle.tool_id, operation, ok=output.ok, content=output.content, error=output.error,
            limit=self._bounds.max_observation_chars,
        ))
        self._breaker.record_tool_outcome(state, ok=output.ok, error=output.error)
        if output.ok:
            state.progressed = True
        self._notify_step(state)
        return output

    def _maybe_wait_for_platform(
        self, state: TaskState, output: ToolOutput, *, tool: str, operation: str, arguments: dict,
        platform: ExecutionPlatform, resource_ref: str | None, scope: dict[str, str] | None,
    ) -> None:
        """docs/23 §5.3: a device operation refused because an on-device
        dependency is unavailable puts the *task* into a bounded wait — at most
        `max_platform_waits` times, each for at most `max_platform_wait_seconds`.
        Only the call's description is kept; nothing is queued on the device,
        and the refused operation itself is never re-sent.

        The same bounded wait covers a device that was not connected but was
        sent a push wake (docs/23 §4): `device_unavailable` with
        `required_platform: device_channel`. The task resumes only when that
        device's authenticated channel is back — never on the push itself."""

        waitable = (output.error == "platform_unavailable" and bool(output.required_platform)) or (
            output.error == "device_unavailable" and output.required_platform == "device_channel"
        )
        if (
            output.ok
            or not waitable
            or platform is not ExecutionPlatform.ANDROID
            or state.principal.device_id is None
            or state.platform_waits >= self._bounds.max_platform_waits
        ):
            return
        state.platform_waits += 1
        state.platform_wait = PlatformWaitStep(
            tool=tool, operation=operation, arguments=arguments, platform=platform,
            resource_ref=resource_ref, scope=scope, dependency=output.required_platform,
            device_id=state.principal.device_id,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=self._bounds.max_platform_wait_seconds),
        )

    async def _run_cancellable(
        self, state: TaskState, tool_id: str, platform: ExecutionPlatform,
        invocation: ToolInvocation, *, timeout: float,
    ) -> ToolOutput:
        """Run one tool call, racing it against the task's cancel signal (05 §9:
        "the runtime stops the loop, aborts any in-flight tool").

        Without the race, `/cancel` only flags the task and the in-flight
        operation runs to its own timeout. Cancelling the call's asyncio task is
        what reaches the executor: `server.execution.process` answers a
        cancellation by killing the child's whole process group.
        """

        if state.cancel_event.is_set():
            return ToolOutput(ok=False, error="cancelled")
        run = asyncio.ensure_future(self._tools.run(tool_id, platform, invocation, timeout=timeout))
        waiter = asyncio.ensure_future(state.cancel_event.wait())
        try:
            await asyncio.wait({run, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if run.done():
                return run.result()
            run.cancel()
            try:
                await run
            except asyncio.CancelledError:
                pass
            return ToolOutput(ok=False, error="cancelled")
        finally:
            waiter.cancel()
            if not run.done():
                run.cancel()

    @staticmethod
    def _bound_arguments(tool_id: str, operation: str, platform: ExecutionPlatform,
                         arguments: Any, scope: dict[str, str] | None) -> dict[str, Any]:
        """What a confirmation token binds to (hashed): the exact tool, operation,
        platform, arguments and narrowing — so a token for one call can never
        authorize a different one (PERM-004)."""

        return {
            "tool": tool_id, "operation": operation, "platform": platform.value,
            "arguments": dict(arguments or {}), "scope": scope or {},
        }

    # ── usage (13) ──────────────────────────────────────────────────────

    async def _precheck(self, env: TaskEnvironment, state: TaskState, projected: float) -> None:
        if projected > 0 and state.cost + projected > self._task_budget(state):
            await self._event(env, state, AgentEvent.LIMIT_EXCEEDED, AuditResult.BLOCKED,
                              resource="limit:per_task_budget")
            raise _Stop(AgentFailureCode.BUDGET_EXCEEDED)
        try:
            await env.usage.precheck(principal=state.principal, projected_cost=projected)
        except UsageLimitReached as exc:
            await self._event(env, state, AgentEvent.LIMIT_EXCEEDED, AuditResult.BLOCKED,
                              resource=f"limit:{exc.limit}")
            raise _Stop(AgentFailureCode.BUDGET_EXCEEDED if exc.is_budget
                        else AgentFailureCode.RATE_LIMITED) from None
        if state.agent is not None and projected > 0:
            # docs/29 §10.5 / §12.2: then the agent's month, read live from
            # attributed usage — concurrent runs of one agent share it.
            if env.agent_runs is None:
                raise _Stop(AgentFailureCode.AGENT_UNAVAILABLE)
            refused = await env.agent_runs.agent_budget(state.agent, projected_cost=projected)
            if refused is not None:
                await self._event(env, state, AgentEvent.LIMIT_EXCEEDED, AuditResult.BLOCKED,
                                  resource="limit:agent_monthly_budget")
                raise _Stop(_gateway_stop(refused))

    async def _meter_model(self, env: TaskEnvironment, state: TaskState, provider: ModelProvider,
                           *, units: int, cost: float) -> None:
        state.cost += cost
        usage_id = await env.usage.record(
            principal=state.principal, graph_id=state.graph_id, kind=UsageKind.MODEL_CALL,
            units=units, estimated_cost=cost, provider=provider.spec.provider,
            model=provider.spec.model,
        )
        await self._attribute(env, state, usage_id, cost)
        self._trace(state, "usage.model_call", resource=f"worker:{_worker_id(provider)}", units=units, cost=cost)

    # ── lifecycle ───────────────────────────────────────────────────────

    async def _pause(self, env: TaskEnvironment, state: TaskState) -> AgentResult:
        if state.tripped is not None:
            # Tripped while this step was being decided: the confirmation just
            # issued is spent by the stop, and the task never pauses.
            return await self._emergency_stop(env, state)
        await self._set_status(env, state, AgentTaskStatus.AWAITING_CONFIRMATION)
        assert state.pending is not None
        await self._event(env, state, AgentEvent.CONFIRMATION_ISSUED, AuditResult.SUCCESS,
                          resource=self._pending_resource(state.pending),
                          decision=PermissionDecisionValue.REQUIRE_CONFIRMATION)
        await self._event(env, state, AgentEvent.TASK_PAUSED, AuditResult.SUCCESS)
        return self._result_from_state(state, AgentTaskStatus.AWAITING_CONFIRMATION)

    async def _pause_for_platform(self, env: TaskEnvironment, state: TaskState) -> AgentResult:
        if state.tripped is not None:
            state.platform_wait = None
            return await self._emergency_stop(env, state)
        assert state.platform_wait is not None
        await self._set_status(env, state, AgentTaskStatus.WAITING_FOR_PLATFORM)
        await self._event(env, state, AgentEvent.TASK_PAUSED, AuditResult.SUCCESS,
                          resource=f"platform_wait:{state.platform_wait.dependency}")
        return self._result_from_state(state, AgentTaskStatus.WAITING_FOR_PLATFORM)

    async def _set_status(self, env: TaskEnvironment, state: TaskState, status: AgentTaskStatus) -> None:
        await update_task_row(
            env.session, state.task_id, status=status, iterations=state.iterations,
            model_calls=state.model_calls, tool_calls=state.tool_calls,
            worker_switches=state.worker_switches,
        )

    async def _finish(self, env: TaskEnvironment, state: TaskState, status: AgentTaskStatus,
                      *, response: str | None = None,
                      failure: AgentFailureCode | None = None, unresolved: bool = False) -> AgentResult:
        state.pending = None
        # 20 §2.4: a break-glass record dies with its task — first, before
        # anything else here can yield to another request.
        await env.security.settle_break_glass(principal=state.principal, graph_id=state.graph_id,
                                              task_id=state.task_id, ended=status, failure=failure)
        revoked = await env.security.deactivate_task(principal=state.principal, task_id=state.task_id)
        # No confirmation token outlives its task (18 §5.3 step 3) — a stop, a
        # cancel, and an ordinary end alike.
        await env.security.invalidate_confirmations(principal=state.principal, task_id=state.task_id)
        self._tools.release_task(state.task_id)
        if revoked:
            await self._event(env, state, AgentEvent.CAPABILITY_DEACTIVATED, AuditResult.SUCCESS)
        if status is AgentTaskStatus.COMPLETED and response and not unresolved:
            await self._form_memory(env, state, response)
        await update_task_row(
            env.session, state.task_id, status=status, iterations=state.iterations,
            model_calls=state.model_calls, tool_calls=state.tool_calls, response=response,
            failure_code=failure.value if failure else None, worker_switches=state.worker_switches,
        )
        if state.agent is not None and env.agent_runs is not None:
            # docs/29 §19: the run's outcome goes to its owner's inbox, as data.
            await env.agent_runs.finished(state.agent, task_id=state.task_id, status=status,
                                          response=response, failure=failure, cost=state.cost)
        event = {
            AgentTaskStatus.COMPLETED: AgentEvent.TASK_COMPLETED,
            AgentTaskStatus.CANCELLED: AgentEvent.TASK_CANCELLED,
        }.get(status, AgentEvent.TASK_FAILED)
        await self._event(env, state, event,
                          AuditResult.SUCCESS if status is AgentTaskStatus.COMPLETED else AuditResult.FAILURE)
        self._states.pop(state.task_id)
        self._notify_ended(state, status, failure=failure, response=response, unresolved=unresolved)
        if unresolved:
            state.notes.append("The worker reported that it could not resolve this request.")
        result = self._result_from_state(state, status)
        result.response = response
        result.unresolved = unresolved
        if failure is not None:
            result.failure = AgentFailure(code=failure, message=_FAILURE_MESSAGES[failure])
        return result

    async def _form_memory(self, env: TaskEnvironment, state: TaskState, answer: str) -> None:
        """docs/21 §2.2 (a) / §3: after a completed task, one extraction call to
        the task's own worker, under the task's own bounds, budget and metering.
        Its inputs are the user's request and the final answer only (MP-T6).

        Formation is a write, so a draft/suggest/observe task never forms memory
        (18 §3). It never changes the task's outcome: any failure here is a note."""

        formation = env.memory
        if formation is None or state.mode is not TaskMode.EXECUTE:
            return
        if state.agent is not None:
            # docs/29 §16.2 (AGENT-T17): agent output never becomes user
            # memory, whatever `memory.auto_extract` says.
            return
        try:
            messages = formation.plan(principal=state.principal, graph_id=state.graph_id,
                                      user_request=state.user_input, final_answer=answer)
            if not messages:
                return
            if state.model_calls >= self._max_model_calls(state):
                state.notes.append(_MEMORY_SKIPPED_NOTE)
                return
            models = await env.models.resolve(principal=state.principal, graph_id=state.graph_id)
            provider = models.chain[min(state.worker_index, len(models.chain) - 1)]
            spec = provider.spec
            projected = spec.projected_cost(prompt_chars=sum(len(m.content) for m in messages))
            if projected > 0 and state.cost + projected > self._task_budget(state):
                state.notes.append(_MEMORY_SKIPPED_NOTE)
                return
            try:
                await env.usage.precheck(principal=state.principal, projected_cost=projected)
            except UsageLimitReached:
                state.notes.append(_MEMORY_SKIPPED_NOTE)
                return
            state.model_calls += 1
            await _release_store(env)
            try:
                result = await asyncio.wait_for(
                    provider.invoke(messages, timeout=spec.timeout_seconds), timeout=spec.timeout_seconds
                )
            except (ModelUnavailable, asyncio.TimeoutError):
                await self._meter_model(env, state, provider, units=0, cost=0.0)
                state.notes.append(_MEMORY_SKIPPED_NOTE)
                return
            cost = spec.pricing.cost(prompt_tokens=result.prompt_tokens,
                                     completion_tokens=result.completion_tokens)
            await self._meter_model(env, state, provider, units=result.total_tokens, cost=cost)
            state.notes.extend(await formation.commit(
                principal=state.principal, graph_id=state.graph_id, task_id=state.task_id,
                model_output=result.content,
            ))
        except Exception:  # noqa: BLE001 — memory formation never fails a finished task
            logger.warning("memory formation failed for task %s", state.task_id, exc_info=False)
            state.notes.append(_MEMORY_SKIPPED_NOTE)

    async def _fail(self, env: TaskEnvironment, state: TaskState, code: AgentFailureCode) -> AgentResult:
        return await self._finish(env, state, AgentTaskStatus.FAILED, failure=code)

    async def _internal_failure(self, env: TaskEnvironment, state: TaskState, exc: Exception) -> AgentResult:
        """Anything unexpected — an exception from a dependency, or from input
        no check anticipated — fails the task closed, like any other failure:
        in-flight work is aborted, grants revoked, tokens spent, temp released,
        any break-glass record ended, and the failure recorded. Without this
        the request would error out, its transaction (task row and audit
        included) would roll back, and the task's live state would be left
        behind in this process."""

        logger.error("task %s failed on an unexpected %s", state.task_id, type(exc).__name__, exc_info=exc)
        state.cancel_event.set()
        try:
            return await self._fail(env, state, AgentFailureCode.INTERNAL_ERROR)
        except Exception:
            # The store itself is failing: the request cannot record anything.
            # At least nothing of the task survives in this process.
            self._states.pop(state.task_id)
            raise

    async def _emergency_stop(self, env: TaskEnvironment, state: TaskState) -> AgentResult:
        """Enforce a breaker trip (18 §5.3). By the time this runs, `trip()`
        has already set the cancel event, so an in-flight tool call was aborted
        and its process group killed (step 2). `_finish` then marks the task
        terminal (1), spends its confirmation tokens (3), and revokes its task
        grants and releases its temp root (4). The trip is audited (6) and the
        user told plainly what stopped the task (7). The task is not resumed,
        and nothing that was pending is replayed (§6)."""

        trip = state.tripped
        assert trip is not None
        if state.stop_enforced:
            # Another request is already enforcing this stop (18 §5.3 is done
            # once); this one only reports it.
            result = self._result_from_state(state, AgentTaskStatus.FAILED)
            result.failure = _trip_failure(trip)
            return result
        state.stop_enforced = True
        result = await self._finish(env, state, AgentTaskStatus.FAILED,
                                    failure=AgentFailureCode.EMERGENCY_STOP)
        # The failure message is what the user's device shows for a failed
        # task, so the plain reason goes there, ahead of the generic text.
        result.failure = _trip_failure(trip)
        await self._event(env, state, AgentEvent.BREAKER_TRIPPED, AuditResult.BLOCKED,
                          resource=_trip_resource(trip.scope, state.task_id, trip.source, trip.reason))
        return result

    async def _fail_row(self, env: TaskEnvironment, row, code: AgentFailureCode,
                        caller: Principal) -> AgentResult:
        """A paused task whose live state is gone (a restart, a pruned pause):
        closed from its row, fail-closed. If another request ended it first,
        its outcome stands and this is simply not awaiting any more."""

        updated, closed = await self._close_from_row(env, row, AgentTaskStatus.FAILED, code,
                                                     event=AgentEvent.TASK_ABANDONED)
        if not closed:
            raise TaskNotAwaiting()
        result = self._result_from_row(updated)
        result.failure = AgentFailure(code=code, message=_FAILURE_MESSAGES[code])
        return result

    async def _close_from_row(self, env: TaskEnvironment, row, status: AgentTaskStatus,
                              failure: AgentFailureCode | None, *, event: AgentEvent):
        """Close a task that has no live state in this process, from its row.

        The row is closed first and only if it is still non-terminal
        (`close_if_live`): that write waits for any request still finishing the
        same task and then sees its outcome, so a completed task is never
        rewritten, and the cleanup below never runs twice. Then, exactly as
        `_finish` does for a live task: any break-glass record ends, task
        grants are revoked, confirmation tokens spent, the temp root released,
        and the close is audited. Returns (row as it now is, whether closed)."""

        updated, closed = await close_if_live(env.session, row.task_id, status=status,
                                              failure_code=failure.value if failure else None)
        if not closed:
            return updated, False
        principal = Principal(user_id=row.user_id, device_id=row.device_id,
                              session_id=row.session_id, active_graph_id=row.graph_id)
        await env.security.settle_break_glass(principal=principal, graph_id=row.graph_id, task_id=row.task_id,
                                              ended=status, failure=failure)
        await env.security.deactivate_task(principal=principal, task_id=row.task_id)
        await env.security.invalidate_confirmations(principal=principal, task_id=row.task_id)
        self._tools.release_task(row.task_id)
        await env.security.record(
            event, principal=principal, graph_id=row.graph_id,
            resource=f"agent_task:{row.task_id}" + (f":{failure.value}" if failure else ""),
            result=AuditResult.FAILURE,
        )
        return updated, True

    async def reconcile_after_restart(self, env: TaskEnvironment) -> list[uuid.UUID]:
        """Close every task a previous run of the server left unfinished
        (18 §2, DECISION_REGISTER "a restart fails a paused task closed").

        Called once at startup, before any request is served, when no task can
        be live in this process: the transcript and the paused action were
        volatile (MEM-001), so none of them can continue. A paused one fails
        `confirmation_state_lost`, a running one `internal_error`, and each is
        cleaned up and audited like any other end — its task grants no longer
        wait out their expiry, its tokens are spent, its temp root is removed.
        Assumes one server process owns the store (the pilot's deployment
        model; the latch, breaker and records are per-process too)."""

        closed: list[uuid.UUID] = []
        for task_id in await non_terminal_task_ids(env.session):
            if self._states.get(task_id) is not None:
                continue
            row = await load_task_row(env.session, task_id)
            if row is None:
                continue
            code = (AgentFailureCode.CONFIRMATION_STATE_LOST
                    if row.status == AgentTaskStatus.AWAITING_CONFIRMATION.value
                    else AgentFailureCode.PLATFORM_UNAVAILABLE
                    if row.status == AgentTaskStatus.WAITING_FOR_PLATFORM.value
                    else AgentFailureCode.INTERNAL_ERROR)
            _, did = await self._close_from_row(env, row, AgentTaskStatus.FAILED, code,
                                                event=AgentEvent.TASK_ABANDONED)
            if did:
                closed.append(task_id)
        return closed

    async def _event(self, env: TaskEnvironment, state: TaskState, event: AgentEvent,
                     result: AuditResult, *, resource: str | None = None,
                     decision: PermissionDecisionValue | None = None) -> None:
        resource = resource or f"agent_task:{state.task_id}"
        await env.security.record(
            event, principal=state.principal, graph_id=state.graph_id,
            resource=resource, result=result, decision=decision,
        )
        self._trace(state, event.value, result=result.value, resource=resource,
                    decision=decision.value if decision is not None else None)

    # ── observation (19 §4, §6) — the Judge's inputs, never its outputs ──

    def _trace(self, state: TaskState, name: str, **fields: Any) -> None:
        if self._observer is None:
            return
        state.trace_events.append(TraceEvent(
            position=max(0, len(state.messages) - 2),
            at=time.monotonic() - state.created_monotonic, name=name, **fields,
        ))

    @staticmethod
    def _snapshot(state: TaskState, status: AgentTaskStatus, *, failure: AgentFailureCode | None = None,
                  response: str | None = None, unresolved: bool = False) -> TaskSnapshot:
        # messages[0] is the system prompt and messages[1] the hydrated context
        # with the user's request: neither is an artifact the worker produced.
        return TaskSnapshot(
            task_id=state.task_id, user_id=state.principal.user_id, graph_id=state.graph_id,
            mode=state.mode.value, user_input=state.user_input, status=status.value,
            failure_code=failure.value if failure is not None else None, final_response=response,
            unresolved=unresolved, iterations=state.iterations, model_calls=state.model_calls,
            tool_calls=state.tool_calls, worker_switches=state.worker_switches, denials=state.denials,
            violations=state.violations, rejections=state.rejections,
            tripped_source=state.tripped.source if state.tripped is not None else None,
            elapsed_seconds=time.monotonic() - state.created_monotonic,
            transcript=tuple((m.role, m.content) for m in state.messages[2:]),
            events=tuple(state.trace_events),
        )

    def _notify_step(self, state: TaskState) -> None:
        if self._observer is None:
            return
        try:
            self._observer.step_completed(
                state.task_id, state.tool_calls,
                lambda: self._snapshot(state, AgentTaskStatus.RUNNING),
            )
        except Exception:  # noqa: BLE001 — an observer can never affect a task
            logger.warning("task observer failed on a step of %s", state.task_id, exc_info=False)

    def _notify_ended(self, state: TaskState, status: AgentTaskStatus, *, failure: AgentFailureCode | None,
                      response: str | None, unresolved: bool) -> None:
        if self._observer is None:
            return
        try:
            self._observer.task_ended(self._snapshot(state, status, failure=failure, response=response,
                                                     unresolved=unresolved))
        except Exception:  # noqa: BLE001 — an observer can never affect a task
            logger.warning("task observer failed at the end of %s", state.task_id, exc_info=False)

    # ── agent runs (docs/29 Phase 2) ────────────────────────────────────
    #
    # An agent run is an ordinary task with a tighter ceiling: each bound is
    # the lower of the server's and the compiled spec's, never higher.

    def _max_model_calls(self, state: TaskState) -> int:
        if state.agent is None:
            return self._bounds.max_model_calls
        return min(self._bounds.max_model_calls, state.agent.max_model_calls)

    def _max_tool_calls(self, state: TaskState) -> int:
        if state.agent is None:
            return self._bounds.max_tool_calls
        return min(self._bounds.max_tool_calls, state.agent.max_tool_calls)

    def _wall_clock(self, state: TaskState) -> float:
        if state.agent is None:
            return self._bounds.wall_clock_timeout_seconds
        return min(self._bounds.wall_clock_timeout_seconds, state.agent.max_run_seconds)

    def _task_budget(self, state: TaskState) -> float:
        if state.agent is None:
            return self._bounds.per_task_budget
        return min(self._bounds.per_task_budget, state.agent.budget_per_run)

    @staticmethod
    async def _resolve_models(env: TaskEnvironment, state: TaskState) -> ResolvedModels:
        if state.agent is None:
            return await env.models.resolve(principal=state.principal, graph_id=state.graph_id)
        # The selected profile's configured entry, through the same key path.
        return await env.models.resolve(principal=state.principal, graph_id=state.graph_id,
                                        agent_model_ref=state.agent.model_ref)

    @staticmethod
    async def _attribute(env: TaskEnvironment, state: TaskState, usage_id: uuid.UUID | None, cost: float) -> None:
        """docs/29 §17: every usage event of an agent run is joined to the run
        — no agent call is free or invisible."""

        if state.agent is not None and env.agent_runs is not None and usage_id is not None:
            await env.agent_runs.usage_recorded(state.agent, usage_id=usage_id, cost=cost)

    @staticmethod
    def _envelope(state: TaskState) -> agent_envelope.Envelope | None:
        return state.agent.envelope if state.agent is not None else None

    def _within_envelope(self, env: TaskEnvironment, state: TaskState, capability: str, capability_operation: str,
                         resource_type: ResourceType, operation: Operation,
                         scope: Mapping[str, str] | None) -> bool:
        envelope = self._envelope(state)
        if envelope is None:
            return True
        tier = env.security.operation_tier(capability=capability, capability_operation=capability_operation,
                                           resource_type=resource_type, operation=operation)
        return agent_envelope.within_envelope(envelope, capability, capability_operation, scope, tier)

    async def _envelope_denied(self, env: TaskEnvironment, state: TaskState, resource: str) -> None:
        await self._event(env, state, AgentEvent.ENVELOPE_DENIED, AuditResult.BLOCKED, resource=resource,
                          decision=PermissionDecisionValue.DENY)
        self._breaker.record_denial(state)

    async def _admit(self, env: TaskEnvironment, state: TaskState, purpose: RunTokenPurpose,
                     digest: str, *, model: ModelCallFacts | None = None) -> GatewayAdmission:
        """One request of this agent run to the Agent Gateway (docs/29 §11)."""

        assert state.agent is not None
        if env.agent_runs is None:
            raise _Stop(AgentFailureCode.AGENT_UNAVAILABLE)
        admission = await env.agent_runs.admit(state.agent, gateway_request(state.agent, purpose, digest),
                                               model=model)
        outcome = ("refused" if admission.refused is not None
                   else "replayed" if admission.replayed is not None else "admitted")
        self._trace(state, f"agent.gateway.{outcome}", resource=f"gateway:{purpose.value}",
                    result=admission.refused)
        return admission

    @staticmethod
    async def _settle(env: TaskEnvironment, state: TaskState, admission: GatewayAdmission,
                      response: Mapping[str, Any]) -> None:
        if state.agent is not None and env.agent_runs is not None and admission.ticket is not None:
            await env.agent_runs.settle(state.agent, admission, response)

    @staticmethod
    async def _check_agent(env: TaskEnvironment, state: TaskState) -> None:
        """Re-validate an agent run's definition (docs/29 §10.3, §14.3): it
        must still exist, be active, and be exactly the version and hash the
        run started with. Read fresh every time — nothing is cached."""

        if state.agent is None:
            return
        if env.agent_runs is None:
            raise _Stop(AgentFailureCode.AGENT_UNAVAILABLE)
        code = await env.agent_runs.check(state.agent)
        if code is not None:
            raise _Stop(code)

    # ── tuning (19 §9) ──────────────────────────────────────────────────

    @staticmethod
    async def _load_tuning(env: TaskEnvironment):
        if env.tuning is None:
            return None
        try:
            return await env.tuning.current()
        except Exception:  # noqa: BLE001 — unreadable tuning = the base config
            logger.warning("worker tuning could not be read; using the base configuration", exc_info=False)
            return None

    def _recovery_for(self, state: TaskState) -> RecoveryPolicy | None:
        """The recovery bounds for this task: the operator's, with any value a
        superuser approved (19 §9) in place of it. Only when recovery is
        configured at all — tuning never turns it on."""

        policy, tuning = self._recovery, state.tuning
        if policy is None or tuning is None:
            return policy
        overrides = {
            name: value for name in ("stall_window", "loop_repeat_limit", "max_worker_switches")
            if (value := getattr(tuning, name)) is not None
        }
        try:
            return replace(policy, **overrides) if overrides else policy
        except ValueError:
            return policy

    @staticmethod
    def _pending_resource(pending: PendingStep) -> str:
        if pending.kind == "capability_activation":
            return f"capability:{pending.capability}"
        return f"tool:{pending.tool_id}.{pending.operation}"

    # ── result shaping ──────────────────────────────────────────────────

    def _result_from_state(self, state: TaskState, status: AgentTaskStatus) -> AgentResult:
        pending = None
        if state.pending is not None and status is AgentTaskStatus.AWAITING_CONFIRMATION:
            p = state.pending
            pending = PendingAction(
                kind=p.kind, capability=p.capability, risk_category=p.risk_category,
                tool_id=p.tool_id, operation=p.operation, resource_ref=p.resource_ref,
                arguments=dict(p.arguments), resource_scope=p.resource_scope,
                requires_step_up=p.risk_category is RiskCategory.HIGH_IRREVERSIBLE,
                confirmation_token=p.token, expires_at=p.expires_at,
            )
        waiting_for = None
        if state.platform_wait is not None and status is AgentTaskStatus.WAITING_FOR_PLATFORM:
            w = state.platform_wait
            waiting_for = PlatformWait(dependency=w.dependency, device_id=w.device_id, expires_at=w.expires_at)
        return AgentResult(
            task_id=state.task_id, status=status, mode=state.mode, pending=pending, waiting_for=waiting_for,
            active_capabilities=state.active_capability_names(), notes=list(state.notes),
            counters=TaskCounters(iterations=state.iterations, model_calls=state.model_calls,
                                  tool_calls=state.tool_calls, worker_switches=state.worker_switches),
        )

    @staticmethod
    def _result_from_row(row) -> AgentResult:
        failure = None
        if row.failure_code:
            code = AgentFailureCode(row.failure_code)
            failure = AgentFailure(code=code, message=_FAILURE_MESSAGES[code])
        return AgentResult(
            task_id=row.task_id, status=AgentTaskStatus(row.status), mode=TaskMode(row.mode),
            response=row.response,
            failure=failure,
            counters=TaskCounters(iterations=row.iterations, model_calls=row.model_calls,
                                  tool_calls=row.tool_calls, worker_switches=row.worker_switches or 0),
        )
