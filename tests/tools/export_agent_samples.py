"""`python -m tests.tools.export_agent_samples [export|check]`.

Writes `shared/android/agent_samples.json` (docs/29 §23.3, Phase 4): the
owner's agent list and agent-run responses exactly as the server serializes
them (`AgentListResponse`, `AgentRunView`, the error envelope) — a run tapped
from a reminder that completed, one paused for the owner's confirmation, and a
refused run. The Android client parses every one with its strict models
(android/contract AgentSampleTest); the paused run's `task` is the same
`AgentResult` the task screen already shows, confirmed through
`/agent/tasks/{id}/confirm` like any task.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from shared.schemas.agent import AgentResult, AgentTaskStatus, PendingAction, TaskCounters
from shared.schemas.agent_factory import (
    AgentListResponse,
    AgentRunStatus,
    AgentRunView,
    AgentStatus,
    AgentView,
    SpecBudget,
)
from shared.schemas.enums import RiskCategory
from shared.schemas.errors import ErrorCode, ErrorDetail, ErrorEnvelope

PATH = Path(__file__).resolve().parents[2] / "shared" / "android" / "agent_samples.json"

AGENT = uuid.UUID("6f1c2d3e-4a5b-4c6d-8e7f-00000000a001")
PAUSED = uuid.UUID("6f1c2d3e-4a5b-4c6d-8e7f-00000000a002")
RUN = uuid.UUID("6f1c2d3e-4a5b-4c6d-8e7f-00000000c001")
TASK = uuid.UUID("6f1c2d3e-4a5b-4c6d-8e7f-00000000f101")
WHEN = datetime(2026, 10, 2, 1, 30, tzinfo=timezone.utc)


def _agent(agent_id: uuid.UUID, name: str, status: AgentStatus, trigger: str) -> AgentView:
    return AgentView(
        agent_id=agent_id, name=name, status=status, current_version=2, template_id="research_digest",
        template_description="Reads web sources the server operator already allows and writes a concise digest "
                             "for the owner's inbox.",
        runtime_display_name="JARVIS built-in runtime", model_profile_display_name="General agent",
        can=("read the web from approved sites",), cannot=("write files", "send messages", "control devices"),
        trigger_display=trigger, budget=SpecBudget(per_run=0.0, per_month=0.0), created_at=WHEN, updated_at=WHEN,
        last_run=WHEN if status is AgentStatus.ACTIVE else None,
    )


def _run(status: AgentRunStatus, task: AgentResult) -> AgentRunView:
    return AgentRunView(run_id=RUN, agent_id=AGENT, version=2, kind="reminder_tap", status=status, task_id=TASK,
                        started_at=WHEN, finished_at=WHEN if status is AgentRunStatus.COMPLETED else None,
                        task=task)


def samples() -> dict:
    agents = AgentListResponse(items=(
        _agent(AGENT, "Security advisory digest", AgentStatus.ACTIVE,
               "when you ask, or when you tap its reminder (0 7 * * *, Asia/Kolkata)"),
        _agent(PAUSED, "Weekly reading list", AgentStatus.PAUSED, "when you ask"),
    ))
    done = _run(AgentRunStatus.COMPLETED, AgentResult(
        task_id=TASK, status=AgentTaskStatus.COMPLETED, response="Two critical advisories this morning.",
        counters=TaskCounters(iterations=2, model_calls=2, tool_calls=1)))
    pending = PendingAction(
        kind="tool_operation", capability="file.read", risk_category=RiskCategory.LOW_READ, tool_id="files.read",
        operation="list_directory", resource_scope={"path_prefix": "reports"}, requires_step_up=False,
        confirmation_token="ct_sample_token_not_real", expires_at=WHEN)
    waiting = _run(AgentRunStatus.WAITING, AgentResult(
        task_id=TASK, status=AgentTaskStatus.AWAITING_CONFIRMATION, pending=pending,
        counters=TaskCounters(iterations=1, model_calls=1)))
    refused = ErrorEnvelope(error=ErrorDetail(
        code=ErrorCode.CONFLICT, message="this agent is not active", request_id="6f1c2d3e-4a5b-4c6d-8e7f-00000000f001",
        retryable=False, details={"reason": "paused"}))
    return {
        "agent_list": json.loads(agents.model_dump_json()),
        "run_completed": json.loads(done.model_dump_json()),
        "run_waiting": json.loads(waiting.model_dump_json()),
        "run_refused": json.loads(refused.model_dump_json()),
        "run_tap_request": {"reminder_delivery_id": "6f1c2d3e-4a5b-4c6d-8e7f-00000000e001"},
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
    print("agent samples up to date" if ok else "agent samples stale — run export")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
