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

BROWSER_USE_RUNTIME_ID = "browser_use"

# OD-AF-6 (owner, 2026-10-02): Browser Use, P2 contained workspace — untrusted
# execution infrastructure in a rootless gVisor container per run (OD-AF-11),
# no network interface but its two sockets (OD-AF-12), JARVIS's egress proxy
# (OD-AF-13). Disabled until the operator enables it, and it can be enabled
# only with its infrastructure and a pinned image (registry checks below).
BROWSER_USE_RUNTIME = AgentRuntimeProfile(
    runtime_id=BROWSER_USE_RUNTIME_ID,
    runtime_type=RuntimeType.SPECIALIZED_RUNTIME,
    version_pin="browser-use==0.13.10",
    supported_template_tags=frozenset({TaskTag.BROWSER_AUTOMATION, TaskTag.MONITORING, TaskTag.EXTRACTION}),
    supported_model_features=frozenset({ModelFeature.AGENTIC_REASONING, ModelFeature.STRUCTURED_OUTPUT,
                                        ModelFeature.LONG_CONTEXT, ModelFeature.BROWSER_SUITABLE}),
    tool_interface=ToolInterface.CONTAINED_WORKSPACE,
    lifecycle_interface=LifecycleInterface.SDK_IN_WORKER_PROCESS,
    persistence_model=PersistenceModel.WORKSPACE_FILES,
    isolation_mode=IsolationMode.CONTAINER_NETNS,
    network_requirements=NetworkRequirement.JARVIS_GATEWAY_PLUS_EGRESS_PROXY,
    observability=Observability.GATEWAY_TRACE_ONLY,
    cancellation=CancellationMode.CONTAINER_KILL,
    export_supported=False,
    deprovision_supported=True,
    human_approval_mode="jarvis_gateway",
    known_limitations=(
        "the egress proxy decides hosts, not methods: no TLS interception (OD-AF-13)",
        "use_vision off and no stored credentials in v1",
    ),
    required_infrastructure=(InfraRequirement.CONTAINER, InfraRequirement.NETNS, InfraRequirement.BROWSER_SANDBOX),
    enabled=False,
    display_name="Browser Use (contained browser)",
)

# OD-AF-14: the image CI builds and publishes (.github/workflows/runtime-image.yml),
# pinned here by its immutable digest. None until the first publish is pinned
# by a reviewed PR: until then the runtime cannot be enabled.
#
# Pinned by this PR. Built, smoke-tested and published by `runtime-image.yml`
# on the push-to-main run triggered by PR #42's merge (commit
# f482fdb903beef4ea7fa11cd66246e355530c85a):
# https://github.com/harsh-life/JARVIS/actions/runs/37094303417/job/111120955670
# — the digest printed to that job's own step summary, which GitHub's API
# does not expose to this session; copied here from that summary.
BROWSER_USE_IMAGE: str | None = (
    "ghcr.io/harsh-life/jarvis-browser-use@sha256:0d1d6c18d48a21e28ca180a309e655e1f80fada301911322285a5e48c1a7cb18"
)

REGISTERED_RUNTIMES: Mapping[str, AgentRuntimeProfile] = MappingProxyType({
    NATIVE_RUNTIME_ID: NATIVE_RUNTIME,
    BROWSER_USE_RUNTIME_ID: BROWSER_USE_RUNTIME,
})

# docs/29 §7.2 / §30 — reserved, no provider in this build.
RESERVED_RUNTIME_IDS = frozenset({
    "letta", "openhands", "openai_agents", "google_adk", "langgraph", "pydantic_ai", "openclaw",
})

__all__ = ["BROWSER_USE_IMAGE", "BROWSER_USE_RUNTIME", "BROWSER_USE_RUNTIME_ID", "NATIVE_RUNTIME", "NATIVE_RUNTIME_ID",
           "REGISTERED_RUNTIMES", "RESERVED_RUNTIME_IDS"]
