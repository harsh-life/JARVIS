"""Pilot plan C6 (M2): `GET /usage` reports the caller's own ledger, over the
policy's own windows, and nothing about anyone else."""

from __future__ import annotations

import datetime
import uuid

import httpx
from sqlalchemy import select

from server.gateway.app import API_V1_PREFIX, create_app
from server.storage.models import AgentDefinitionRow, AgentRunRow, AgentRunUsageRow, Session, UsageEvent
from shared.schemas.enums import UsageKind
from shared.schemas.evaluation import EVALUATOR_USAGE_PREFIX
from tests.support import make_test_config

FIELDS = {"spent_24h", "daily_limit", "remaining_24h", "calls_last_minute", "calls_per_minute_limit",
          "spent_30d", "agents_30d"}


async def _user_of(api, onboarded) -> uuid.UUID:
    async with api.storage.session() as db:
        return (await db.execute(
            select(Session.user_id).where(Session.device_id == onboarded.device_id)
        )).scalars().first()


def _event(user_id, cost, at, tool_id=None) -> UsageEvent:
    return UsageEvent(usage_id=uuid.uuid4(), request_id=uuid.uuid4(), user_id=user_id,
                      kind=UsageKind.MODEL_CALL, provider="openai_compatible", model="m", tool_id=tool_id,
                      tokens_or_units=100, estimated_cost=cost, timestamp=at)


async def test_usage_is_the_callers_own_ledger_over_the_policy_windows(api):
    alice, bob = await api.onboard("alice"), await api.onboard("bob")
    alice_id, bob_id = await _user_of(api, alice), await _user_of(api, bob)
    now = datetime.datetime.now(datetime.timezone.utc)
    agent_id, run_id = uuid.uuid4(), uuid.uuid4()
    run_event = _event(alice_id, 0.25, now - datetime.timedelta(hours=1))
    async with api.storage.session() as db:
        db.add_all([
            _event(alice_id, 0.30, now - datetime.timedelta(seconds=20)),     # inside the rate minute
            _event(alice_id, 0.20, now - datetime.timedelta(days=2)),           # outside 24h, inside 30d
            _event(alice_id, 9.00, now - datetime.timedelta(days=40)),          # outside both
            _event(alice_id, 0.50, now, tool_id=f"{EVALUATOR_USAGE_PREFIX}x"),  # the Judge: never the user's
            _event(bob_id, 7.00, now),                                          # someone else's
            run_event,
            AgentDefinitionRow(agent_id=agent_id, owner_user_id=alice_id, name="Digest", status="active",
                               current_version=1, created_at=now, updated_at=now),
        ])
        await db.flush()
        db.add(AgentRunRow(run_id=run_id, agent_id=agent_id, owner_user_id=alice_id, version=1,
                           spec_hash="0" * 64, kind="on_demand", status="completed", cost_total=0.25,
                           started_at=now))
        await db.flush()
        db.add(AgentRunUsageRow(run_id=run_id, usage_id=run_event.usage_id))
        await db.commit()

    config = make_test_config(security={
        "oidc": {"client_id": "c", "issuer": "https://accounts.google.com"},
        "budgets": {"per_user_daily_cost_limit": 1.0, "global_daily_cost_limit": 3.0},
        "rate_limits": {"per_user_requests_per_minute": 40},
    })
    app = create_app(config=config, storage=api.storage, security=api.core)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"{API_V1_PREFIX}/usage", headers=alice.auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == FIELDS
    assert body["spent_24h"] == 0.55
    assert body["daily_limit"] == 1.0
    assert body["remaining_24h"] == 0.45
    assert body["calls_per_minute_limit"] == 40
    assert body["calls_last_minute"] == 1
    assert body["spent_30d"] == 0.75
    assert body["agents_30d"] == [{"agent_id": str(agent_id), "name": "Digest", "runs": 1, "spent": 0.25}]
    # Nothing about the global budget or anyone else's spend.
    assert "3.0" not in resp.text and "7.0" not in resp.text


async def test_usage_of_a_new_user_is_zero_and_needs_a_token(api):
    alice = await api.onboard("alice")
    assert (await api.client.get(f"{API_V1_PREFIX}/usage")).status_code == 401
    body = (await api.client.get(f"{API_V1_PREFIX}/usage", headers=alice.auth)).json()
    assert body["spent_24h"] == 0.0 and body["agents_30d"] == []
    assert body["daily_limit"] == 0.0 and body["remaining_24h"] == 0.0
