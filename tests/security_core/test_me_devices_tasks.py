"""Pilot plan C3/C4/C5 (M2): `GET /me`, `GET /devices`, `GET /agent/tasks`.

Owner-scoped reads through the real HTTP surface: another user's devices and
tasks are absent (not refused), responses carry only their documented fields,
and the task history pages by an owner-scoped cursor.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from sqlalchemy import select

from server.config import AppConfig
from server.gateway.app import API_V1_PREFIX
from server.gateway.routers.me import FeatureFlags, features_from_config
from server.storage.models import AgentTask, Session
from tests.support import make_test_config


ME_FIELDS = {"user_id", "display_name", "device_id", "active_graph_id", "active_devices", "features"}
DEVICE_FIELDS = {"device_id", "platform", "registered_at", "last_seen", "revoked", "revoked_at",
                 "step_up_enrolled", "connected", "current"}
TASK_FIELDS = {"task_id", "status", "mode", "failure_code", "origin", "device_id", "model_calls",
               "tool_calls", "created_at", "updated_at", "finished_at"}


async def _tasks(api, owner, n: int, *, start: datetime.datetime) -> list[uuid.UUID]:
    async with api.storage.session() as db:
        session_id = (await db.execute(
            select(Session.session_id).where(Session.device_id == owner.device_id)
        )).scalars().first()
        user_id = (await db.execute(
            select(Session.user_id).where(Session.session_id == session_id)
        )).scalar_one()
        ids = []
        for i in range(n):
            at = start + datetime.timedelta(minutes=i)
            task = AgentTask(task_id=uuid.uuid4(), user_id=user_id, device_id=owner.device_id,
                             session_id=session_id, status="completed", mode="execute",
                             response="SECRET RESULT TEXT", created_at=at, updated_at=at, finished_at=at)
            db.add(task)
            ids.append(task.task_id)
        await db.commit()
    return ids


async def test_me_is_the_caller_and_says_nothing_more(api):
    alice = await api.onboard("alice")
    resp = await api.client.get(f"{API_V1_PREFIX}/me", headers=alice.auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == ME_FIELDS
    assert body["device_id"] == str(alice.device_id)
    assert body["active_devices"] == 1
    assert body["features"] == FeatureFlags().model_dump()


async def test_me_and_friends_need_a_token(api):
    for path in ("/me", "/devices", "/agent/tasks"):
        assert (await api.client.get(f"{API_V1_PREFIX}{path}")).status_code == 401


def test_feature_flags_are_the_operators_configuration():
    config: AppConfig = make_test_config(memory={"enabled": True}, agents={"enabled": True})
    flags = features_from_config(config)
    assert flags.memory and flags.agents and flags.scheduler
    assert not flags.vault and not flags.android
    assert features_from_config(None) == FeatureFlags()


async def test_devices_are_the_callers_own_and_carry_no_credential(api):
    alice_phone = await api.onboard("alice")
    alice_tablet = await api.onboard("alice")
    bob = await api.onboard("bob")
    resp = await api.client.get(f"{API_V1_PREFIX}/devices", headers=alice_tablet.auth)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert {i["device_id"] for i in items} == {str(alice_phone.device_id), str(alice_tablet.device_id)}
    assert str(bob.device_id) not in resp.text
    for item in items:
        assert set(item) == DEVICE_FIELDS
        assert item["current"] == (item["device_id"] == str(alice_tablet.device_id))
    assert alice_phone.credential not in resp.text


async def test_a_revoked_device_is_listed_as_revoked(api):
    phone = await api.onboard("alice")
    tablet = await api.onboard("alice")
    assert (await api.client.delete(f"{API_V1_PREFIX}/devices/{phone.device_id}",
                                    headers=tablet.auth)).status_code == 204
    items = {i["device_id"]: i for i in (await api.client.get(f"{API_V1_PREFIX}/devices",
                                                               headers=tablet.auth)).json()["items"]}
    assert items[str(phone.device_id)]["revoked"] is True
    assert items[str(phone.device_id)]["connected"] is False
    me = (await api.client.get(f"{API_V1_PREFIX}/me", headers=tablet.auth)).json()
    assert me["active_devices"] == 1


async def test_task_history_is_the_callers_own_newest_first_and_bounded(api):
    alice = await api.onboard("alice")
    bob = await api.onboard("bob")
    t0 = datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc)
    mine = await _tasks(api, alice, 3, start=t0)
    theirs = await _tasks(api, bob, 2, start=t0)
    resp = await api.client.get(f"{API_V1_PREFIX}/agent/tasks", headers=alice.auth)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [i["task_id"] for i in body["items"]] == [str(t) for t in reversed(mine)]
    assert body["next_before"] is None
    assert all(set(i) == TASK_FIELDS for i in body["items"])
    assert all(i["origin"] == "device" for i in body["items"])
    assert "SECRET RESULT TEXT" not in resp.text
    assert not any(str(t) in resp.text for t in theirs)


async def test_task_history_pages_by_an_owner_scoped_cursor(api):
    alice = await api.onboard("alice")
    bob = await api.onboard("bob")
    t0 = datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc)
    mine = list(reversed(await _tasks(api, alice, 5, start=t0)))
    theirs = await _tasks(api, bob, 1, start=t0)
    seen, before = [], None
    while True:
        params = {"limit": 2} | ({"before": before} if before else {})
        page = (await api.client.get(f"{API_V1_PREFIX}/agent/tasks", params=params, headers=alice.auth)).json()
        seen += [i["task_id"] for i in page["items"]]
        before = page["next_before"]
        if before is None:
            break
    assert seen == [str(t) for t in mine]
    # Another user's task id is not a cursor for the caller.
    resp = await api.client.get(f"{API_V1_PREFIX}/agent/tasks", params={"before": str(theirs[0])},
                                headers=alice.auth)
    assert resp.status_code == 422
    assert (await api.client.get(f"{API_V1_PREFIX}/agent/tasks", params={"limit": 0},
                                 headers=alice.auth)).status_code == 422
    assert (await api.client.get(f"{API_V1_PREFIX}/agent/tasks", params={"limit": 101},
                                 headers=alice.auth)).status_code == 422
