"""Central intelligence runs: E0 (the production prompt) and E1 (the Central
contract, v1 or v2), plus Central synthesis over specialist contributions (E3/E4).

A Central run is one or more metered model calls. It never acts: a proposal to
use a tool is recorded (`action_proposed`) and answered with the refusal the
production runtime would give a task that holds no capability, and nothing
runs. (That refusal is an emulation and is *not* what production says: see
docs/31 §9.4.)

Phase 0.1: an outcome carries what the response finalizer needs and nothing
it could mistake for authority. `model_text` is the model's contribution for
the user, with the v1 renderer's "Not done (needs your confirmation in the
app)" line left out: that line asserted a confirmation flow the harness does
not have, and the action's status is now the finalizer's to state, from the
ledger. `text` keeps the Phase 0 rendering unchanged, so rubric-1 quality
stays comparable with the Phase 0 runs.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from server.models.provider import ChatMessage, ModelUnavailable

from experiments.accounting import BudgetRefused, MeteredModel
from experiments.contracts import ResultBundle
from experiments.parsing import Unparseable, extract_object, is_placeholder, parse_baseline
from experiments.prompts import BASELINE_RETRY, baseline_messages, baseline_observation, central_contract_messages
from experiments.tasks import Task

NO_CAPABILITY = ("The request was refused: this task holds no capability and no tool is available. "
                 "Give your final answer.")


@dataclass
class CentralOutcome:
    task_id: str
    experiment: str
    status: str                      # completed | unparseable | failed | timed_out | budget_refused
    text: str = ""
    structured: dict | None = None
    cited: list[str] | None = None
    action_proposed: bool = False
    proposals: list[str] = field(default_factory=list)
    model_steps: int = 0
    latency_ms: float = 0.0
    error: str | None = None
    # The last raw model output when it could not be parsed (truncated; the
    # trace writer redacts it), so a parse failure can be diagnosed.
    raw_unparsed: str | None = None
    # Phase 0.1. The last raw model output whatever happened (evidence), the
    # model's text for the user (finalizer input), the top-level keys of its
    # JSON reply (to detect invented state fields), and parse failures.
    raw_output: str | None = None
    model_text: str = ""
    model_fields: list[str] = field(default_factory=list)
    parse_failures: int = 0
    prompt_version: str = "1"


class CentralReply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intent: str = ""
    objective: str = ""
    constraints: list[str] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification_question: str | None = None
    answer: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    uncertainty: str | None = None
    declined_actions: list[str] = Field(default_factory=list)

    # Small models often write a list field as one string, or a text field as
    # a list. The *shape* is normalized; no content is invented or dropped.
    @field_validator("constraints", "evidence_refs", "contradictions", "declined_actions", mode="before")
    @classmethod
    def _listify(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, list):
            return [v if isinstance(v, str) else json.dumps(v) for v in value]
        return value

    @field_validator("intent", "objective", "answer", "clarification_question", "uncertainty", mode="before")
    @classmethod
    def _textify(cls, value):
        if isinstance(value, list):
            return "; ".join(v if isinstance(v, str) else json.dumps(v) for v in value)
        return value

    def user_facing(self) -> str:
        """What Central would show the user (the Phase 0 renderer, unchanged).
        Placeholder entries ("None", "No contradictions found") are not
        rendered as findings."""

        return self._render(with_declined=True)

    def model_text(self) -> str:
        """The model's contribution, for the finalizer: everything but the
        declined-actions line, whose status claim is not the model's to make."""

        return self._render(with_declined=False)

    def _render(self, *, with_declined: bool) -> str:
        contradictions = [c for c in self.contradictions if not is_placeholder(c)]
        declined = [d for d in self.declined_actions if not is_placeholder(d)]
        uncertainty = self.uncertainty if self.uncertainty and not is_placeholder(self.uncertainty) else None
        parts = []
        if self.needs_clarification and self.clarification_question:
            parts.append(self.clarification_question)
        if self.answer:
            parts.append(self.answer)
        if contradictions:
            parts.append("Conflicting evidence: " + "; ".join(contradictions))
        if uncertainty:
            parts.append("Uncertainty: " + uncertainty)
        if declined and with_declined:
            parts.append("Not done (needs your confirmation in the app): " + "; ".join(declined))
        return "\n".join(parts)


class CentralReplyV2(CentralReply):
    """v2: no `declined_actions`, and no field of any kind for a status.
    `action_requests` is what the user asked for, used only to score whether
    the model understood the request (protocol compliance, never authority)."""

    declined_actions: list[str] = Field(default_factory=list, exclude=True)
    action_requests: list[str] = Field(default_factory=list)

    @field_validator("action_requests", mode="before")
    @classmethod
    def _listify_requests(cls, value):
        return CentralReply._listify(value)

    def user_facing(self) -> str:
        return self._render(with_declined=False)


REPLY_MODELS: dict[str, type[CentralReply]] = {"1": CentralReply, "2": CentralReplyV2}


def _fail(outcome: CentralOutcome, exc: BaseException) -> CentralOutcome:
    if isinstance(exc, BudgetRefused):
        outcome.status, outcome.error = "budget_refused", "budget_refused"
    elif isinstance(exc, asyncio.TimeoutError):
        outcome.status, outcome.error = "timed_out", "timeout"
    else:
        outcome.status, outcome.error = "failed", str(exc)[:200]
    return outcome


async def run_baseline(task: Task, model: MeteredModel, *, timeout: float, max_tokens: int,
                       max_parse_retries: int = 2, max_steps: int = 4) -> CentralOutcome:
    """E0: the production prompt and parser rules, single model, no tools."""

    outcome = CentralOutcome(task_id=task.id, experiment="e0", status="unparseable")
    messages: list[ChatMessage] = baseline_messages(task.prompt, task.evidence)
    started = time.perf_counter()
    failures = 0
    try:
        while outcome.model_steps < max_steps:
            result = await model.call(messages, task_id=task.id, role="central", max_tokens=max_tokens,
                                      timeout=timeout)
            outcome.model_steps += 1
            outcome.raw_output = result.content[:4000]
            messages.append(ChatMessage("assistant", result.content))
            try:
                kind, content = parse_baseline(result.content)
            except Unparseable as exc:
                outcome.raw_unparsed = result.content[:2000]
                outcome.parse_failures += 1
                failures += 1
                if failures > max_parse_retries:
                    break
                messages.append(baseline_observation(BASELINE_RETRY.format(error=exc)))
                continue
            failures = 0
            if kind == "final_answer":
                outcome.status, outcome.text = "completed", content or ""
                outcome.model_text = outcome.text
                outcome.model_fields = sorted(extract_object(result.content))
                break
            outcome.action_proposed = True
            outcome.proposals.append(kind)
            messages.append(baseline_observation(NO_CAPABILITY))
    except (BudgetRefused, asyncio.TimeoutError, ModelUnavailable) as exc:
        _fail(outcome, exc)
    outcome.latency_ms = (time.perf_counter() - started) * 1000.0
    return outcome


async def run_contract(task: Task, model: MeteredModel, *, timeout: float, max_tokens: int,
                       contributions: Sequence[ResultBundle] = (), max_chars: int | None = None,
                       experiment: str = "e1", max_parse_retries: int = 1, version: str = "1",
                       record: str | None = None) -> CentralOutcome:
    """E1 (and synthesis for E3/E4): the Central contract. `record` is the
    harness-rendered ACTION RECORD (v2 only)."""

    outcome = CentralOutcome(task_id=task.id, experiment=experiment, status="unparseable", prompt_version=version)
    reply_model = REPLY_MODELS[version]
    messages = central_contract_messages(task.prompt, task.evidence, max_chars=max_chars, contributions=contributions,
                                         version=version, record=record if version != "1" else None)
    started = time.perf_counter()
    try:
        for attempt in range(max_parse_retries + 1):
            result = await model.call(messages, task_id=task.id, role="central", max_tokens=max_tokens,
                                      timeout=timeout)
            outcome.model_steps += 1
            outcome.raw_output = result.content[:4000]
            try:
                payload = extract_object(result.content)
                reply = reply_model.model_validate(payload)
            except (Unparseable, ValidationError) as exc:
                outcome.raw_unparsed = result.content[:2000]
                outcome.parse_failures += 1
                outcome.error = f"{type(exc).__name__}"
                messages = [*messages, ChatMessage("assistant", result.content),
                            ChatMessage("user", f"That was not one valid JSON object ({type(exc).__name__}). "
                                                "Reply with exactly one JSON object in the documented form.")]
                continue
            outcome.status, outcome.raw_unparsed, outcome.error = "completed", None, None
            outcome.structured = reply.model_dump()
            outcome.text = reply.user_facing()
            outcome.model_text = reply.model_text()
            outcome.model_fields = sorted(payload)
            outcome.cited = list(reply.evidence_refs)
            break
    except (BudgetRefused, asyncio.TimeoutError, ModelUnavailable) as exc:
        _fail(outcome, exc)
    outcome.latency_ms = (time.perf_counter() - started) * 1000.0
    return outcome


__all__ = ["REPLY_MODELS", "CentralOutcome", "CentralReply", "CentralReplyV2", "run_baseline", "run_contract"]
