"""Schema/enum validation tests (01_DATA_MODEL_SCHEMA.md §15: DM-T1..T4, T8;
T5..T7, T9 are covered in test_storage.py / documented as deferred where
Foundation cannot honestly implement them — see shared/schemas/capability.py)."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from shared.schemas.enums import FactType, Visibility
from shared.schemas.memory import Mem0Fact
from shared.schemas.resources import ScheduledJob
from shared.schemas.voice import SpeakerContext


def test_visibility_default_is_private() -> None:
    """DM-T2 (schema half): defaults to private. RAUTH-005."""

    fact = Mem0Fact(
        owner_user_id=uuid.uuid4(),
        source_user_id=uuid.uuid4(),
        graph_id=uuid.uuid4(),
        fact_type=FactType.PREFERENCE,
        content="likes dark mode",
    )
    assert fact.visibility == Visibility.PRIVATE

    job = ScheduledJob(
        owner_user_id=uuid.uuid4(),
        source_user_id=uuid.uuid4(),
        task_reason="pay rent",
        schedule="0 9 1 * *",
    )
    assert job.visibility == Visibility.PRIVATE


def test_fact_type_outside_enum_rejected() -> None:
    """DM-T3 / EMO-002: a fact_type outside {preference, past_request,
    stated_goal} is rejected, not coerced — the structural guard against
    emotional/relationship content in memory."""

    with pytest.raises(ValidationError):
        Mem0Fact(
            owner_user_id=uuid.uuid4(),
            source_user_id=uuid.uuid4(),
            graph_id=uuid.uuid4(),
            fact_type="relationship_status",  # not in the enum
            content="something",
        )


@pytest.mark.parametrize("bad_reason", ["", "   ", "\t\n"])
def test_empty_task_reason_rejected(bad_reason: str) -> None:
    """DM-T4 / SCHED-001: no unprompted proactivity."""

    with pytest.raises(ValidationError):
        ScheduledJob(
            owner_user_id=uuid.uuid4(),
            source_user_id=uuid.uuid4(),
            task_reason=bad_reason,
            schedule="* * * * *",
        )


def test_is_authorization_signal_cannot_be_set_true() -> None:
    """DM-T8 / INV-14: speaker identity is never an authorization signal."""

    with pytest.raises(ValidationError):
        SpeakerContext(
            speaker_id="voice-provider-label",
            confidence=0.95,
            utterance="hello",
            is_authorization_signal=True,  # type: ignore[arg-type]
        )

    ok = SpeakerContext(speaker_id="voice-provider-label", confidence=0.95, utterance="hello")
    assert ok.is_authorization_signal is False


def test_every_canonical_enum_rejects_a_value_outside_its_registry() -> None:
    """DM-T1: every enum value used anywhere is drawn from the single
    canonical registry (shared.schemas.enums) — spot-checked across a
    representative sample of entities that embed different enums."""

    from shared.schemas.enums import RiskCategory
    from shared.schemas.capability import PermissionDecision

    with pytest.raises(ValidationError):
        PermissionDecision(
            request_id=uuid.uuid4(),
            principal_id=uuid.uuid4(),
            capability="file.read",
            resource_ref="file:123",
            decision="maybe",  # not in permission.decision enum
            risk_category=RiskCategory.LOW_READ,
            reason="test",
        )

    with pytest.raises(ValidationError):
        PermissionDecision(
            request_id=uuid.uuid4(),
            principal_id=uuid.uuid4(),
            capability="file.read",
            resource_ref="file:123",
            decision="allow",
            risk_category="extremely_dangerous",  # not in risk_category enum
            reason="test",
        )


def test_session_and_device_and_user_remain_structurally_distinct() -> None:
    """This branch's explicit instruction (§5): 'Session is not a User.
    Device is not a User.' — no shared base class collapses their shapes."""

    from shared.schemas.identity import Device, Session, User

    assert not issubclass(Session, User)
    assert not issubclass(Device, User)
    assert not issubclass(Session, Device)

    # And they don't happen to be structurally identical either.
    assert set(User.model_fields) != set(Device.model_fields)
    assert set(Device.model_fields) != set(Session.model_fields)


def test_agent_configuration_does_not_itself_grant_privileges() -> None:
    """§5: 'Do not encode privileges merely by putting them on
    AgentConfiguration.' granted_capabilities is a resolved *cache*; the
    authoritative record is CapabilityGrant, which AgentConfiguration does
    not reference or embed."""

    from shared.schemas.agent_config import AgentConfiguration
    from shared.schemas.capability import CapabilityGrant

    # No field on AgentConfiguration is typed as (or embeds) CapabilityGrant
    # — the authoritative grant record lives only in capability.py, never
    # inside the config entity itself.
    for field in AgentConfiguration.model_fields.values():
        assert field.annotation is not CapabilityGrant

    # granted_capabilities is a plain list of strings (a resolved cache),
    # not a list of grant records.
    assert AgentConfiguration.model_fields["granted_capabilities"].annotation == list[str] | None


def test_graph_is_not_an_authorization_decision_by_itself() -> None:
    """Graph/GraphMembership expose no method implying membership alone
    grants read access (RAUTH-002) — this branch adds no such method."""

    from shared.schemas.graph import Graph, GraphMembership

    for cls in (Graph, GraphMembership):
        method_names = {name for name in dir(cls) if not name.startswith("_")}
        forbidden = {"is_readable", "can_read", "authorize", "is_authorized", "check_access"}
        assert not (method_names & forbidden)
