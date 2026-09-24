"""The runtime's audit vocabulary.

`server.agent` cannot import `server.security.events` (that package's audit
writer reaches `server.secrets`), so the runtime names its events here and the
composition root maps each one onto an `AuditAction` — the same translation
pattern the SecretStore uses (`SECRET_ACTION_BY_NAME`). An unmapped event is a
programming error there, not a free-form action name.
"""

from __future__ import annotations

from enum import Enum


class AgentEvent(str, Enum):
    TASK_SUBMITTED = "agent.task.submitted"
    TASK_PAUSED = "agent.task.paused"
    TASK_COMPLETED = "agent.task.completed"
    TASK_FAILED = "agent.task.failed"
    TASK_CANCELLED = "agent.task.cancelled"
    PROPOSAL_REJECTED = "agent.proposal.rejected"
    TOOL_EXECUTED = "agent.tool.executed"
    TOOL_FAILED = "agent.tool.failed"
    CAPABILITY_ACTIVATED = "capability.activated"
    CAPABILITY_ACTIVATION_REFUSED = "capability.activation.refused"
    CAPABILITY_DEACTIVATED = "capability.deactivated"
    CONFIRMATION_ISSUED = "confirmation.issued"
    CONFIRMATION_ACCEPTED = "confirmation.accepted"
    CONFIRMATION_REJECTED = "confirmation.rejected"
    LIMIT_EXCEEDED = "usage.limit.exceeded"
    BREAKER_TRIPPED = "breaker.tripped"
