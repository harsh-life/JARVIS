"""The operator console's views (28 §3) — read-only by construction.

> The dashboard shows. Controls live elsewhere. (28 §0)

Every method here is a read: `SELECT`s over the storage models plus the
read-only live snapshots declared in `server/dashboard/ports.py`. There is no
insert, update, delete, flush or commit anywhere in this package (checked by
`tests/dashboard/test_console_readonly.py`), and the package can reach no
mutation path (pyproject: "The dashboard is read-only"). Authentication is not
here either: the gateway's routes (`server/gateway/routers/admin.py`) require
the superuser principal and hand this class a session.

What leaves this module is secret-free (DASH-005) and PII-redacted (DASH-006):
user content appears only as its length, secrets only as handles plus whether
they resolve, and every free-text field is additionally scrubbed with the
repository's secret patterns. `task_content` is the one exception — the
unredacted view — and the route that calls it audits the access first.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config.schema import AppConfig
from server.dashboard.ports import ComponentProbes, LiveStateView, SecretResolvability
from server.dashboard.redaction import config_view, redact_user_text, scrub
from server.storage.models import (
    AgentTask,
    AuditEvent,
    ConfigVersion,
    Device,
    ImprovementCandidateRow,
    SupervisorLatch,
    TaskEvaluation,
    UsageEvent,
    User,
)
from shared.schemas.agent import AgentTaskStatus, TERMINAL_STATUSES
from shared.schemas.evaluation import EVALUATOR_USAGE_PREFIX

MAX_ROWS = 500
_PAUSED = {AgentTaskStatus.AWAITING_CONFIRMATION.value, AgentTaskStatus.WAITING_FOR_PLATFORM.value}
_TERMINAL = {s.value for s in TERMINAL_STATUSES}
_RECOVERY_ACTIONS = (
    "agent.worker.switched", "agent.worker.failed", "agent.stall.detected", "agent.recovery.exhausted",
    "breaker.tripped", "breaker.global.latched", "breaker.global.cleared",
)
_BREAK_GLASS_ACTIONS = ("break_glass.activated", "break_glass.invoked", "break_glass.ended", "control.break_glass")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _id(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None


def _limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_ROWS))


class OperatorConsole:
    """Implements `server.gateway.console_port.OperatorConsolePort`."""

    def __init__(self, *, config: AppConfig, live: LiveStateView, probes: ComponentProbes,
                 secrets: SecretResolvability) -> None:
        self._config = config
        self._live = live
        self._probes = probes
        self._secrets = secrets

    # ── the persistent banner (28 §4, DSH-T5) ──────────────────────────

    async def banners(self, session: AsyncSession) -> list[dict]:
        banners: list[dict] = []
        row = await session.get(SupervisorLatch, 1)
        latched_row = row is not None and row.latched
        if latched_row or self._live.latched_in_process():
            banners.append({
                "kind": "global_stop_latched",
                "message": "A global emergency stop is latched: no new tasks are accepted until an "
                           "operator clears it.",
                "reason": row.reason if latched_row else None,
                "since": _iso(row.changed_at) if latched_row else None,
            })
        if self._live.break_glass_enabled():
            active = self._live.break_glass_records()
            banners.append({
                "kind": "break_glass_enabled",
                "message": "Break-glass (unconfined execution) is enabled on this server"
                           + (f"; {len(active)} activation(s) are live." if active else "."),
                "active_records": len(active),
            })
        return banners

    # ── 1. health ──────────────────────────────────────────────────────

    async def health(self, session: AsyncSession, *, probe_models: bool = False) -> dict:
        components = await self._probes.components(probe_models=probe_models)
        try:
            await session.execute(select(func.count()).select_from(User))
            components["database"] = {"status": "ok"}
        except Exception:  # noqa: BLE001 — a view reports, it does not fail
            components["database"] = {"status": "unavailable"}
        return {"components": components}

    # ── 2. tasks ───────────────────────────────────────────────────────

    async def tasks(self, session: AsyncSession, *, limit: int = 100, status: str | None = None) -> dict:
        counts = dict((await session.execute(
            select(AgentTask.status, func.count()).group_by(AgentTask.status)
        )).all())
        query = select(AgentTask).order_by(AgentTask.created_at.desc()).limit(_limit(limit))
        if status:
            query = query.where(AgentTask.status == status)
        rows = (await session.execute(query)).scalars().all()
        return {
            "counts": {
                "running": sum(n for s, n in counts.items() if s == AgentTaskStatus.RUNNING.value),
                "paused": sum(n for s, n in counts.items() if s in _PAUSED),
                "terminal": sum(n for s, n in counts.items() if s in _TERMINAL),
                "by_status": counts,
            },
            "live": [
                {**{k: v for k, v in vars(t).items()}, "task_id": str(t.task_id), "user_id": str(t.user_id)}
                for t in self._live.live_tasks()
            ],
            "tasks": [
                {
                    "task_id": str(r.task_id), "user_id": str(r.user_id), "device_id": str(r.device_id),
                    "graph_id": _id(r.graph_id), "status": r.status, "mode": r.mode,
                    "failure_code": r.failure_code, "worker_switches": r.worker_switches or 0,
                    "counters": {"iterations": r.iterations, "model_calls": r.model_calls,
                                 "tool_calls": r.tool_calls},
                    "created_at": _iso(r.created_at), "finished_at": _iso(r.finished_at),
                    "response": redact_user_text(r.response),
                }
                for r in rows
            ],
        }

    # ── 3. recovery & breaker ──────────────────────────────────────────

    async def recovery(self, session: AsyncSession, *, limit: int = 100) -> dict:
        counts = dict((await session.execute(
            select(AuditEvent.action, func.count()).where(AuditEvent.action.in_(_RECOVERY_ACTIONS))
            .group_by(AuditEvent.action)
        )).all())
        trips = (await session.execute(
            select(AuditEvent).where(AuditEvent.action == "breaker.tripped")
            .order_by(AuditEvent.timestamp.desc()).limit(_limit(limit))
        )).scalars().all()
        by_source: Counter[str] = Counter()
        recent = []
        for event in trips:
            parts = event.resource.split(":")  # breaker:task:<id>:<source>[:<reason>]
            source = parts[3] if len(parts) > 3 else "unknown"
            by_source[source] += 1
            recent.append({"task_id": parts[2] if len(parts) > 2 else None, "source": source,
                           "reason": parts[4] if len(parts) > 4 else source,
                           "user_id": _id(event.user_id), "at": _iso(event.timestamp)})
        row = await session.get(SupervisorLatch, 1)
        return {
            "counts": {
                "worker_switches": counts.get("agent.worker.switched", 0),
                "worker_failures": counts.get("agent.worker.failed", 0),
                "stalls": counts.get("agent.stall.detected", 0),
                "recovery_exhausted": counts.get("agent.recovery.exhausted", 0),
                "trips": counts.get("breaker.tripped", 0),
                "global_latched_events": counts.get("breaker.global.latched", 0),
                "global_cleared_events": counts.get("breaker.global.cleared", 0),
            },
            "trips_by_source": dict(by_source),
            "recent_trips": recent,
            "global_latch": {
                "latched": bool(row is not None and row.latched) or self._live.latched_in_process(),
                "persisted": bool(row is not None and row.latched),
                "in_process": self._live.latched_in_process(),
                "reason": row.reason if row is not None else None,
                "changed_at": _iso(row.changed_at) if row is not None else None,
            },
        }

    # ── 4. break-glass ─────────────────────────────────────────────────

    async def break_glass(self, session: AsyncSession, *, limit: int = 100) -> dict:
        events = (await session.execute(
            select(AuditEvent).where(AuditEvent.action.in_(_BREAK_GLASS_ACTIONS))
            .order_by(AuditEvent.timestamp.desc()).limit(_limit(limit))
        )).scalars().all()
        return {
            "enabled": self._live.break_glass_enabled(),
            "active": [
                {**vars(r), "task_id": str(r.task_id), "user_id": str(r.user_id),
                 "executables": list(r.executables), "activated_at": _iso(r.activated_at),
                 "expires_at": _iso(r.expires_at)}
                for r in self._live.break_glass_records()
            ],
            "events": [
                {"action": e.action, "resource": scrub(e.resource), "result": e.result.value,
                 "user_id": _id(e.user_id), "at": _iso(e.timestamp)}
                for e in events
            ],
        }

    # ── 5. evaluations & the review queue ──────────────────────────────

    async def evaluations(self, session: AsyncSession, *, limit: int = 100) -> dict:
        rows = (await session.execute(
            select(TaskEvaluation).order_by(TaskEvaluation.created_at.desc()).limit(_limit(limit))
        )).scalars().all()
        outcomes = dict((await session.execute(
            select(TaskEvaluation.outcome, func.count()).group_by(TaskEvaluation.outcome)
        )).all())
        candidates = (await session.execute(
            select(ImprovementCandidateRow).order_by(ImprovementCandidateRow.created_at.desc())
            .limit(_limit(limit))
        )).scalars().all()
        versions = (await session.execute(
            select(ConfigVersion).order_by(ConfigVersion.version_id.desc()).limit(_limit(limit))
        )).scalars().all()
        return {
            "judge": self._probes.judge_state(),
            "outcomes": outcomes,
            "evaluations": [self._evaluation(r) for r in rows],
            "candidates": [
                {
                    "candidate_id": str(c.candidate_id), "evaluation_id": str(c.evaluation_id),
                    "task_id": str(c.task_id), "source_user_id": str(c.source_user_id),
                    "evaluator_id": c.evaluator_id, "target": c.target, "subject": c.subject,
                    "status": c.status, "created_at": _iso(c.created_at), "decided_at": _iso(c.decided_at),
                    "decision_reason": c.decision_reason, "config_version_id": c.config_version_id,
                    "evidence": list(c.evidence or []),
                    "proposed_value": redact_user_text(c.proposed_value),
                    "expected_effect": redact_user_text(c.expected_effect),
                }
                for c in candidates
            ],
            "config_versions": [
                {"version_id": v.version_id, "target_key": v.target_key, "action": v.action,
                 "candidate_id": _id(v.candidate_id), "rolled_back_version_id": v.rolled_back_version_id,
                 "reason": v.reason, "created_at": _iso(v.created_at),
                 "value": redact_user_text(v.value)}
                for v in versions
            ],
        }

    @staticmethod
    def _evaluation(r: TaskEvaluation, *, unredacted: bool = False) -> dict:
        findings = dict(r.findings or {})
        failures = []
        for f in findings.get("failures") or []:
            note = f.get("note") or ""
            failures.append({"step_ref": f.get("step_ref"), "category": f.get("category"),
                             "note": scrub(note) if unredacted else redact_user_text(note)})
        return {
            "evaluation_id": str(r.evaluation_id), "task_id": str(r.task_id), "owner_user_id": str(r.owner_user_id),
            "evaluator_id": r.evaluator_id, "evaluator_version": r.evaluator_version, "kind": r.kind,
            "outcome": r.outcome, "reason_code": r.reason_code, "quality": r.quality, "efficiency": r.efficiency,
            "anomaly": r.anomaly, "anomaly_reason": r.anomaly_reason,
            "stop": {"requested": r.stop_requested, "honoured": r.stop_honoured,
                     "not_honoured_because": r.stop_not_honoured_because},
            "redundant_steps": list(findings.get("redundant_steps") or []),
            "failures": failures, "reward": findings.get("reward"),
            "redactions": list(r.redactions or []), "created_at": _iso(r.created_at),
        }

    # ── 6. usage & budget ──────────────────────────────────────────────

    async def usage(self, session: AsyncSession) -> dict:
        now = _utcnow()
        day, minute = now - timedelta(days=1), now - timedelta(minutes=1)
        judge = UsageEvent.tool_id.like(f"{EVALUATOR_USAGE_PREFIX}%")
        not_judge = UsageEvent.tool_id.is_(None) | ~judge

        async def totals(*where) -> dict:
            calls, cost = (await session.execute(
                select(func.count(), func.coalesce(func.sum(UsageEvent.estimated_cost), 0.0)).where(*where)
            )).one()
            return {"calls": int(calls), "cost": float(cost)}

        per_user = (await session.execute(
            select(UsageEvent.user_id, func.count(), func.coalesce(func.sum(UsageEvent.estimated_cost), 0.0))
            .where(UsageEvent.timestamp >= day, not_judge).group_by(UsageEvent.user_id)
        )).all()
        per_user_judge = {
            uid: (int(n), float(c)) for uid, n, c in (await session.execute(
                select(UsageEvent.user_id, func.count(), func.coalesce(func.sum(UsageEvent.estimated_cost), 0.0))
                .where(UsageEvent.timestamp >= day, judge).group_by(UsageEvent.user_id)
            )).all()
        }
        budgets, rates = self._config.security.budgets, self._config.security.rate_limits
        users = {uid for uid, *_ in per_user} | set(per_user_judge)
        spent = {uid: (int(n), float(c)) for uid, n, c in per_user}
        return {
            "window": {"budget": "24h", "rate": "1m"},
            "global": {
                "last_24h": await totals(UsageEvent.timestamp >= day, not_judge),
                "last_minute": await totals(UsageEvent.timestamp >= minute, not_judge),
                "judge_last_24h": await totals(UsageEvent.timestamp >= day, judge),
            },
            "per_user": [
                {"user_id": str(uid), "calls_24h": spent.get(uid, (0, 0.0))[0],
                 "cost_24h": spent.get(uid, (0, 0.0))[1],
                 "judge_calls_24h": per_user_judge.get(uid, (0, 0.0))[0],
                 "judge_cost_24h": per_user_judge.get(uid, (0, 0.0))[1]}
                for uid in sorted(users, key=str)
            ],
            "limits": {
                "per_user_daily_cost_limit": budgets.per_user_daily_cost_limit,
                "global_daily_cost_limit": budgets.global_daily_cost_limit,
                "per_user_requests_per_minute": rates.per_user_requests_per_minute,
                "global_requests_per_minute": rates.global_requests_per_minute,
                "evaluation_budget": self._config.evaluation.budget,
                "evaluation_budget_scope": self._config.evaluation.budget_scope,
            },
        }

    # ── 7. memory & vault ──────────────────────────────────────────────

    async def memory(self, session: AsyncSession) -> dict:
        user_ids = list((await session.execute(select(User.user_id).limit(1000))).scalars())
        counts = await self._probes.memory_fact_counts(user_ids)
        return {
            "memory": {
                "enabled": counts is not None,
                "facts_per_user": (
                    [{"user_id": str(uid), "facts": n} for uid, n in sorted(counts.items(), key=lambda i: str(i[0]))]
                    if counts is not None else None
                ),
                "content": "withheld",  # never shown here (28 §3)
            },
            "vault": await self._probes.vault_status(),
        }

    # ── 8. devices ─────────────────────────────────────────────────────

    async def devices(self, session: AsyncSession, *, limit: int = 200) -> dict:
        rows = (await session.execute(
            select(Device).order_by(Device.registered_at.desc()).limit(_limit(limit))
        )).scalars().all()
        connected = await self._probes.connected_device_ids()
        return {
            "devices": [
                {"device_id": str(d.device_id), "user_id": str(d.user_id), "platform": d.platform.value,
                 "registered_at": _iso(d.registered_at), "last_seen": _iso(d.last_seen),
                 "revoked": d.revoked, "revoked_at": _iso(d.revoked_at),
                 "connected": d.device_id in connected,
                 "step_up_key_registered": d.step_up_public_key is not None,
                 "push_registered": d.push_token is not None}
                for d in rows
            ],
            "connected_count": len(connected),
        }

    # ── 9. audit ───────────────────────────────────────────────────────

    async def audit(self, session: AsyncSession, *, action: str | None = None, user_id: uuid.UUID | None = None,
                    result: str | None = None, resource_prefix: str | None = None,
                    since: datetime | None = None, limit: int = 200) -> dict:
        query = select(AuditEvent).order_by(AuditEvent.timestamp.desc()).limit(_limit(limit))
        if action:
            query = query.where(AuditEvent.action.like(action[:-1] + "%") if action.endswith("*")
                                else AuditEvent.action == action)
        if user_id is not None:
            query = query.where(AuditEvent.user_id == user_id)
        if result:
            query = query.where(AuditEvent.result == result)
        if resource_prefix:
            query = query.where(AuditEvent.resource.like(resource_prefix.replace("%", "") + "%"))
        if since is not None:
            query = query.where(AuditEvent.timestamp >= since)
        rows = (await session.execute(query)).scalars().all()
        return {
            "events": [
                {"event_id": str(e.event_id), "request_id": str(e.request_id), "at": _iso(e.timestamp),
                 "actor": e.actor.value, "action": e.action, "resource": scrub(e.resource),
                 "result": e.result.value, "decision": e.decision.value if e.decision else None,
                 "user_id": _id(e.user_id), "device_id": _id(e.device_id), "graph_id": _id(e.graph_id)}
                for e in rows
            ],
        }

    # ── 10. configuration ──────────────────────────────────────────────

    async def configuration(self, session: AsyncSession) -> dict:
        effective = self._config.model_dump(mode="json")
        return {"effective": await config_view(effective, self._secrets.resolves)}

    # ── the unredacted view (DASH-006) — the route audits before calling ─

    async def task_content(self, session: AsyncSession, task_id: uuid.UUID) -> dict | None:
        """One task's user content, unredacted — except for secret-shaped text,
        which no view ever shows. Only the privileged, audited route calls this."""

        task = await session.get(AgentTask, task_id)
        if task is None:
            return None
        evaluations = (await session.execute(
            select(TaskEvaluation).where(TaskEvaluation.task_id == task_id).order_by(TaskEvaluation.created_at)
        )).scalars().all()
        candidates = (await session.execute(
            select(ImprovementCandidateRow).where(ImprovementCandidateRow.task_id == task_id)
        )).scalars().all()
        return {
            "task_id": str(task.task_id), "user_id": str(task.user_id), "status": task.status,
            "response": scrub(task.response) if task.response is not None else None,
            "evaluations": [self._evaluation(r, unredacted=True) for r in evaluations],
            "candidates": [
                {"candidate_id": str(c.candidate_id), "target": c.target, "subject": c.subject,
                 "status": c.status, "proposed_value": scrub(c.proposed_value),
                 "expected_effect": scrub(c.expected_effect)}
                for c in candidates
            ],
        }


def view_names() -> tuple[str, ...]:
    return ("health", "tasks", "recovery", "break_glass", "evaluations", "usage", "memory", "devices",
            "audit", "configuration")


__all__: Any = ["OperatorConsole", "view_names"]
