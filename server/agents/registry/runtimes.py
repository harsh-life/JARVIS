"""The AgentRuntimeProfile registry (docs/29 §7).

Runtime providers are registered **in code**; an operator can only enable
one that exists here. v1 registers exactly one: the native JARVIS runtime
(docs/29 §7.4) — the existing task runtime (`05`/`18`), in-process, under the
existing authorization path. Every other runtime docs/29 §30 names is a
*reserved id* with no profile: enabling it is a startup failure, because no
provider, no container/netns isolation, no MCP transport and no egress proxy
exist in this repository (docs/29 §21, Phase 6 `[BLOCKED BY INFRASTRUCTURE]`).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from shared.schemas.agent_factory import (
    AgentRuntimeProfile,
    CancellationMode,
    InfraRequirement,
    IsolationMode,
    LifecycleInterface,
    ModelFeature,
    NetworkRequirement,
    Observability,
    PersistenceModel,
    RuntimeType,
    TaskTag,
    ToolInterface,
)

NATIVE_RUNTIME_ID = "native"

NATIVE_RUNTIME = AgentRuntimeProfile(
    runtime_id=NATIVE_RUNTIME_ID,
    runtime_type=RuntimeType.NATIVE,
    version_pin="jarvis-native-1",
    # Browser and coding classes need a contained workspace (docs/29 §21.1 P2).
    supported_template_tags=frozenset(TaskTag) - {TaskTag.BROWSER_AUTOMATION, TaskTag.CODING_MAINTENANCE},
    # The native loop drives a text chat model through JSON proposals.
    supported_model_features=frozenset({
        ModelFeature.AGENTIC_REASONING, ModelFeature.TOOL_CALLING, ModelFeature.STRUCTURED_OUTPUT,
        ModelFeature.LONG_CONTEXT, ModelFeature.CODING_SUITABLE, ModelFeature.WRITING,
        ModelFeature.DOCUMENT_STRUCTURED,
    }),
    tool_interface=ToolInterface.IN_PROCESS_TOOL_CATALOG,
    lifecycle_interface=LifecycleInterface.TASK_RUNTIME,
    persistence_model=PersistenceModel.VOLATILE_TASK,
    isolation_mode=IsolationMode.IN_PROCESS,
    network_requirements=NetworkRequirement.NONE,
    observability=Observability.FULL_TRACE,
    cancellation=CancellationMode.COOPERATIVE_EVENT,
    export_supported=True,
    deprovision_supported=True,
    human_approval_mode="jarvis_gateway",
    known_limitations=("in-process: inherits the OD-A1 residual unchanged",),
    required_infrastructure=(InfraRequirement.NONE,),
    enabled=False,
    display_name="JARVIS built-in runtime",
)

REGISTERED_RUNTIMES: Mapping[str, AgentRuntimeProfile] = MappingProxyType({NATIVE_RUNTIME_ID: NATIVE_RUNTIME})

# docs/29 §7.2 / §30 — reserved, no provider in this build.
RESERVED_RUNTIME_IDS = frozenset({
    "letta", "openhands", "browser_use", "openai_agents", "google_adk", "langgraph", "pydantic_ai", "openclaw",
})

__all__ = ["NATIVE_RUNTIME", "NATIVE_RUNTIME_ID", "REGISTERED_RUNTIMES", "RESERVED_RUNTIME_IDS"]
