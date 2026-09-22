"""The agent runtime — 05_AGENT_RUNTIME.md.

The propose→authorize→execute loop (`orchestrator.py`), task lifecycle
(`tasks.py`), deterministic context assembly (`context.py`), and deterministic
proposal parsing (`proposal.py`). See `ports.py`'s module docstring for the
mechanically-enforced boundary this package sits behind: it imports nothing
from `server.graph`, `server.capabilities`, `server.secrets`, `server.storage`,
or `server.gateway`, and nothing from its sibling packages
(`server.tools`/`server.modeltools`/`server.models`/`server.memory`) either —
every capability it needs is a constructor-injected `Protocol` implementation
the gateway composition root (`server/gateway/runtime.py`) supplies.

The runtime coordinates; it does not become the security authority (this
branch's own instruction, restated as code rather than merely as a comment):
every action a model proposes is authorized by `04` through the injected
`Authorizer` port before anything executes, and a denial is fed back to the
model as an observation, never bypassed.
"""

from server.agent.orchestrator import AgentOrchestrator
from server.agent.proposal import parse_proposal
from server.agent.tasks import LoopState, PendingAction, TaskConflict, TaskStore, UnknownTask

__all__ = [
    "AgentOrchestrator",
    "LoopState",
    "PendingAction",
    "TaskConflict",
    "TaskStore",
    "UnknownTask",
    "parse_proposal",
]
