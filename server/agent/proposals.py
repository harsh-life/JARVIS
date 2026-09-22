"""Deterministic proposal parsing (05 §2 "Parse proposal").

The model's output is text. This module turns it into exactly one typed
proposal or rejects it. It is the only interpretation the runtime ever applies
to model output, and it is strict on purpose:

* **Three kinds only** — `final_answer`, `tool_call`, `request_capabilities`.
* **No authority fields.** None of the schemas has a place for a risk tier, a
  disposition, a "confirmed"/"approved" flag, a principal, a user id, or a
  graph id. `extra="forbid"` makes a proposal that *includes* one a parse
  failure rather than a silently ignored hint — so "the model downgraded the
  risk" or "the model said the user already approved" is not expressible
  (PERM-005, TL-T10, INV-2).
* **Bounded.** Oversized arguments or answers are rejected, not truncated into
  something the model did not say.

A malformed proposal gets a bounded number of re-prompts (05 §6); the retries
count against the iteration and model-call ceilings like any other step.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from shared.schemas.agent import ExecutionPlatform

MAX_ARGUMENTS_CHARS = 16_000
MAX_ANSWER_CHARS = 20_000
MAX_CAPABILITIES_PER_REQUEST = 8


class ProposalError(Exception):
    """The model output is not a valid proposal. The message is fed back to the
    model as an observation, so it describes the problem without echoing
    arbitrary model text."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FinalAnswer(_Strict):
    type: Literal["final_answer"]
    content: str = Field(min_length=1, max_length=MAX_ANSWER_CHARS)


class ToolCall(_Strict):
    type: Literal["tool_call"]
    tool: str = Field(min_length=1, max_length=128)
    operation: str = Field(min_length=1, max_length=64)
    arguments: dict = Field(default_factory=dict)
    resource_ref: str | None = Field(default=None, max_length=256)
    platform: ExecutionPlatform | None = None
    # Which activated narrowing to act under, when a capability was activated
    # more than once (e.g. for two apps). Must equal one exactly — it selects,
    # it never widens.
    scope: dict[str, str] | None = None

    @field_validator("arguments")
    @classmethod
    def _bounded(cls, value: dict) -> dict:
        if len(json.dumps(value, default=str)) > MAX_ARGUMENTS_CHARS:
            raise ValueError("arguments too large")
        return value


class CapabilityAsk(_Strict):
    capability: str = Field(min_length=1, max_length=128)
    resource_scope: dict[str, str] | None = None


class RequestCapabilities(_Strict):
    type: Literal["request_capabilities"]
    capabilities: list[CapabilityAsk] = Field(
        min_length=1, max_length=MAX_CAPABILITIES_PER_REQUEST
    )
    reason: str | None = Field(default=None, max_length=500)


Proposal = Annotated[
    Union[FinalAnswer, ToolCall, RequestCapabilities], Field(discriminator="type")
]
_PROPOSAL = TypeAdapter(Proposal)


def _extract_json(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end < start:
        raise ProposalError("no JSON object found")
    return stripped[start : end + 1]


def parse_proposal(text: str) -> FinalAnswer | ToolCall | RequestCapabilities:
    if not isinstance(text, str) or not text.strip():
        raise ProposalError("empty output")
    try:
        payload = json.loads(_extract_json(text))
    except json.JSONDecodeError:
        raise ProposalError("output is not valid JSON") from None
    if not isinstance(payload, dict):
        raise ProposalError("output must be a single JSON object")
    try:
        return _PROPOSAL.validate_python(payload)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in err["loc"]) or "type" for err in exc.errors()})
        raise ProposalError(f"invalid proposal fields: {', '.join(fields)[:200]}") from None
