"""The agent runtime — 05_AGENT_RUNTIME.md.

The model proposes; deterministic infrastructure decides and executes; the human
confirms where the risk model requires it (P1).

`server.agent` imports no authorization, capability, or secrets module (CI
contracts in pyproject). Everything it needs from the Security Core arrives
through the Protocols in `ports.py`, satisfied at the composition root
(`server/composition/`) by the existing `AuthorizationEngine`,
`ConfirmationService`, `CapabilityGrantService`, and `AuditLogger`.
"""

from server.agent.bounds import ConcurrencyGate, ConcurrencyLimits, RuntimeBounds
from server.agent.runtime import (
    AgentRuntime,
    ConfirmationMismatch,
    StepUpNeeded,
    StopOutcome,
    SubmissionsSuspended,
    TaskNotAwaiting,
    TaskNotFound,
)

__all__ = [
    "AgentRuntime",
    "ConcurrencyGate",
    "ConcurrencyLimits",
    "ConfirmationMismatch",
    "RuntimeBounds",
    "StepUpNeeded",
    "StopOutcome",
    "SubmissionsSuspended",
    "TaskNotAwaiting",
    "TaskNotFound",
]
