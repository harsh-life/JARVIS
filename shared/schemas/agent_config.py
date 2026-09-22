"""Runtime-configuration entities — AgentConfiguration, ModelConfiguration,
ToolConfiguration, ToolContract.

Source: 01_DATA_MODEL_SCHEMA.md §4, §9, §10.

IMPORTANT (this branch's explicit instruction, §5): "AgentConfiguration is
configuration ... Do not encode privileges merely by putting them on
AgentConfiguration." `granted_capabilities` below is a *resolved, read-only
cache* of capability strings for the agent runtime to consult — the
authoritative grant record is `CapabilityGrant` (capability.py). Nothing in
this module creates, checks, or revokes a grant.

Foundation does NOT implement: ModelProvider adapters (06), the tool
registry/execution machinery (07), or capability enforcement. It represents
the data shapes those later branches will read and write.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import Field

from shared.schemas.common import ORMBase, utcnow
from shared.schemas.enums import AgentConfigScopeType, ModelProvider, RiskCategory


class ModelConfiguration(ORMBase):
    """PRD MODEL-001..005. Embedded inside AgentConfiguration.primary_model
    and inside model-tool ToolConfiguration entries — not a standalone table
    (01 §9.1: "Embedded in AgentConfiguration.primary_model and in
    model-tool entries").

    `secret_ref` is a handle only (SECRET-004) — never a literal API key.
    """

    provider: ModelProvider
    model: str
    endpoint: str | None = None
    secret_ref: str | None = None
    timeout_seconds: int = Field(gt=0)
    retry: dict | None = None
    generation_policy: dict | None = None


class ToolConfiguration(ORMBase):
    """PRD TOOL-001..004 (01 §9.3) — the *enabled configuration* of a tool
    instance. Its contract lives separately in ToolContract below."""

    tool_id: str
    enabled: bool = False
    config: dict | None = None
    secret_ref: str | None = None
    overrides: dict | None = None


class ToolContract(ORMBase):
    """PRD TOOL-002 (01 §10) — the machine-readable contract every tool
    must publish before it is callable (TOOL-003: a tool with no
    ToolContract cannot be registered or run).

    Foundation defines this shape only; nothing in this branch registers,
    validates, or enforces a real ToolContract. The `network`/`filesystem`
    declarations are enforced by 09/10, not trusted from the tool itself —
    foundation builds none of that enforcement.
    """

    tool_id: str
    version: str
    description: str
    input_schema: dict
    output_schema: dict
    required_capability: str
    resource_scope: dict | None = None
    network: dict
    filesystem: dict
    risk_category: RiskCategory
    timeout_seconds: int = Field(gt=0)
    retry_policy: dict | None = None
    rate_limits: dict | None = None
    confirmation_required: bool
    failure_behavior: str
    audit: str


class AgentConfiguration(ORMBase):
    """PRD AGENT-001, MODEL-001, §6 (01 §4.1).

    Resolution precedence when both a user-scope and graph-scope config
    exist for a request is [IMPL, constrained] per 01 §4.1: it "MUST be
    deterministic and documented in 05_AGENT_RUNTIME.md" — that is
    05_AGENT_RUNTIME's job (agent runtime branch), not foundation's. This
    schema deliberately implements no resolution logic (see
    OD-RT-3 in 00_CANONICAL_PRD.md §47).
    """

    config_id: UUID = Field(default_factory=uuid4)
    scope_type: AgentConfigScopeType
    scope_id: UUID
    primary_model: ModelConfiguration
    model_tools: list[str] | None = None
    enabled_tools: list[str] | None = None
    granted_capabilities: list[str] | None = None
    updated_at: datetime = Field(default_factory=utcnow)
