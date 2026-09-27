"""`python -m tests.tools.export_reminder_samples [export|check]`.

Writes `shared/android/reminder_samples.json`: reminder frames exactly as the
server serializes them (`DeviceReminder.model_dump_json`) — on time, late and
recurring — the acknowledgement the device sends back, and a `hello` declaring
the `reminders` feature. The Android client parses every one with its strict
models (android/contract ReminderFrameTest), and the server parses the device's
frames back with its own (tests/scheduler/test_reminder_contract.py).
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from shared.schemas.device_channel import ChannelFeature, DeviceHello, DeviceReminder, DeviceReminderAck

PATH = Path(__file__).resolve().parents[2] / "shared" / "android" / "reminder_samples.json"

DELIVERY = uuid.UUID("6f1c2d3e-4a5b-4c6d-8e7f-00000000e001")
JOB = uuid.UUID("6f1c2d3e-4a5b-4c6d-8e7f-00000000b001")
DEVICE = uuid.UUID("6f1c2d3e-4a5b-4c6d-8e7f-00000000d001")
WHEN = datetime(2026, 10, 1, 3, 30, tzinfo=timezone.utc)


def samples() -> dict:
    on_time = DeviceReminder(delivery_id=DELIVERY, job_id=JOB, device_id=DEVICE,
                             task_reason="Call the dentist to move Thursday's appointment", scheduled_for=WHEN)
    late = on_time.model_copy(update={"late": True, "recurring": True,
                                      "task_reason": "Every Monday at 8, remind me to put the bins out"})
    hello = DeviceHello(access_token="sample-token-not-real", device_proof="v1.sample.proof.not.real",
                        mapping_version="1-0123456789abcdef", client_version="0.1",
                        features=[ChannelFeature.REMINDERS])
    return {
        "server_to_device": {
            "on_time": json.loads(on_time.model_dump_json()),
            "late_recurring": json.loads(late.model_dump_json()),
        },
        "device_to_server": {
            "ack": json.loads(DeviceReminderAck(delivery_id=DELIVERY).model_dump_json()),
            "hello_with_reminders": json.loads(hello.model_dump_json()),
        },
    }


def render_file() -> str:
    return json.dumps(samples(), indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "check"
    if command == "export":
        PATH.write_text(render_file(), encoding="ascii")
        print(f"wrote {PATH}")
        return 0
    ok = PATH.exists() and PATH.read_text(encoding="ascii") == render_file()
    print("reminder samples up to date" if ok else "reminder samples stale — run export")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
