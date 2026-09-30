"""docs/29 §4.1 — `agentdefinition` is an owner-private resource decided by
the one authorization engine (04), exactly like any other resource: only the
owner can read, update or delete it; anyone else gets `not_found` (anti-
enumeration, AGENT-T9); creating, updating and deleting are `consequential`
and pause for the owner's confirmation (docs/29 §23.1, OD-AF-3 proposed)."""

from __future__ import annotations

import uuid

import pytest

from server.capabilities.risk import resource_operation_tier
from server.composition.agents import AgentDefinitionLoader
from server.gateway.security import build_security_core
from server.graph.authorization import AccessRequest
from server.security.audit import AuditLogger
from server.storage.models import AgentDefinitionRow
from shared.schemas.authorization import DenialSurface, Operation, Principal, ResourceType
from shared.schemas.enums import PermissionDecisionValue, RiskCategory
from tests.agents.conftest import NOW, make_user
from tests.agents.test_registry import _config
from server.config.schema import AppConfig



def principal(user_id: uuid.UUID) -> Principal:
    return Principal(user_id=user_id, device_id=uuid.uuid4(), session_id=uuid.uuid4())


@pytest.fixture
def core():
    core = build_security_core(AppConfig.model_validate(_config()))
    core.resource_loader.register(ResourceType.AGENTDEFINITION, AgentDefinitionLoader())
    return core


async def _agent(storage, owner: uuid.UUID, *, status: str = "active") -> uuid.UUID:
    agent_id = uuid.uuid4()
    async with storage.session() as s:
        s.add(AgentDefinitionRow(agent_id=agent_id, owner_user_id=owner, name="a", status=status,
                                 current_version=1, created_at=NOW, updated_at=NOW))
        await s.commit()
    return agent_id


async def decide(core, storage, who, op, agent_id=None, token=None):
    async with storage.session() as s:
        outcome = await core.engine.authorize(s, AccessRequest(
            principal=who, operation=op, resource_type=ResourceType.AGENTDEFINITION,
            resource_ref=str(agent_id) if agent_id else None, confirmation_token=token,
        ), audit=AuditLogger(s, request_id=uuid.uuid4()))
        await s.commit()
    return outcome


def test_creating_updating_and_deleting_an_agent_are_consequential():
    for op in (Operation.CREATE, Operation.WRITE, Operation.DELETE, Operation.SHARE):
        assert resource_operation_tier(ResourceType.AGENTDEFINITION, op) is RiskCategory.CONSEQUENTIAL
    assert resource_operation_tier(ResourceType.AGENTDEFINITION, Operation.READ) is RiskCategory.LOW_READ


async def test_the_owner_reads_their_agent(core, storage):
    alice = await make_user(storage)
    agent = await _agent(storage, alice)
    outcome = await decide(core, storage, principal(alice), Operation.READ, agent)
    assert outcome.allowed


@pytest.mark.parametrize("op", [Operation.READ, Operation.WRITE, Operation.DELETE, Operation.SHARE])
async def test_agent_t9_another_user_sees_nothing(core, storage, op):
    alice, bob = await make_user(storage), await make_user(storage)
    agent = await _agent(storage, alice)
    outcome = await decide(core, storage, principal(bob), op, agent)
    assert outcome.decision is PermissionDecisionValue.DENY
    # Indistinguishable from an agent that does not exist.
    assert outcome.surface is DenialSurface.NOT_FOUND
    missing = await decide(core, storage, principal(bob), op, uuid.uuid4())
    assert missing.surface is DenialSurface.NOT_FOUND


@pytest.mark.parametrize("op", [Operation.WRITE, Operation.DELETE])
async def test_the_owners_changes_need_confirmation(core, storage, op):
    alice = await make_user(storage)
    agent = await _agent(storage, alice)
    outcome = await decide(core, storage, principal(alice), op, agent)
    assert outcome.needs_confirmation and outcome.risk_category is RiskCategory.CONSEQUENTIAL


async def test_creation_needs_confirmation(core, storage):
    alice = await make_user(storage)
    outcome = await decide(core, storage, principal(alice), Operation.CREATE)
    assert outcome.needs_confirmation


async def test_a_deleted_agent_is_not_found_even_for_its_owner(core, storage):
    alice = await make_user(storage)
    agent = await _agent(storage, alice, status="deleted")
    outcome = await decide(core, storage, principal(alice), Operation.READ, agent)
    assert outcome.surface is DenialSurface.NOT_FOUND


async def test_the_projection_is_private_and_owner_scoped(storage):
    alice = await make_user(storage)
    agent = await _agent(storage, alice)
    async with storage.session() as s:
        d = await AgentDefinitionLoader().load(s, ResourceType.AGENTDEFINITION, str(agent))
        assert (d.owner_user_id, d.visibility.value, d.resource_type) == (alice, "private", ResourceType.AGENTDEFINITION)
        assert await AgentDefinitionLoader().load(s, ResourceType.AGENTDEFINITION, "not-a-uuid") is None
        assert await AgentDefinitionLoader().load(s, ResourceType.MEM0FACT, str(agent)) is None
