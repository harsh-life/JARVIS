"""Owner-private agent definitions: previews, versions, lifecycle
(docs/29 §4, §9.4, §14, §22).

This service **stores and loads; it never authorizes**. Every public path to
it — the `agent.*` tool adapters and the `/agents` endpoints — asks the
authorization engine first (`server/composition/agents.py`), with the owner
and visibility dimensions of the `agentdefinition` resource. What the service
guarantees by itself:

* **No definition before approval.** A compile writes only a preview: bound
  to its owner and (when compiled inside a task) that task, single-use,
  expiring. A definition row and its first spec version are written together
  when an approved preview is consumed, so a rejected or expired approval
  leaves nothing behind. (docs/29 §14.1's `awaiting_confirmation` is the
  preview's role here; no definition row is ever parked in that state.)
* **Specs are immutable.** An update is a new version compiled against the
  current one; a preview compiled against a version that is no longer current
  is refused.
* **Tampering is detected on load** (docs/29 §9.6 rule 8, AGENT-T33): a stored
  spec whose hash does not recompute, or that no longer matches its head row,
  revokes the agent (`spec_tampered`) and is never returned.
* **Deletion leaves a tombstone** — ids and timestamps; the name is cleared
  and every spec version and pending preview purged (docs/29 §22.1).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Mapping

from pydantic import ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.agents.compiler import (
    CompileTarget,
    OwnerContext,
    TriggerPreview,
    compile_draft,
    verify_spec_hash,
)
from server.agents.registry import AgentRegistries
from server.agents.rendering import budget_line, can_lines, cannot_lines, spec_view, trigger_line
from server.storage.models import (
    AgentCompilePreviewRow,
    AgentDefinitionRow,
    AgentInboxItemRow,
    AgentNotebookEntryRow,
    AgentRunRow,
    AgentSpecVersionRow,
    AgentTask,
)
from shared.schemas.agent import AgentFailureCode, AgentResult, AgentTaskStatus
from shared.schemas.agent_factory import (
    AgentDraft,
    AgentRunStatus,
    AgentInboxItemView,
    AgentRunView,
    AgentStatus,
    NotebookEntryView,
    AgentView,
    CompiledAgentSpec,
    CompileOutcome,
    SpecBudget,
)
from shared.schemas.enums import Visibility

_RUN_STATUS = {
    AgentTaskStatus.COMPLETED: AgentRunStatus.COMPLETED,
    AgentTaskStatus.FAILED: AgentRunStatus.FAILED,
    AgentTaskStatus.CANCELLED: AgentRunStatus.CANCELLED,
}
_TERMINAL_TASK = frozenset(_RUN_STATUS)

# docs/29 §16.3: the notebook's bounds (`[IMPL]`: docs/29 fixes no numbers).
NOTEBOOK_MAX_ENTRIES = 200
NOTEBOOK_MAX_VALUE_CHARS = 8000
_NOTEBOOK_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Statuses that still count against the per-owner quota and can be updated.
_LIVE = (AgentStatus.ACTIVE.value, AgentStatus.PAUSED.value, AgentStatus.NEEDS_REAPPROVAL.value)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class PreviewRefused(Exception):
    """An approved preview cannot be applied (stale, tampered, already used,
    over quota). Nothing was written."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AgentDefinitionService:
    def __init__(
        self,
        registries: AgentRegistries,
        *,
        preview_ttl_minutes: int,
        max_agents_per_user: int = 5,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._registries = registries
        self._ttl = timedelta(minutes=preview_ttl_minutes)
        self._max_agents = max_agents_per_user
        self._clock = clock

    @property
    def registries(self) -> AgentRegistries:
        return self._registries

    def now(self) -> datetime:
        return self._clock()

    # ── compiling ───────────────────────────────────────────────────────

    async def active_count(self, session: AsyncSession, owner_user_id: uuid.UUID) -> int:
        return int((await session.execute(
            select(func.count()).select_from(AgentDefinitionRow).where(
                AgentDefinitionRow.owner_user_id == owner_user_id, AgentDefinitionRow.status.in_(_LIVE))
        )).scalar_one())

    async def compile(
        self,
        session: AsyncSession,
        *,
        draft: AgentDraft,
        owner: OwnerContext,
        task_id: uuid.UUID | None,
        agent: AgentDefinitionRow | None,
        runtime_health: Mapping[str, bool],
        trigger_preview: TriggerPreview,
    ) -> CompileOutcome:
        now = self._clock()
        await session.execute(delete(AgentCompilePreviewRow).where(AgentCompilePreviewRow.expires_at <= now))
        target = CompileTarget(
            agent_id=agent.agent_id if agent is not None else uuid.uuid4(),
            version=agent.current_version + 1 if agent is not None else 1,
            created_at=now,
        )
        result = compile_draft(draft, owner=owner, target=target, registries=self._registries,
                               runtime_health=runtime_health, trigger_preview=trigger_preview)
        if result.kind != "compiled" or result.spec is None:
            return CompileOutcome(kind=result.kind, questions=result.questions, reason_codes=result.reason_codes)
        spec = result.spec
        compile_id = uuid.uuid4()
        expires_at = now + self._ttl
        session.add(AgentCompilePreviewRow(
            compile_id=compile_id, owner_user_id=owner.owner_user_id, task_id=task_id, agent_id=spec.agent_id,
            base_version=agent.current_version if agent is not None else None,
            spec_hash=spec.spec_hash, spec_json=spec.model_dump(mode="json"), created_at=now, expires_at=expires_at,
        ))
        await session.flush()
        return CompileOutcome(kind="compiled", compile_id=compile_id, expires_at=expires_at,
                              spec_preview=spec_view(spec, self._registries))

    async def load_preview(
        self,
        session: AsyncSession,
        *,
        compile_id: uuid.UUID,
        owner_user_id: uuid.UUID,
        task_id: uuid.UUID | None,
        for_agent: uuid.UUID | None,
    ) -> AgentCompilePreviewRow | None:
        """The preview, if it is this owner's, compiled in this task (or both
        outside any task), unexpired, and for the intended target: a new agent
        (`for_agent=None`) or an update of exactly `for_agent`."""

        row = await session.get(AgentCompilePreviewRow, compile_id)
        if row is None or row.owner_user_id != owner_user_id or row.task_id != task_id:
            return None
        if _as_utc(row.expires_at) <= self._clock():
            return None
        if for_agent is None:
            return row if row.base_version is None else None
        return row if row.base_version is not None and row.agent_id == for_agent else None

    def preview_spec(self, preview: AgentCompilePreviewRow) -> CompiledAgentSpec:
        try:
            spec = CompiledAgentSpec.model_validate(preview.spec_json)
        except ValidationError:
            raise PreviewRefused("preview_invalid") from None
        if spec.spec_hash != preview.spec_hash or not verify_spec_hash(spec):
            raise PreviewRefused("preview_tampered")
        if spec.agent_id != preview.agent_id or spec.owner_user_id != preview.owner_user_id:
            raise PreviewRefused("preview_tampered")
        return spec

    async def create_from_preview(
        self,
        session: AsyncSession,
        preview: AgentCompilePreviewRow,
        *,
        created_by_device_id: uuid.UUID | None,
        created_from_task_id: uuid.UUID | None,
    ) -> tuple[AgentDefinitionRow, CompiledAgentSpec]:
        spec = self.preview_spec(preview)
        if preview.base_version is not None or spec.version != 1:
            raise PreviewRefused("not_a_creation")
        if await session.get(AgentDefinitionRow, spec.agent_id) is not None:
            raise PreviewRefused("already_created")
        # Re-checked at write time: two approvals racing past the compile-time check.
        if await self.active_count(session, spec.owner_user_id) >= self._max_agents:
            raise PreviewRefused("agent_quota_reached")
        now = self._clock()
        definition = AgentDefinitionRow(
            agent_id=spec.agent_id, owner_user_id=spec.owner_user_id, graph_id=spec.graph_id, name=spec.name,
            status=AgentStatus.ACTIVE.value, current_version=1, visibility=Visibility.PRIVATE,
            created_from_task_id=created_from_task_id, created_by_device_id=created_by_device_id,
            created_at=now, updated_at=now,
        )
        session.add(definition)
        await session.flush()
        session.add(AgentSpecVersionRow(agent_id=spec.agent_id, version=1, spec_hash=spec.spec_hash,
                                        spec_json=spec.model_dump(mode="json"), created_at=now))
        await session.delete(preview)
        await session.flush()
        return definition, spec

    async def update_from_preview(
        self, session: AsyncSession, definition: AgentDefinitionRow, preview: AgentCompilePreviewRow
    ) -> tuple[AgentDefinitionRow, CompiledAgentSpec]:
        spec = self.preview_spec(preview)
        if definition.status not in _LIVE:
            raise PreviewRefused("not_updatable")
        if preview.agent_id != definition.agent_id or preview.base_version != definition.current_version:
            raise PreviewRefused("stale_preview")
        if spec.version != definition.current_version + 1 or spec.owner_user_id != definition.owner_user_id:
            raise PreviewRefused("stale_preview")
        now = self._clock()
        session.add(AgentSpecVersionRow(agent_id=spec.agent_id, version=spec.version, spec_hash=spec.spec_hash,
                                        spec_json=spec.model_dump(mode="json"), created_at=now))
        definition.current_version = spec.version
        definition.name = spec.name
        definition.updated_at = now
        if definition.status == AgentStatus.NEEDS_REAPPROVAL.value:
            # The owner approved a recompiled version: that is the re-approval.
            definition.status = AgentStatus.ACTIVE.value
            definition.status_reason = None
        await session.delete(preview)
        await session.flush()
        return definition, spec

    # ── loading ─────────────────────────────────────────────────────────

    async def _current_spec(self, session: AsyncSession, definition: AgentDefinitionRow,
                            fresh: bool = False) -> CompiledAgentSpec | None:
        row = await session.get(AgentSpecVersionRow, (definition.agent_id, definition.current_version),
                                populate_existing=fresh)
        if row is None:
            return None
        try:
            spec = CompiledAgentSpec.model_validate(row.spec_json)
        except ValidationError:
            return None
        intact = (
            verify_spec_hash(spec)
            and spec.spec_hash == row.spec_hash
            and spec.agent_id == definition.agent_id
            and spec.version == definition.current_version
            and spec.owner_user_id == definition.owner_user_id
            and spec.graph_id == definition.graph_id
        )
        return spec if intact else None

    async def load(
        self, session: AsyncSession, agent_id: uuid.UUID, *, fresh: bool = False
    ) -> tuple[AgentDefinitionRow, CompiledAgentSpec | None] | None:
        """The head row and its verified current spec; `None` for an absent or
        deleted agent. A revoked agent, or one whose spec fails verification
        (which revokes it now), comes back with no spec.

        `fresh` re-reads both rows from the store even if this session holds
        them (sessions do not expire on commit): what a running agent's
        per-step re-validation needs, so a change made by any other request —
        a delete, a pause, a new version — is seen at the next step."""

        definition = await session.get(AgentDefinitionRow, agent_id, populate_existing=fresh)
        if definition is None or definition.status == AgentStatus.DELETED.value:
            return None
        if definition.status == AgentStatus.REVOKED.value:
            return definition, None
        spec = await self._current_spec(session, definition, fresh)
        if spec is None:
            definition.status = AgentStatus.REVOKED.value
            definition.status_reason = "spec_tampered"
            definition.updated_at = self._clock()
            await session.flush()
            return definition, None
        return definition, spec

    async def list_for_owner(
        self, session: AsyncSession, owner_user_id: uuid.UUID
    ) -> list[tuple[AgentDefinitionRow, CompiledAgentSpec | None]]:
        rows = (await session.execute(
            select(AgentDefinitionRow.agent_id).where(
                AgentDefinitionRow.owner_user_id == owner_user_id,
                AgentDefinitionRow.status != AgentStatus.DELETED.value,
            ).order_by(AgentDefinitionRow.created_at, AgentDefinitionRow.agent_id)
        )).scalars().all()
        loaded = []
        for agent_id in rows:
            item = await self.load(session, agent_id)
            if item is not None:
                loaded.append(item)
        return loaded

    # ── deleting ────────────────────────────────────────────────────────

    async def delete(self, session: AsyncSession, definition: AgentDefinitionRow) -> None:
        now = self._clock()
        await session.execute(delete(AgentSpecVersionRow).where(AgentSpecVersionRow.agent_id == definition.agent_id))
        await session.execute(delete(AgentCompilePreviewRow).where(AgentCompilePreviewRow.agent_id == definition.agent_id))
        definition.status = AgentStatus.DELETED.value
        definition.status_reason = None
        definition.name = None
        definition.deleted_at = now
        definition.updated_at = now
        # No run of a deleted agent stays open: a live one fails its next
        # step's re-validation, and its record says it was stopped.
        for run in await self.live_runs(session, definition.agent_id):
            await self.run_cancelled(session, run.run_id, reason="deleted")
        await self.purge_runtime_state(session, definition.agent_id)
        await session.flush()

    async def purge_runtime_state(self, session: AsyncSession, agent_id: uuid.UUID) -> None:
        """What JARVIS keeps for an agent at runtime, beyond its definition and
        run records: removed with the agent (docs/29 §14.4)."""

        await self.notebook_clear(session, agent_id)
        await session.execute(delete(AgentInboxItemRow).where(AgentInboxItemRow.agent_id == agent_id))
        await session.flush()

    # ── the inbox (docs/29 §19) ─────────────────────────────────────────

    async def inbox_deliver(self, session: AsyncSession, run: AgentRunRow, *, body: str, withheld: bool,
                            truncated: bool) -> AgentInboxItemRow | None:
        """One item per finished run, to the run's owner — there is no other
        recipient. Idempotent: a run already delivered is not delivered twice."""

        if run.finished_at is None:
            return None
        existing = (await session.execute(
            select(AgentInboxItemRow).where(AgentInboxItemRow.run_id == run.run_id))).scalars().first()
        if existing is not None:
            return existing
        item = AgentInboxItemRow(
            item_id=uuid.uuid4(), owner_user_id=run.owner_user_id, agent_id=run.agent_id, run_id=run.run_id,
            status=run.status, failure_code=run.failure_code,
            body=body if run.status == AgentRunStatus.COMPLETED.value else "",
            withheld=withheld, truncated=truncated, created_at=self._clock(),
        )
        session.add(item)
        await session.flush()
        return item

    async def inbox_list(self, session: AsyncSession, *, owner_user_id: uuid.UUID,
                         agent_id: uuid.UUID | None = None, unread: bool = False,
                         limit: int = 100) -> list[AgentInboxItemRow]:
        query = select(AgentInboxItemRow).where(AgentInboxItemRow.owner_user_id == owner_user_id)
        if agent_id is not None:
            query = query.where(AgentInboxItemRow.agent_id == agent_id)
        if unread:
            query = query.where(AgentInboxItemRow.read_at.is_(None))
        query = query.order_by(AgentInboxItemRow.created_at.desc(), AgentInboxItemRow.item_id).limit(limit)
        return list((await session.execute(query)).scalars().all())

    async def inbox_item(self, session: AsyncSession, item_id: uuid.UUID) -> AgentInboxItemRow | None:
        return await session.get(AgentInboxItemRow, item_id)

    async def inbox_mark_read(self, session: AsyncSession, item: AgentInboxItemRow) -> None:
        if item.read_at is None:
            item.read_at = self._clock()
            await session.flush()

    async def inbox_delete(self, session: AsyncSession, item: AgentInboxItemRow) -> None:
        await session.delete(item)
        await session.flush()

    async def inbox_item_for_run(self, session: AsyncSession, run_id: uuid.UUID) -> uuid.UUID | None:
        return (await session.execute(
            select(AgentInboxItemRow.item_id).where(AgentInboxItemRow.run_id == run_id))).scalars().first()

    async def inbox_view(self, session: AsyncSession, item: AgentInboxItemRow) -> AgentInboxItemView:
        definition = await session.get(AgentDefinitionRow, item.agent_id)
        return AgentInboxItemView(
            item_id=item.item_id, agent_id=item.agent_id,
            agent_name=definition.name if definition is not None else None, run_id=item.run_id,
            status=item.status, failure_code=item.failure_code, body=item.body, withheld=item.withheld,
            truncated=item.truncated, created_at=_as_utc(item.created_at),
            read_at=_as_utc(item.read_at) if item.read_at else None,
        )

    # ── the notebook (docs/29 §16.3) ────────────────────────────────────

    async def notebook_get(self, session: AsyncSession, agent_id: uuid.UUID, key: str) -> str | None:
        row = await session.get(AgentNotebookEntryRow, (agent_id, key))
        return row.value if row is not None else None

    async def notebook_keys(self, session: AsyncSession, agent_id: uuid.UUID) -> list[str]:
        return list((await session.execute(
            select(AgentNotebookEntryRow.key).where(AgentNotebookEntryRow.agent_id == agent_id)
            .order_by(AgentNotebookEntryRow.key)
        )).scalars().all())

    async def notebook_entries(self, session: AsyncSession, agent_id: uuid.UUID,
                               owner_user_id: uuid.UUID) -> list[AgentNotebookEntryRow]:
        return list((await session.execute(
            select(AgentNotebookEntryRow).where(AgentNotebookEntryRow.agent_id == agent_id,
                                                AgentNotebookEntryRow.owner_user_id == owner_user_id)
            .order_by(AgentNotebookEntryRow.key)
        )).scalars().all())

    async def notebook_put(self, session: AsyncSession, *, agent_id: uuid.UUID, owner_user_id: uuid.UUID,
                           run_id: uuid.UUID, key: str, value: str) -> str | None:
        """Store one note; `None`, or why it was refused. Bounded: a key is a
        short slug, a value at most `NOTEBOOK_MAX_VALUE_CHARS` printable
        characters, and at most `NOTEBOOK_MAX_ENTRIES` notes per agent (an
        existing note can always be replaced)."""

        if not _NOTEBOOK_KEY.fullmatch(key):
            return "invalid_key"
        if len(value) > NOTEBOOK_MAX_VALUE_CHARS:
            return "value_too_long"
        if _CONTROL.search(value):
            return "invalid_value"
        row = await session.get(AgentNotebookEntryRow, (agent_id, key))
        if row is None:
            count = (await session.execute(select(func.count()).select_from(AgentNotebookEntryRow)
                                           .where(AgentNotebookEntryRow.agent_id == agent_id))).scalar_one()
            if count >= NOTEBOOK_MAX_ENTRIES:
                return "notebook_full"
            row = AgentNotebookEntryRow(agent_id=agent_id, key=key, owner_user_id=owner_user_id, value=value,
                                        updated_by_run_id=run_id, updated_at=self._clock())
            session.add(row)
        else:
            if row.owner_user_id != owner_user_id:
                return "invalid_key"
            row.value, row.updated_by_run_id, row.updated_at = value, run_id, self._clock()
        await session.flush()
        return None

    @staticmethod
    def notebook_view(row: AgentNotebookEntryRow) -> NotebookEntryView:
        return NotebookEntryView(key=row.key, value=row.value, updated_at=_as_utc(row.updated_at))

    async def notebook_clear(self, session: AsyncSession, agent_id: uuid.UUID) -> None:
        await session.execute(delete(AgentNotebookEntryRow).where(AgentNotebookEntryRow.agent_id == agent_id))
        await session.flush()

    # ── pause / resume (docs/29 §14.1) ──────────────────────────────────

    async def pause(self, session: AsyncSession, definition: AgentDefinitionRow) -> bool:
        """Active → paused; anything else is left as it is. Returns whether
        the status changed. Pausing is always allowed: it only removes."""

        if definition.status != AgentStatus.ACTIVE.value:
            return False
        definition.status = AgentStatus.PAUSED.value
        definition.status_reason = "owner_paused"
        definition.updated_at = self._clock()
        await session.flush()
        return True

    async def resume(self, session: AsyncSession, definition: AgentDefinitionRow) -> None:
        """Paused → active. The caller has re-run every check a new run would
        face, and the owner's confirmation, before calling this."""

        if definition.status != AgentStatus.PAUSED.value:
            raise ValueError("only a paused agent can be resumed")
        definition.status = AgentStatus.ACTIVE.value
        definition.status_reason = None
        definition.updated_at = self._clock()
        await session.flush()

    # ── runs (docs/29 §14.2, §22.1) ─────────────────────────────────────

    async def mark_needs_reapproval(self, session: AsyncSession, definition: AgentDefinitionRow,
                                    reason: str) -> None:
        """docs/29 §5.3: the agent's template (or its model profile) moved on;
        it runs again only after the owner re-approves a recompile."""

        definition.status = AgentStatus.NEEDS_REAPPROVAL.value
        definition.status_reason = reason
        definition.updated_at = self._clock()
        await session.flush()

    async def check_run(self, session: AsyncSession, *, run_id: uuid.UUID, agent_id: uuid.UUID, version: int,
                        spec_hash: str,
                        is_member: Callable[[uuid.UUID, uuid.UUID], Awaitable[bool]]) -> AgentFailureCode | None:
        """May a run bound to (run, agent, version, hash) take its next step?
        Everything is read fresh from the store: `None`, or why not.
        `is_member(graph_id, user_id)` is the graph repository's live answer:
        an owner who has left the agent's graph stops its run (04 §9)."""

        run = await session.get(AgentRunRow, run_id, populate_existing=True)
        if run is None or run.agent_id != agent_id or run.finished_at is not None:
            return AgentFailureCode.AGENT_UNAVAILABLE
        loaded = await self.load(session, agent_id, fresh=True)
        if loaded is None:
            return AgentFailureCode.AGENT_UNAVAILABLE
        definition, spec = loaded
        if spec is None or definition.status != AgentStatus.ACTIVE.value:
            return AgentFailureCode.AGENT_UNAVAILABLE
        if definition.owner_user_id != run.owner_user_id:
            return AgentFailureCode.AGENT_UNAVAILABLE
        if definition.graph_id is not None and not await is_member(definition.graph_id, run.owner_user_id):
            return AgentFailureCode.AGENT_UNAVAILABLE
        if definition.current_version != version or spec.spec_hash != spec_hash:
            return AgentFailureCode.SPEC_CHANGED
        return None

    async def create_run(self, session: AsyncSession, *, spec: CompiledAgentSpec,
                         run_id: uuid.UUID) -> AgentRunRow:
        run = AgentRunRow(run_id=run_id, agent_id=spec.agent_id, owner_user_id=spec.owner_user_id,
                          version=spec.version, spec_hash=spec.spec_hash, kind="on_demand",
                          status=AgentRunStatus.QUEUED.value, cost_total=0.0, started_at=self._clock())
        session.add(run)
        await session.flush()
        return run

    async def run_started(self, session: AsyncSession, run_id: uuid.UUID, *, task_id: uuid.UUID) -> AgentRunRow | None:
        run = await session.get(AgentRunRow, run_id)
        if run is not None:
            run.task_id = task_id
            run.status = AgentRunStatus.RUNNING.value
            await session.flush()
        return run

    async def run_waiting(self, session: AsyncSession, run_id: uuid.UUID) -> None:
        run = await session.get(AgentRunRow, run_id)
        if run is not None and run.finished_at is None:
            run.status = AgentRunStatus.WAITING.value
            await session.flush()

    async def run_cancelled(self, session: AsyncSession, run_id: uuid.UUID, *, reason: str) -> AgentRunRow | None:
        run = await session.get(AgentRunRow, run_id, populate_existing=True)
        if run is None or run.finished_at is not None:
            return run
        return await self.run_finished(session, run_id, status=AgentTaskStatus.CANCELLED, failure=reason,
                                       cost=run.cost_total)

    async def run_finished(self, session: AsyncSession, run_id: uuid.UUID, *, status: AgentTaskStatus,
                           failure: str | None, cost: float) -> AgentRunRow | None:
        # Fresh: a stop recorded by another request is never overwritten.
        run = await session.get(AgentRunRow, run_id, populate_existing=True)
        if run is None or run.finished_at is not None:
            return run
        run.status = _RUN_STATUS.get(status, AgentRunStatus.FAILED).value
        run.failure_code = failure
        run.cost_total = max(0.0, float(cost))
        run.finished_at = self._clock()
        await session.flush()
        return run

    async def _synced(self, session: AsyncSession, run: AgentRunRow) -> AgentRunRow:
        """A run whose task ended without its live state (a restart, a cancel
        from the row) is closed from that task's outcome when read."""

        if run.finished_at is not None or run.task_id is None:
            return run
        task = await session.get(AgentTask, run.task_id, populate_existing=True)
        if task is not None and AgentTaskStatus(task.status) in _TERMINAL_TASK:
            await self.run_finished(session, run.run_id, status=AgentTaskStatus(task.status),
                                    failure=task.failure_code, cost=run.cost_total)
        return run

    async def get_run(self, session: AsyncSession, run_id: uuid.UUID) -> AgentRunRow | None:
        run = await session.get(AgentRunRow, run_id, populate_existing=True)
        return await self._synced(session, run) if run is not None else None

    async def list_runs(self, session: AsyncSession, *, agent_id: uuid.UUID, owner_user_id: uuid.UUID,
                        limit: int = 50) -> list[AgentRunRow]:
        rows = (await session.execute(
            select(AgentRunRow).where(AgentRunRow.agent_id == agent_id, AgentRunRow.owner_user_id == owner_user_id)
            .order_by(AgentRunRow.started_at.desc(), AgentRunRow.run_id).limit(limit)
        )).scalars().all()
        return [await self._synced(session, r) for r in rows]

    async def live_runs(self, session: AsyncSession, agent_id: uuid.UUID) -> list[AgentRunRow]:
        rows = (await session.execute(
            select(AgentRunRow).where(AgentRunRow.agent_id == agent_id, AgentRunRow.finished_at.is_(None))
        )).scalars().all()
        return list(rows)

    @staticmethod
    def run_view(run: AgentRunRow, *, task: AgentResult | None = None,
                 inbox_item_id: uuid.UUID | None = None) -> AgentRunView:
        return AgentRunView(
            run_id=run.run_id, agent_id=run.agent_id, version=run.version, status=AgentRunStatus(run.status),
            failure_code=run.failure_code, task_id=run.task_id, started_at=_as_utc(run.started_at),
            finished_at=_as_utc(run.finished_at) if run.finished_at else None, cost_total=run.cost_total,
            inbox_item_id=inbox_item_id, task=task,
        )

    # ── views ───────────────────────────────────────────────────────────

    def view(self, definition: AgentDefinitionRow, spec: CompiledAgentSpec | None) -> AgentView:
        common = dict(
            agent_id=definition.agent_id, name=definition.name or "", status=AgentStatus(definition.status),
            current_version=definition.current_version, created_at=_as_utc(definition.created_at),
            updated_at=_as_utc(definition.updated_at),
        )
        if spec is None:
            return AgentView(**common, template_id="", template_description="", runtime_display_name="",
                             model_profile_display_name="", can=(), cannot=(), trigger_display="",
                             budget=SpecBudget(per_run=0.0, per_month=0.0))
        template = self._registries.templates.get(spec.template_id)
        runtime = self._registries.runtimes.get(spec.selection.runtime_id)
        model = self._registries.model_profiles.get(spec.selection.model_profile_id)
        return AgentView(
            **common,
            template_id=spec.template_id,
            template_description=template.description if template else "",
            runtime_display_name=(runtime.display_name if runtime else "") or spec.selection.runtime_id,
            model_profile_display_name=(model.profile.display_name if model else "") or spec.selection.model_profile_id,
            can=can_lines(spec),
            cannot=cannot_lines(spec),
            trigger_display=trigger_line(spec),
            budget=spec.budget,
        )

    def budget_display(self, spec: CompiledAgentSpec) -> str:
        return budget_line(spec)


__all__ = ["AgentDefinitionService", "PreviewRefused"]
