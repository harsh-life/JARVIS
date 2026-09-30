"""docs/29 §4, §9.4, §14, §22 — persistence of owner-private definitions.

The service stores and loads; it never authorizes (the composition root asks
the engine before calling it). What it must guarantee on its own:
* a preview is single-use, owner-bound, task-bound and expiring;
* a definition exists only after approval, with an immutable spec version;
* an update is a new version, never an edit, and a stale preview is refused;
* a spec altered at rest is detected on load: the agent is revoked and its
  spec is never returned (AGENT-T33, M-AG14);
* deleting leaves a tombstone with no name and no spec (docs/29 §22.1).
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from server.agents.compiler import OwnerContext
from server.agents.service import AgentDefinitionService, PreviewRefused
from server.storage.models import AgentCompilePreviewRow, AgentDefinitionRow, AgentSpecVersionRow
from shared.schemas.agent_factory import AgentDraft, AgentStatus
from tests.agents.conftest import NOW, make_user
from tests.agents.support import registries

pytestmark = pytest.mark.asyncio


def draft(**overrides) -> AgentDraft:
    payload = {
        "name": "Advisory digest",
        "purpose": "Summarize critical advisories.",
        "task_tags": ["security_research"],
        "requested_abilities": ["read_web_allowlisted"],
    }
    payload.update(overrides)
    return AgentDraft.model_validate(payload)


def owner_ctx(user_id, *, count: int = 0, **overrides) -> OwnerContext:
    base = dict(
        owner_user_id=user_id, graph_id=None, owner_primary_model_ref="agent.primary",
        budget_per_run_policy=0.0, budget_per_month_policy=0.0, active_agent_count=count, max_agents_per_user=5,
        runtime_max_seconds=120.0, runtime_max_model_calls=16, runtime_max_tool_calls=24,
        url_allowed=lambda url: False,
    )
    base.update(overrides)
    return OwnerContext(**base)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(clock):
    return AgentDefinitionService(registries(), preview_ttl_minutes=15, clock=clock)


def health():
    return {"native": True}


def preview_fn(cron, tz, now):
    return now + timedelta(hours=1)


async def compile_(service, session, user_id, *, task_id=None, agent=None, d=None):
    count = await service.active_count(session, user_id)
    return await service.compile(
        session, draft=d or draft(), owner=owner_ctx(user_id, count=count), task_id=task_id, agent=agent,
        runtime_health=health(), trigger_preview=preview_fn,
    )


async def create(service, session, user_id, *, task_id=None):
    outcome = await compile_(service, session, user_id, task_id=task_id)
    assert outcome.kind == "compiled", outcome
    preview = await service.load_preview(session, compile_id=outcome.compile_id, owner_user_id=user_id,
                                         task_id=task_id, for_agent=None)
    definition, spec = await service.create_from_preview(session, preview, created_by_device_id=None,
                                                         created_from_task_id=task_id)
    await session.commit()
    return definition, spec


# ── previews ─────────────────────────────────────────────────────────────


async def test_a_compiled_draft_leaves_one_bound_expiring_preview(storage, service):
    alice = await make_user(storage)
    task = uuid.uuid4()
    async with storage.session() as s:
        outcome = await compile_(service, s, alice, task_id=task)
        await s.commit()
    assert outcome.kind == "compiled" and outcome.compile_id is not None
    assert outcome.expires_at == NOW + timedelta(minutes=15)
    assert outcome.spec_preview.card.title.startswith('Create agent "Advisory digest"')
    [row] = await _rows(storage, AgentCompilePreviewRow)
    assert (row.owner_user_id, row.task_id, row.base_version) == (alice, task, None)
    assert await _rows(storage, AgentDefinitionRow) == []


async def test_a_clarification_persists_nothing(storage, service):
    alice = await make_user(storage)
    async with storage.session() as s:
        outcome = await compile_(service, s, alice, d=draft(sources=[{"kind": "url", "value": "https://x.example"}]))
        await s.commit()
    assert outcome.kind == "needs_clarification" and outcome.compile_id is None
    assert await _rows(storage, AgentCompilePreviewRow) == []


@pytest.mark.parametrize("case", ["other_owner", "other_task", "no_task_given", "expired", "unknown"])
async def test_a_preview_is_owner_and_task_bound_and_expires(storage, service, clock, case):
    alice, bob = await make_user(storage), await make_user(storage)
    task = uuid.uuid4()
    async with storage.session() as s:
        outcome = await compile_(service, s, alice, task_id=task)
        await s.commit()
    who, which, compile_id = alice, task, outcome.compile_id
    if case == "other_owner":
        who = bob
    elif case == "other_task":
        which = uuid.uuid4()
    elif case == "no_task_given":
        which = None
    elif case == "expired":
        clock.now = NOW + timedelta(minutes=16)
    else:
        compile_id = uuid.uuid4()
    async with storage.session() as s:
        assert await service.load_preview(s, compile_id=compile_id, owner_user_id=who, task_id=which,
                                          for_agent=None) is None


async def test_a_preview_is_single_use(storage, service):
    alice = await make_user(storage)
    async with storage.session() as s:
        outcome = await compile_(service, s, alice)
        preview = await service.load_preview(s, compile_id=outcome.compile_id, owner_user_id=alice, task_id=None,
                                             for_agent=None)
        await service.create_from_preview(s, preview, created_by_device_id=None, created_from_task_id=None)
        await s.commit()
    async with storage.session() as s:
        assert await service.load_preview(s, compile_id=outcome.compile_id, owner_user_id=alice, task_id=None,
                                          for_agent=None) is None


# ── definitions and versions ─────────────────────────────────────────────


async def test_creation_stores_an_active_definition_and_its_immutable_spec(storage, service):
    alice = await make_user(storage)
    async with storage.session() as s:
        definition, spec = await create(service, s, alice)
    assert definition.status == AgentStatus.ACTIVE.value and definition.current_version == 1
    assert definition.owner_user_id == alice and definition.visibility.value == "private"
    [version] = await _rows(storage, AgentSpecVersionRow)
    assert (version.agent_id, version.version, version.spec_hash) == (definition.agent_id, 1, spec.spec_hash)
    assert await _rows(storage, AgentCompilePreviewRow) == []


async def test_an_update_is_a_new_version_and_a_stale_preview_is_refused(storage, service):
    alice = await make_user(storage)
    async with storage.session() as s:
        definition, _ = await create(service, s, alice)
    async with storage.session() as s:
        definition = await s.get(AgentDefinitionRow, definition.agent_id)
        first = await compile_(service, s, alice, agent=definition, d=draft(name="Digest v2"))
        second = await compile_(service, s, alice, agent=definition, d=draft(name="Digest v2b"))
        p1 = await service.load_preview(s, compile_id=first.compile_id, owner_user_id=alice, task_id=None,
                                        for_agent=definition.agent_id)
        _, spec = await service.update_from_preview(s, definition, p1)
        await s.commit()
    assert spec.version == 2 and spec.name == "Digest v2"
    async with storage.session() as s:
        definition = await s.get(AgentDefinitionRow, definition.agent_id)
        p2 = await service.load_preview(s, compile_id=second.compile_id, owner_user_id=alice, task_id=None,
                                        for_agent=definition.agent_id)
        with pytest.raises(PreviewRefused):
            await service.update_from_preview(s, definition, p2)
    versions = await _rows(storage, AgentSpecVersionRow)
    assert sorted(v.version for v in versions) == [1, 2]


async def test_a_preview_for_one_agent_cannot_update_another(storage, service):
    alice = await make_user(storage)
    async with storage.session() as s:
        a, _ = await create(service, s, alice)
        b, _ = await create(service, s, alice)
    async with storage.session() as s:
        a = await s.get(AgentDefinitionRow, a.agent_id)
        outcome = await compile_(service, s, alice, agent=a)
        assert await service.load_preview(s, compile_id=outcome.compile_id, owner_user_id=alice, task_id=None,
                                          for_agent=b.agent_id) is None


async def test_the_quota_counts_live_agents(storage, service):
    alice = await make_user(storage)
    async with storage.session() as s:
        for _ in range(5):
            await create(service, s, alice)
        outcome = await compile_(service, s, alice)
    assert outcome.kind == "rejected" and "agent_quota_reached" in outcome.reason_codes


async def test_listing_is_per_owner_and_hides_deleted(storage, service):
    alice, bob = await make_user(storage), await make_user(storage)
    async with storage.session() as s:
        a1, _ = await create(service, s, alice)
        a2, _ = await create(service, s, alice)
        await create(service, s, bob)
    async with storage.session() as s:
        await service.delete(s, await s.get(AgentDefinitionRow, a2.agent_id))
        await s.commit()
    async with storage.session() as s:
        listed = await service.list_for_owner(s, alice)
    assert [d.agent_id for d, _ in listed] == [a1.agent_id]


# ── AGENT-T33: tampering at rest ─────────────────────────────────────────


@pytest.mark.parametrize("tamper", ["widen_envelope", "swap_owner", "hash_column"])
async def test_agent_t33_a_spec_altered_at_rest_revokes_the_agent(storage, service, tamper):
    alice = await make_user(storage)
    async with storage.session() as s:
        definition, spec = await create(service, s, alice)
    async with storage.session() as s:
        row = await s.get(AgentSpecVersionRow, (definition.agent_id, 1))
        data = dict(row.spec_json)
        if tamper == "widen_envelope":
            data["envelope_ceiling"] = [{"capability": "net.request", "operations": ["get", "post"], "scope": {}}]
        elif tamper == "swap_owner":
            data["owner_user_id"] = str(uuid.uuid4())
        await s.execute(update(AgentSpecVersionRow).where(AgentSpecVersionRow.agent_id == definition.agent_id)
                        .values(spec_json=data, spec_hash="0" * 64 if tamper == "hash_column" else row.spec_hash))
        await s.commit()
    async with storage.session() as s:
        loaded = await service.load(s, definition.agent_id)
        await s.commit()
    assert loaded is not None
    head, current = loaded
    assert current is None
    assert head.status == AgentStatus.REVOKED.value and head.status_reason == "spec_tampered"


async def test_an_intact_spec_loads(storage, service):
    alice = await make_user(storage)
    async with storage.session() as s:
        definition, spec = await create(service, s, alice)
    async with storage.session() as s:
        head, current = await service.load(s, definition.agent_id)
    assert current == spec and head.status == AgentStatus.ACTIVE.value


# ── deletion (docs/29 §14.3, §22.1) ──────────────────────────────────────


async def test_deleting_leaves_a_nameless_tombstone_and_no_spec(storage, service, clock):
    alice = await make_user(storage)
    async with storage.session() as s:
        definition, _ = await create(service, s, alice)
        outcome = await compile_(service, s, alice, agent=await s.get(AgentDefinitionRow, definition.agent_id))
        await s.commit()
    clock.now = NOW + timedelta(minutes=1)
    async with storage.session() as s:
        await service.delete(s, await s.get(AgentDefinitionRow, definition.agent_id))
        await s.commit()
    [tomb] = await _rows(storage, AgentDefinitionRow)
    assert tomb.status == AgentStatus.DELETED.value and tomb.name is None
    assert tomb.deleted_at is not None
    assert await _rows(storage, AgentSpecVersionRow) == []
    # A pending update preview for the deleted agent is gone too.
    assert await _rows(storage, AgentCompilePreviewRow) == []
    async with storage.session() as s:
        assert await service.load(s, definition.agent_id) is None


async def test_nothing_in_a_stored_spec_is_a_credential(storage, service):
    alice = await make_user(storage)
    async with storage.session() as s:
        await create(service, s, alice)
    [row] = await _rows(storage, AgentSpecVersionRow)
    text = repr(row.spec_json)
    for word in ("secret_ref", "secretstore:", "api_key", "endpoint", "token", "password"):
        assert word not in text


async def _rows(storage, model):
    async with storage.session() as s:
        return list((await s.execute(select(model))).scalars().all())
