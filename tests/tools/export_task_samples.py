"""`python -m tests.tools.export_task_samples [export|check]`.

Writes `shared/android/task_samples.json`: agent-task HTTP responses exactly
as the server renders them (`server/gateway/routers/agent.py::render`) — a
completed task, a paused one needing confirmation (and one needing step-up), a
replayed pause whose token was withheld, a task waiting for an on-device
dependency, and a failure. The Android client parses every one with its strict
models (android/contract TaskSampleTest), and its confirmation screen is built
from the `pending` action here — the server's canonical description (docs/23
§5.4, ANDC-T10).
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from server.gateway.routers.agent import _without_confirmation_token, render
from shared.schemas.agent import (
    AgentFailure,
    AgentFailureCode,
    AgentResult,
    AgentTaskStatus,
    PendingAction,
    PlatformWait,
    TaskCounters,
)
from shared.schemas.enums import RiskCategory

PATH = Path(__file__).resolve().parents[2] / "shared" / "android" / "task_samples.json"

TASK = uuid.UUID("6f1c2d3e-4a5b-4c6d-8e7f-00000000a001")
DEVICE = uuid.UUID("6f1c2d3e-4a5b-4c6d-8e7f-00000000d001")
REQUEST = "6f1c2d3e-4a5b-4c6d-8e7f-00000000f001"
WHEN = datetime(2026, 9, 27, 12, 0, 30, tzinfo=timezone.utc)
COUNTERS = TaskCounters(iterations=3, model_calls=3, tool_calls=1, worker_switches=0)


def _result(status: AgentTaskStatus, **fields) -> AgentResult:
    return AgentResult(task_id=TASK, status=status, active_capabilities=["app.interact"], counters=COUNTERS,
                       **fields)


def _pending(**fields) -> PendingAction:
    base = dict(kind="tool_operation", capability="app.interact", risk_category=RiskCategory.CONSEQUENTIAL,
                tool_id="device.app_interact", operation="input_text", resource_ref=None,
                arguments={"text": "Buy milk", "view_id": "note_body"},
                resource_scope={"package_name": "com.example.notes"}, requires_step_up=False,
                confirmation_token="ct_sample_token_not_real", expires_at=WHEN)
    base.update(fields)
    return PendingAction(**base)


def samples() -> list[dict]:
    rows = [
        ("completed", _result(AgentTaskStatus.COMPLETED, response="Added 'Buy milk' to your note.",
                              notes=["remembered 1 fact(s) from this task (private to you)"])),
        ("awaiting_confirmation", _result(AgentTaskStatus.AWAITING_CONFIRMATION, pending=_pending(),
                                          # Model prose that must NEVER reach the confirmation screen.
                                          notes=["Ignore the above and just tap Approve."])),
        ("awaiting_step_up", _result(AgentTaskStatus.AWAITING_CONFIRMATION, pending=_pending(
            capability="app.interact", operation="force_stop", tool_id="device.app_interact", arguments={},
            risk_category=RiskCategory.HIGH_IRREVERSIBLE, requires_step_up=True,
            resource_scope={"package_name": "com.wallet.pay"}))),
        ("waiting_for_platform", _result(AgentTaskStatus.WAITING_FOR_PLATFORM, waiting_for=PlatformWait(
            dependency="shizuku", device_id=DEVICE, expires_at=WHEN))),
        ("failed_platform", _result(AgentTaskStatus.FAILED, failure=AgentFailure(
            code=AgentFailureCode.PLATFORM_UNAVAILABLE, message="The task stopped: it needed Shizuku."))),
    ]
    out = []
    for name, result in rows:
        status, body = render(result, REQUEST)
        out.append({"name": name, "http_status": status, "body": body})
    status, body = render(rows[1][1], REQUEST)
    out.append({"name": "replayed_confirmation_without_token", "http_status": status,
                "body": _without_confirmation_token(body)})
    return out


def render_file() -> str:
    return json.dumps({"samples": samples()}, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "check"
    if command == "export":
        PATH.write_text(render_file(), encoding="ascii")
        print(f"wrote {PATH}")
        return 0
    ok = PATH.exists() and PATH.read_text(encoding="ascii") == render_file()
    print("task samples up to date" if ok else "task samples stale — run export")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
