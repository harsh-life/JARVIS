"""The StandingDelegation's policy — docs/29 §15 (Phase 5).

OD-AF-2/4/5/7/8, ratified 2026-10-02 (docs/DECISION_REGISTER.md §2K). A
standing delegation is the owner's step-up-confirmed permission for **one**
agent to run **unattended**, on exactly its compiled schedule, within exactly
its compiled envelope, under explicit budgets, run limits and an expiry. It is
a **ceiling, never a grant**: every unattended step still needs the owner's
live grant and the engine's decision, and an unattended run activates nothing
the owner has not already granted.

This module is pure — no store, no clock of its own, no engine. It answers
three questions from values its callers read fresh:

* `unattended_spec_refusal(spec)` — may this spec run unattended at all? Its
  trigger, its risk ceiling and every envelope operation must be inside the
  unattended ceiling (docs/29 §15.7, `shared…unattended_refusal`), checked
  against the capability registry's own tiers. The compiler asks it before a
  spec exists; the grant and every freshness check ask it again.
* `delegation_terms(spec, request, …)` — the terms a grant would write, all
  derived from the stored spec except the owner's chosen limits, which may
  only be tighter than the spec's.
* `delegation_refusal(facts, spec, …)` — is a stored delegation still exactly
  what was granted, for exactly this spec, now? Any mismatch is a refusal.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from server.agents.hashing import canonical_json
from server.capabilities.registry import CAPABILITY_REGISTRY
from shared.schemas.agent_factory import (
    UNATTENDED_RISK_CEILING,
    CompiledAgentSpec,
    DelegationRequest,
    DelegationStatus,
    OutputKind,
    TriggerKind,
    risk_severity,
    unattended_refusal,
)

MAX_RUNS_PER_DAY = 24


def envelope_hash(spec: CompiledAgentSpec) -> str:
    """SHA-256 of what the agent may ever attempt: its envelope, risk ceiling,
    mode, hydration, notebook, outputs and graph. Wording (name, purpose)
    does not change it; any change of authority does."""

    payload = {
        "envelope_ceiling": [e.model_dump(mode="json") for e in spec.envelope_ceiling],
        "risk_ceiling": spec.risk_ceiling.value,
        "run_mode": spec.run_mode.value,
        "hydration": spec.hydration.model_dump(mode="json"),
        "notebook_enabled": spec.notebook_enabled,
        "outputs": sorted(o.value for o in spec.outputs),
        "graph_id": str(spec.graph_id) if spec.graph_id is not None else None,
        "owner_user_id": str(spec.owner_user_id),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def unattended_spec_refusal(spec: CompiledAgentSpec) -> str | None:
    """`None` when the spec can run unattended (docs/29 §15.6–§15.7)."""

    trigger = spec.trigger
    if trigger.kind is not TriggerKind.UNATTENDED or not trigger.cron or not trigger.timezone:
        return "not_unattended"
    if tuple(spec.outputs) != (OutputKind.INBOX,):
        return "output_unavailable"
    if risk_severity(spec.risk_ceiling) > risk_severity(UNATTENDED_RISK_CEILING):
        return "above_unattended_ceiling"
    for entry in spec.envelope_ceiling:
        definition = CAPABILITY_REGISTRY.get(entry.capability)
        for operation in entry.operations:
            tier = definition.operations.get(operation) if definition is not None else None
            refused = unattended_refusal(entry.capability, operation, tier)
            if refused is not None:
                return refused
    return None


@dataclass(frozen=True)
class DelegationTerms:
    spec_version: int
    spec_hash: str
    envelope_hash: str
    cron: str
    timezone: str
    max_runs_per_day: int
    budget_per_run: float
    budget_per_month: float
    expires_at: datetime


def delegation_terms(spec: CompiledAgentSpec, request: DelegationRequest, *, now: datetime,
                     max_days: int) -> DelegationTerms | str:
    """What a grant writes, or why it cannot be granted. `now` is the grant's
    `created_at`; the expiry is mandatory and never later than `max_days`
    after it (OD-AF-5). Budgets are explicit, non-zero and within the spec's
    (OD-AF-7): a spec with no budget cannot be delegated."""

    refused = unattended_spec_refusal(spec)
    if refused is not None:
        return refused
    if spec.budget.per_run <= 0 or spec.budget.per_month <= 0:
        return "budget_required"
    if request.budget_per_run > spec.budget.per_run or request.budget_per_month > spec.budget.per_month \
            or request.budget_per_run > request.budget_per_month:
        return "budget_above_spec"
    days = request.expires_in_days if request.expires_in_days is not None else max_days
    if days > max_days:
        return "expiry_too_long"
    assert spec.trigger.cron is not None
    return DelegationTerms(
        spec_version=spec.version, spec_hash=spec.spec_hash, envelope_hash=envelope_hash(spec),
        cron=spec.trigger.cron, timezone=spec.trigger.timezone, max_runs_per_day=request.max_runs_per_day,
        budget_per_run=request.budget_per_run, budget_per_month=request.budget_per_month,
        expires_at=now + timedelta(days=days),
    )


@dataclass(frozen=True)
class DelegationFacts:
    """A stored delegation, as the policy sees it (`server.storage` rows are
    turned into this by the service)."""

    status: DelegationStatus
    agent_id: UUID
    owner_user_id: UUID
    graph_id: UUID | None
    spec_version: int
    spec_hash: str
    envelope_hash: str
    cron: str
    timezone: str
    max_runs_per_day: int
    budget_per_run: float
    budget_per_month: float
    expires_at: datetime
    created_with_step_up: bool


def delegation_refusal(facts: DelegationFacts, spec: CompiledAgentSpec | None, *, now: datetime) -> str | None:
    """docs/29 §15.2 freshness: `None` only if the delegation is active,
    unexpired, step-up-granted, and bound to exactly this spec — its version,
    hash, recomputed envelope hash, schedule, owner and graph — with budgets
    and limits that are still well-formed and within the spec's. Fail-closed:
    every other answer is the reason it no longer holds."""

    if facts.status is not DelegationStatus.ACTIVE:
        return "delegation_inactive"
    if not facts.created_with_step_up:
        return "no_step_up"
    if now >= facts.expires_at:
        return "delegation_expired"
    if spec is None:
        return "agent_unavailable"
    if facts.agent_id != spec.agent_id:
        return "agent_mismatch"
    if facts.owner_user_id != spec.owner_user_id:
        return "owner_mismatch"
    if facts.graph_id != spec.graph_id:
        return "graph_mismatch"
    if facts.spec_version != spec.version or facts.spec_hash != spec.spec_hash:
        return "spec_changed"
    if facts.envelope_hash != envelope_hash(spec):
        return "envelope_changed"
    if (facts.cron, facts.timezone) != (spec.trigger.cron, spec.trigger.timezone):
        return "trigger_changed"
    refused = unattended_spec_refusal(spec)
    if refused is not None:
        return refused
    if not 1 <= facts.max_runs_per_day <= MAX_RUNS_PER_DAY:
        return "run_limit_invalid"
    if facts.budget_per_run <= 0 or facts.budget_per_month <= 0:
        return "budget_required"
    if facts.budget_per_run > spec.budget.per_run or facts.budget_per_month > spec.budget.per_month:
        return "budget_above_spec"
    return None


__all__ = [
    "MAX_RUNS_PER_DAY",
    "DelegationFacts",
    "DelegationTerms",
    "delegation_refusal",
    "delegation_terms",
    "envelope_hash",
    "unattended_spec_refusal",
]
