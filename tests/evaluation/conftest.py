"""Harness for the evaluation suites: the runtime harness (production composition
root, real Security Core, real ledgers) with deterministic evaluation switched on
like any operator would — `evaluation: {enabled: true, evaluator: rules}`.

The LLM Judge these suites used to script is retired (D-JDG-RETIRE); there is no
model to script for evaluation any more. `TOKEN`/`SU` remain because other suites
use them as the disposable superuser credential.
"""

from __future__ import annotations

from typing import Any

import pytest_asyncio
from sqlalchemy import select

from server.security.events import AuditAction
from server.security.superuser import SUPERUSER_TOKEN_ENV
from server.storage.models import AuditEvent, TaskEvaluation
from tests.runtime.conftest import make_harness  # noqa: F401  (fixture re-export)

TOKEN = "TEST-ONLY-superuser-credential-0123456789abcdef"
SU = {"Authorization": f"Superuser {TOKEN}"}
PKG = {"package_name": "com.example"}


def rules_config(**overrides: Any) -> dict:
    section: dict[str, Any] = {
        "enabled": True,
        "evaluator": "rules",
        "post_hoc": {"sample_successful": 1.0, "always_on_failure": True},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(section.get(key), dict):
            section[key] = {**section[key], **value}
        else:
            section[key] = value
    return {"evaluation": section}


@pytest_asyncio.fixture
async def evaluated(make_harness, monkeypatch):  # noqa: F811 — the re-exported fixture
    """`await evaluated(**evaluation_overrides)` → a harness with rules evaluation on."""

    monkeypatch.setenv(SUPERUSER_TOKEN_ENV, TOKEN)

    async def _make(*, extra_config: dict | None = None, **evaluation: Any):
        config = rules_config(**evaluation)
        if extra_config:
            config = {**config, **extra_config}
        return await make_harness(config=config)

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
