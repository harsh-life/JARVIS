"""The reminder frames on the device channel (docs/22 §2, docs/23 §4) — one
contract, two strict parsers. The Android side parses the same file in
android/contract ReminderFrameTest."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from shared.schemas.device_channel import (
    DeviceHello,
    DeviceOperationEnvelope,
    DeviceReminder,
    DeviceReminderAck,
)
from tests.tools.export_reminder_samples import PATH, render_file

SAMPLE = json.loads(PATH.read_text(encoding="ascii"))


def test_the_shared_reminder_samples_are_current():
    """Regenerate: `python -m tests.tools.export_reminder_samples export`."""

    assert PATH.read_text(encoding="ascii") == render_file()


def test_the_server_parses_what_the_device_sends():
    ack = DeviceReminderAck.model_validate(SAMPLE["device_to_server"]["ack"])
    assert ack.delivery_id == uuid.UUID(SAMPLE["server_to_device"]["on_time"]["delivery_id"])
    hello = DeviceHello.model_validate(SAMPLE["device_to_server"]["hello_with_reminders"])
    assert [f.value for f in hello.features] == ["reminders"]


def test_a_hello_without_features_is_still_accepted():
    """A client from before reminders keeps working; it just gets none."""

    payload = dict(SAMPLE["device_to_server"]["hello_with_reminders"])
    payload.pop("features")
    assert DeviceHello.model_validate(payload).features == []


@pytest.mark.parametrize("field", ["capability", "operation", "primitive", "arguments", "confirmation_token",
                                   "authorized", "expires_at", "task_id"])
def test_a_reminder_cannot_carry_authority_or_an_operation(field):
    payload = {**SAMPLE["server_to_device"]["on_time"], field: "x"}
    with pytest.raises(ValidationError):
        DeviceReminder.model_validate(payload)


def test_a_reminder_is_not_an_operation_and_vice_versa():
    with pytest.raises(ValidationError):
        DeviceOperationEnvelope.model_validate(SAMPLE["server_to_device"]["on_time"])
    with pytest.raises(ValidationError):
        DeviceReminder.model_validate({**SAMPLE["server_to_device"]["on_time"], "type": "operation"})


def test_reminder_text_is_bounded_and_non_empty():
    base = dict(SAMPLE["server_to_device"]["on_time"])
    for bad in ("", "x" * 8001):
        with pytest.raises(ValidationError):
            DeviceReminder.model_validate({**base, "task_reason": bad})
    assert DeviceReminder(delivery_id=uuid.uuid4(), job_id=uuid.uuid4(), device_id=uuid.uuid4(),
                          task_reason="x" * 8000, scheduled_for=datetime.now(timezone.utc))


def test_an_agent_reminder_carries_only_an_identifier_and_only_for_clients_that_asked():
    """docs/29 §17.1: `agent_id` is data for an `agent_reminders` client's
    "Run agent" button — no other sample carries it, and it grants nothing."""

    agent = DeviceReminder.model_validate(SAMPLE["server_to_device"]["agent_reminder"])
    assert agent.agent_id is not None
    for name in ("on_time", "late_recurring"):
        assert "agent_id" not in SAMPLE["server_to_device"][name]
    hello = DeviceHello.model_validate(SAMPLE["device_to_server"]["hello_with_agent_reminders"])
    assert [f.value for f in hello.features] == ["reminders", "agent_reminders"]
    with pytest.raises(ValidationError):
        DeviceReminder.model_validate({**SAMPLE["server_to_device"]["agent_reminder"], "agent_id": "not-an-id"})
