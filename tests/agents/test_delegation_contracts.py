"""docs/29 §15 — Phase 5 slice 5A: the StandingDelegation's contracts.

OD-AF-2/4/5/7/8 (DECISION_REGISTER §2K, 2026-10-02). What this slice proves,
before anything runs unattended:

* the feature is off by default and can be switched on only by the operator's
  explicit, ratified configuration;
* the unattended ceiling (docs/29 §15.7) is one pure, fail-closed predicate:
  ≤ low_write, server platform only, no device/app/system.restricted/agent.*,
  `net.request` `get` only;
* the compiler refuses an unattended agent that could reach past it, and a
  template cannot even claim unattended support above it;
* a delegation's terms are derived from the stored spec and the owner's
  explicit, non-zero budgets — bounded by the spec, mandatory expiry within
  `delegation_max_days` — and any mismatch later is a refusal (freshness);
* the store keeps the structural rules: one active delegation per agent,
  step-up mandatory, a task is either a present user's or a delegation's.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from server.agents.delegation import (
    DelegationFacts,
    delegation_refusal,
    delegation_terms,
    envelope_hash,
    unattended_spec_refusal,
)
from server.agents.errors import AgentRegistryError
from server.agents.registry.templates import load_templates
from server.config.schema import AgentsConfig, AppConfig
from server.storage.models import (
    AgentDefinitionRow,
    AgentInboxItemRow,
    AgentRunRow,
    AgentTask,
    StandingDelegationRow,
)
from shared.schemas.agent_factory import (
    AgentInboxItemView,
    AgentNotice,
    AgentRunView,
    CompiledTrigger,
    DelegationRequest,
    DelegationStatus,
    EnvelopeEntry,
    TriggerKind,
    unattended_refusal,
)
from shared.schemas.agent import TaskMode
from shared.schemas.enums import RiskCategory
from tests.agents.conftest import make_user
from tests.agents.test_compiler import compile_, compiled, draft
from tests.agents.test_registry import _config, _copy_template

NOW = datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)
_FIXED = uuid.UUID("99999999-9999-9999-9999-999999999999")
UNATTENDED = {"kind": "unattended", "cron": "0 7 * * *", "timezone": "Asia/Kolkata"}


# ── configuration: off by default, on only by the operator ────────────────


def test_unattended_is_off_by_default():
    agents = AppConfig.model_validate(_config()).agents
    assert (agents.unattended_enabled, agents.standing_delegation_ratified) == (False, False)
    assert agents.delegation_max_days == 30  # OD-AF-5


@pytest.mark.parametrize("agents", [
    {"unattended_enabled": True},                                          # not ratified
    {"unattended_enabled": True, "standing_delegation_ratified": True},    # factory off
    {"enabled": True, "unattended_enabled": True},                         # not ratified
    {"misfire_grace_minutes": 0},
    {"trigger_interval_seconds": 1},
    {"delegation_max_days": 0},
])
def test_unattended_switches_on_only_when_ratified_and_the_factory_is_on(agents):
    with pytest.raises(ValidationError):
        AppConfig.model_validate(_config(**agents))


def test_the_ratified_operator_configuration_loads():
    agents = AgentsConfig(enabled=True, unattended_enabled=True, standing_delegation_ratified=True)
    assert agents.unattended_enabled and agents.misfire_grace_minutes >= 1


# ── the unattended ceiling (docs/29 §15.7) ────────────────────────────────


@pytest.mark.parametrize("capability,operation,tier", [
    ("net.request", "get", RiskCategory.LOW_READ),
    ("file.read", "read", RiskCategory.LOW_READ),
    ("file.write", "write", RiskCategory.LOW_WRITE),
    ("model.invoke", "invoke", RiskCategory.LOW_READ),
])
def test_low_risk_server_work_is_inside_the_ceiling(capability, operation, tier):
    assert unattended_refusal(capability, operation, tier) is None


@pytest.mark.parametrize("capability,operation,tier,platform", [
    ("file.write", "delete", RiskCategory.CONSEQUENTIAL, "server"),          # consequential
    ("file.write", "write", RiskCategory.HIGH_IRREVERSIBLE, "server"),       # high_irreversible
    ("file.read", "read", None, "server"),                                    # unknown tier
    ("net.request", "post", RiskCategory.LOW_READ, "server"),                 # POST, whatever its tier says
    ("net.request", "put", RiskCategory.LOW_WRITE, "server"),
    ("device.read", "read_notifications", RiskCategory.LOW_READ, "server"),  # device execution
    ("app.interact", "open", RiskCategory.LOW_READ, "server"),
    ("device.ui_control", "tap", RiskCategory.LOW_WRITE, "server"),
    ("system.restricted", "run", RiskCategory.LOW_READ, "server"),
    ("agent.define", "create", RiskCategory.LOW_READ, "server"),             # agent creation
    ("agent.delegate", "grant_standing", RiskCategory.LOW_READ, "server"),
    ("file.read", "read", RiskCategory.LOW_READ, "android"),                  # not the server platform
    ("Device.Read", "read", RiskCategory.LOW_READ, "server"),                 # case games
    (" agent.inspect", "list", RiskCategory.LOW_READ, "server"),
])
def test_everything_else_is_outside_the_ceiling(capability, operation, tier, platform):
    assert unattended_refusal(capability, operation, tier, platform=platform) is not None


# ── templates and the compiler ────────────────────────────────────────────


@pytest.mark.parametrize("old,new", [
    # claims unattended support without listing the trigger, or the reverse
    ("trigger_support: [on_demand, reminder]", "trigger_support: [on_demand, reminder, unattended]"),
])
def test_a_template_cannot_claim_unattended_above_low_write(tmp_path, old, new):
    # file_organizer is low_write but unattended_supported: false — listing
    # the trigger without the flag is inconsistent and stops startup.
    with pytest.raises(AgentRegistryError):
        load_templates(_copy_template(tmp_path, "file_organizer", **{old: new}))


def test_a_template_above_low_write_cannot_claim_unattended_even_consistently(tmp_path):
    with pytest.raises(AgentRegistryError, match="capped"):
        load_templates(_copy_template(tmp_path, "file_organizer", **{
            "trigger_support: [on_demand, reminder]": "trigger_support: [on_demand, reminder, unattended]",
            "unattended_supported: false": "unattended_supported: true",
            "risk_ceiling: low_write": "risk_ceiling: consequential",
        }))


def test_the_compiler_refuses_unattended_above_the_ceiling_whatever_the_registry_let_through():
    # Defence in depth: a template that somehow skipped load validation (an
    # unvalidated copy) still cannot yield an unattended spec above low_write.
    import dataclasses
    from tests.agents.support import registries
    reg = registries()
    wide = reg.enabled_templates["file_organizer"].model_copy(update={
        "unattended_supported": True, "risk_ceiling": RiskCategory.CONSEQUENTIAL,
        "trigger_support": (TriggerKind.ON_DEMAND, TriggerKind.REMINDER, TriggerKind.UNATTENDED)})
    reg = dataclasses.replace(reg, enabled_templates={**reg.enabled_templates, "file_organizer": wide})
    d = dict(task_tags=["document_processing"], requested_abilities=["read_sandbox_files", "write_sandbox_files"],
             sources=[{"kind": "sandbox_path", "value": "inbox"}])
    assert compiled(compile_(draft(**d), reg=reg)).template_id == "file_organizer"
    result = compile_(draft(**d, trigger_request=UNATTENDED), reg=reg, unattended_available=True)
    assert result.kind == "rejected" and "above_unattended_ceiling" in result.reason_codes


def test_a_template_flagged_unattended_must_list_the_trigger(tmp_path):
    with pytest.raises(AgentRegistryError):
        load_templates(_copy_template(tmp_path, "research_digest", **{
            "trigger_support: [on_demand, reminder, unattended]": "trigger_support: [on_demand, reminder]"}))


def test_the_shipped_unattended_templates_are_read_only():
    templates = load_templates()
    for template in templates.values():
        if template.unattended_supported:
            assert TriggerKind.UNATTENDED in template.trigger_support
            assert template.risk_ceiling is RiskCategory.LOW_READ


def test_unattended_stays_unavailable_unless_switched_on():
    result = compile_(draft(trigger_request=UNATTENDED))
    assert result.kind == "rejected" and "unattended_unavailable" in result.reason_codes


def test_an_unattended_agent_compiles_on_an_unattended_template_when_available():
    spec = compiled(compile_(draft(trigger_request=UNATTENDED), unattended_available=True))
    assert (spec.trigger.kind, spec.trigger.cron, spec.trigger.timezone) == (
        TriggerKind.UNATTENDED, "0 7 * * *", "Asia/Kolkata")
    assert unattended_spec_refusal(spec) is None


def test_an_unattended_trigger_needs_an_exact_schedule():
    result = compile_(draft(trigger_request={"kind": "unattended", "schedule_text": "mornings"}),
                      unattended_available=True)
    assert result.kind == "needs_clarification"
    assert {"schedule_needed", "timezone_needed"} <= set(result.reason_codes)


def test_a_write_template_cannot_be_compiled_unattended():
    # file_organizer does not support the trigger: nothing is selected.
    d = dict(task_tags=["document_processing"], requested_abilities=["read_sandbox_files", "write_sandbox_files"],
             sources=[{"kind": "sandbox_path", "value": "inbox"}])
    assert compiled(compile_(draft(**d))).template_id == "file_organizer"   # on demand: fine
    result = compile_(draft(**d, trigger_request=UNATTENDED), unattended_available=True)
    assert result.kind != "compiled"


@pytest.mark.parametrize("change", [
    {"risk_ceiling": RiskCategory.CONSEQUENTIAL},
    {"envelope_ceiling": (EnvelopeEntry(capability="net.request", operations=("get", "post")),)},
    {"envelope_ceiling": (EnvelopeEntry(capability="device.read", operations=("read_notifications",)),)},
    {"envelope_ceiling": (EnvelopeEntry(capability="system.restricted", operations=("run",)),)},
    {"envelope_ceiling": (EnvelopeEntry(capability="file.write", operations=("delete",)),)},
    {"envelope_ceiling": (EnvelopeEntry(capability="nonexistent.thing", operations=("go",)),)},
    {"trigger": CompiledTrigger(kind=TriggerKind.REMINDER, cron="0 7 * * *", timezone="UTC")},
    {"trigger": CompiledTrigger(kind=TriggerKind.UNATTENDED, cron=None, timezone="UTC")},
    {"outputs": ()},
])
def test_a_spec_outside_the_unattended_ceiling_is_refused(change):
    spec = _unattended_spec()
    assert unattended_spec_refusal(spec.model_copy(update=change)) is not None


# ── delegation terms (docs/29 §15.3) ──────────────────────────────────────


def _unattended_spec():
    return compiled(compile_(draft(trigger_request=UNATTENDED), unattended_available=True))


def _request(**overrides) -> DelegationRequest:
    payload = {"max_runs_per_day": 2, "budget_per_run": 0.01, "budget_per_month": 0.2}
    payload.update(overrides)
    return DelegationRequest.model_validate(payload)


def _funded(spec):
    return spec.model_copy(update={"budget": spec.budget.model_copy(update={"per_run": 0.05, "per_month": 1.0})})


def test_terms_come_from_the_stored_spec_and_expire_by_default_at_the_maximum():
    spec = _funded(_unattended_spec())
    terms = delegation_terms(spec, _request(), now=NOW, max_days=30)
    assert not isinstance(terms, str), terms
    assert (terms.spec_version, terms.spec_hash, terms.envelope_hash) == (spec.version, spec.spec_hash,
                                                                         envelope_hash(spec))
    assert (terms.cron, terms.timezone) == (spec.trigger.cron, spec.trigger.timezone)
    assert terms.expires_at == NOW + timedelta(days=30)
    assert (terms.max_runs_per_day, terms.budget_per_run, terms.budget_per_month) == (2, 0.01, 0.2)


@pytest.mark.parametrize("overrides,max_days,reason", [
    ({"budget_per_run": 0.06}, 30, "budget_above_spec"),
    ({"budget_per_month": 1.5}, 30, "budget_above_spec"),
    ({"budget_per_run": 0.3, "budget_per_month": 0.2}, 30, "budget_above_spec"),
    ({"expires_in_days": 31}, 30, "expiry_too_long"),
    ({"expires_in_days": 8}, 7, "expiry_too_long"),
])
def test_terms_never_exceed_the_spec_or_the_lifetime(overrides, max_days, reason):
    assert delegation_terms(_funded(_unattended_spec()), _request(**overrides), now=NOW,
                            max_days=max_days) == reason


def test_a_spec_without_budgets_cannot_be_delegated():
    spec = _unattended_spec()
    zero = spec.model_copy(update={"budget": spec.budget.model_copy(update={"per_run": 0.0, "per_month": 0.0})})
    assert delegation_terms(zero, _request(), now=NOW, max_days=30) == "budget_required"


@pytest.mark.parametrize("payload", [
    {"max_runs_per_day": 0, "budget_per_run": 0.01, "budget_per_month": 0.2},
    {"max_runs_per_day": 25, "budget_per_run": 0.01, "budget_per_month": 0.2},
    {"max_runs_per_day": 1, "budget_per_run": 0.0, "budget_per_month": 0.2},
    {"max_runs_per_day": 1, "budget_per_run": 0.01, "budget_per_month": 0.0},
    {"max_runs_per_day": 1, "budget_per_run": 0.01, "budget_per_month": 0.2, "expires_in_days": 0},
    # authority and identity are never the request's to name
    {"max_runs_per_day": 1, "budget_per_run": 0.01, "budget_per_month": 0.2, "agent_id": str(_FIXED)},
    {"max_runs_per_day": 1, "budget_per_run": 0.01, "budget_per_month": 0.2, "capabilities": ["file.write"]},
    {"max_runs_per_day": 1, "budget_per_run": 0.01, "budget_per_month": 0.2, "cron": "* * * * *"},
    {"max_runs_per_day": 1, "budget_per_run": 0.01, "budget_per_month": 0.2, "device_id": str(_FIXED)},
    {"max_runs_per_day": 1, "budget_per_run": 0.01},                       # budgets are explicit
])
def test_a_delegation_request_is_strict(payload):
    with pytest.raises(ValidationError):
        DelegationRequest.model_validate(payload)


def test_the_envelope_hash_follows_authority_not_wording():
    spec = _unattended_spec()
    assert envelope_hash(spec) == envelope_hash(spec.model_copy(update={"name": "Another name"}))
    for change in ({"risk_ceiling": RiskCategory.LOW_WRITE},
                   {"envelope_ceiling": (EnvelopeEntry(capability="file.read", operations=("read",)),)},
                   {"run_mode": TaskMode.EXECUTE}):
        assert envelope_hash(spec.model_copy(update=change)) != envelope_hash(spec)


# ── freshness (docs/29 §15.2): any mismatch refuses ───────────────────────


def _facts(spec, **overrides) -> DelegationFacts:
    terms = delegation_terms(_funded(spec), _request(), now=NOW, max_days=30)
    assert not isinstance(terms, str)
    base = dict(
        status=DelegationStatus.ACTIVE, agent_id=spec.agent_id, owner_user_id=spec.owner_user_id,
        graph_id=spec.graph_id, spec_version=terms.spec_version, spec_hash=terms.spec_hash,
        envelope_hash=terms.envelope_hash, cron=terms.cron, timezone=terms.timezone,
        max_runs_per_day=terms.max_runs_per_day, budget_per_run=terms.budget_per_run,
        budget_per_month=terms.budget_per_month, expires_at=terms.expires_at, created_with_step_up=True,
    )
    base.update(overrides)
    return DelegationFacts(**base)


def test_a_fresh_delegation_on_its_own_spec_is_valid():
    spec = _funded(_unattended_spec())
    assert delegation_refusal(_facts(spec), spec, now=NOW + timedelta(days=1)) is None


@pytest.mark.parametrize("overrides,reason", [
    ({"status": DelegationStatus.REVOKED}, "delegation_inactive"),
    ({"status": DelegationStatus.EXPIRED}, "delegation_inactive"),
    ({"status": DelegationStatus.INVALIDATED}, "delegation_inactive"),
    ({"expires_at": NOW}, "delegation_expired"),
    ({"spec_hash": "0" * 64}, "spec_changed"),
    ({"spec_version": 2}, "spec_changed"),
    ({"envelope_hash": "f" * 64}, "envelope_changed"),
    ({"cron": "0 8 * * *"}, "trigger_changed"),
    ({"timezone": "UTC"}, "trigger_changed"),
    ({"agent_id": _FIXED}, "agent_mismatch"),
    ({"owner_user_id": _FIXED}, "owner_mismatch"),
    ({"graph_id": _FIXED}, "graph_mismatch"),
    ({"created_with_step_up": False}, "no_step_up"),
    ({"budget_per_run": 0.0}, "budget_required"),
    ({"budget_per_month": 0.0}, "budget_required"),
    ({"budget_per_run": 0.06}, "budget_above_spec"),
    ({"max_runs_per_day": 0}, "run_limit_invalid"),
    ({"max_runs_per_day": 25}, "run_limit_invalid"),
])
def test_any_mismatch_refuses(overrides, reason):
    spec = _funded(_unattended_spec())
    assert delegation_refusal(_facts(spec, **overrides), spec, now=NOW + timedelta(days=29)) == reason


def test_a_spec_that_left_the_ceiling_refuses_even_with_matching_hashes():
    spec = _funded(_unattended_spec())
    wider = spec.model_copy(update={"risk_ceiling": RiskCategory.CONSEQUENTIAL})
    facts = _facts(spec, spec_hash=wider.spec_hash, envelope_hash=envelope_hash(wider))
    assert delegation_refusal(facts, wider, now=NOW) is not None


def test_no_spec_refuses():
    spec = _funded(_unattended_spec())
    assert delegation_refusal(_facts(spec), None, now=NOW) == "agent_unavailable"


# ── views ─────────────────────────────────────────────────────────────────


def test_runs_and_inbox_items_can_say_unattended_and_notice():
    assert "unattended" in AgentRunView.model_fields["kind"].annotation.__args__
    item = AgentInboxItemView(item_id=uuid.uuid4(), agent_id=uuid.uuid4(), agent_name="a", run_id=None,
                              status="notice", kind="notice", notice=AgentNotice.MISFIRE_COALESCED, body="",
                              created_at=NOW)
    assert item.notice is AgentNotice.MISFIRE_COALESCED and item.run_id is None


# ── the store ─────────────────────────────────────────────────────────────


async def _agent(storage, owner_id) -> uuid.UUID:
    agent_id = uuid.uuid4()
    async with storage.session() as s:
        s.add(AgentDefinitionRow(agent_id=agent_id, owner_user_id=owner_id, graph_id=None, name="a",
                                 status="active", current_version=1, created_at=NOW, updated_at=NOW))
        await s.commit()
    return agent_id


def _delegation(agent_id, owner_id, **overrides) -> StandingDelegationRow:
    base = dict(
        delegation_id=uuid.uuid4(), agent_id=agent_id, owner_user_id=owner_id, graph_id=None,
        spec_version=1, spec_hash="a" * 64, envelope_hash="b" * 64, allowed_trigger_cron="0 7 * * *",
        timezone="UTC", max_runs_per_day=1, budget_per_run=0.01, budget_per_month=0.1,
        created_at=NOW, expires_at=NOW + timedelta(days=30), created_with_step_up=True,
        created_by_device_id=uuid.uuid4(), created_by_session_id=uuid.uuid4(), status="active",
    )
    base.update(overrides)
    return StandingDelegationRow(**base)


async def _add(storage, *rows) -> None:
    async with storage.session() as s:
        for row in rows:
            s.add(row)
        await s.commit()


async def test_one_active_delegation_per_agent(storage):
    owner_id = await make_user(storage)
    agent_id = await _agent(storage, owner_id)
    await _add(storage, _delegation(agent_id, owner_id, status="revoked"), _delegation(agent_id, owner_id))
    with pytest.raises(IntegrityError):
        await _add(storage, _delegation(agent_id, owner_id))


@pytest.mark.parametrize("overrides", [
    {"created_with_step_up": False},
    {"max_runs_per_day": 0},
    {"max_runs_per_day": 25},
    {"budget_per_run": 0.0},
    {"budget_per_month": 0.0},
    {"status": "paused"},
    {"expires_at": NOW},
])
async def test_the_store_refuses_a_malformed_delegation(storage, overrides):
    owner_id = await make_user(storage)
    agent_id = await _agent(storage, owner_id)
    with pytest.raises(IntegrityError):
        await _add(storage, _delegation(agent_id, owner_id, **overrides))


def _task(owner_id, **identity) -> AgentTask:
    return AgentTask(task_id=uuid.uuid4(), user_id=owner_id, graph_id=None, status="running", mode="observe",
                     iterations=0, model_calls=0, tool_calls=0, worker_switches=0, created_at=NOW,
                     updated_at=NOW, **identity)


async def test_a_delegated_task_has_a_delegation_and_no_device_or_session(storage):
    owner_id = await make_user(storage)
    agent_id = await _agent(storage, owner_id)
    delegation = _delegation(agent_id, owner_id)
    await _add(storage, delegation)
    await _add(storage, _task(owner_id, device_id=None, session_id=None, delegation_id=delegation.delegation_id))


@pytest.mark.parametrize("identity", ["neither", "device_and_delegation", "session_only"])
async def test_a_task_is_either_a_present_users_or_a_delegations(storage, identity):
    owner_id = await make_user(storage)
    agent_id = await _agent(storage, owner_id)
    delegation = _delegation(agent_id, owner_id)
    await _add(storage, delegation)
    fields = {
        "neither": dict(device_id=None, session_id=None, delegation_id=None),
        "device_and_delegation": dict(device_id=None, session_id=uuid.uuid4(),
                                      delegation_id=delegation.delegation_id),
        "session_only": dict(device_id=None, session_id=uuid.uuid4(), delegation_id=None),
    }[identity]
    with pytest.raises(IntegrityError):
        await _add(storage, _task(owner_id, **fields))


async def test_an_unattended_run_is_one_per_occurrence(storage):
    owner_id = await make_user(storage)
    agent_id = await _agent(storage, owner_id)
    delegation = _delegation(agent_id, owner_id)
    await _add(storage, delegation)
    occurrence = NOW + timedelta(hours=1)

    def run():
        return AgentRunRow(run_id=uuid.uuid4(), agent_id=agent_id, owner_user_id=owner_id, version=1,
                           spec_hash="a" * 64, kind="unattended", delegation_id=delegation.delegation_id,
                           occurrence_at=occurrence, status="queued", cost_total=0.0, started_at=NOW)

    await _add(storage, run())
    with pytest.raises(IntegrityError):
        await _add(storage, run())


@pytest.mark.parametrize("fields", [
    dict(kind="unattended", delegation_id=None, occurrence_at=None),        # unattended needs both
    dict(kind="on_demand", delegation_id="set", occurrence_at=NOW),         # a present-user run has none
])
async def test_an_unattended_run_and_only_one_names_a_delegation(storage, fields):
    owner_id = await make_user(storage)
    agent_id = await _agent(storage, owner_id)
    delegation = _delegation(agent_id, owner_id)
    await _add(storage, delegation)
    fields = dict(fields)
    if fields["delegation_id"] == "set":
        fields["delegation_id"] = delegation.delegation_id
    with pytest.raises(IntegrityError):
        await _add(storage, AgentRunRow(run_id=uuid.uuid4(), agent_id=agent_id, owner_user_id=owner_id,
                                        version=1, spec_hash="a" * 64, status="queued", cost_total=0.0,
                                        started_at=NOW, **fields))


async def test_a_notice_is_an_inbox_item_without_a_run(storage):
    owner_id = await make_user(storage)
    agent_id = await _agent(storage, owner_id)
    delegation = _delegation(agent_id, owner_id)
    await _add(storage, delegation)
    await _add(storage, AgentInboxItemRow(
        item_id=uuid.uuid4(), owner_user_id=owner_id, agent_id=agent_id, run_id=None, kind="notice",
        notice="misfire_coalesced", delegation_id=delegation.delegation_id, status="notice", body="",
        created_at=NOW))
    with pytest.raises(IntegrityError):
        # a result always names its run
        await _add(storage, AgentInboxItemRow(
            item_id=uuid.uuid4(), owner_user_id=owner_id, agent_id=agent_id, run_id=None, kind="result",
            status="completed", body="x", created_at=NOW))
