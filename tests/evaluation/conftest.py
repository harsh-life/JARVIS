"""Harness for the Judge suites: the runtime harness (production composition root,
real Security Core, real ledgers), with the Judge configured like any operator
would — `evaluation.provider` naming a model — and that model scripted."""

from __future__ import annotations

from typing import Any

import pytest_asyncio
from sqlalchemy import select

from server.security.events import AuditAction
from server.security.superuser import SUPERUSER_TOKEN_ENV
from server.storage.models import AuditEvent, TaskEvaluation
from tests.runtime.conftest import ScriptedModel, make_harness  # noqa: F401  (fixture re-export)

TOKEN = "TEST-ONLY-superuser-credential-0123456789abcdef"
SU = {"Authorization": f"Superuser {TOKEN}"}
PKG = {"package_name": "com.example"}


def judge_config(**overrides: Any) -> dict:
    section: dict[str, Any] = {
        "enabled": True,
        "evaluator": "llm",
        "provider": {"provider": "ollama", "model": "scripted-judge", "timeout_seconds": 5},
        "post_hoc": {"sample_successful": 1.0, "always_on_failure": True},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(section.get(key), dict):
            section[key] = {**section[key], **value}
        else:
            section[key] = value
    return {"evaluation": section}


def verdict(**fields: Any) -> str:
    import json

    return json.dumps(fields)


@pytest_asyncio.fixture
async def judged(make_harness, monkeypatch):
    """`await judged(config_overrides...)` → (harness, judge model)."""

    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, TOKEN)

    async def _make(*, extra_config: dict | None = None, **evaluation: Any):
        judge = ScriptedModel("scripted-judge")
        config = judge_config(**evaluation)
        if extra_config:
            config = {**config, **extra_config}
        h = await make_harness(config=config, models={"scripted-judge": judge})
        return h, judge

    return _make


async def drain(h) -> None:
    jobs = h.app.state.evaluation
    if jobs is not None:
        await jobs.queue.drain()


async def evaluations(h) -> list[TaskEvaluation]:
    async with h.storage.session() as s:
        return list((await s.execute(select(TaskEvaluation).order_by(TaskEvaluation.created_at))).scalars())


async def audit(h, action: AuditAction) -> list[AuditEvent]:
    return [r for r in await h.rows(AuditEvent) if r.action == action.value]
