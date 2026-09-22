"""Canonical enum registry — the single source of truth for every enum value
used anywhere in Track B.

Source: 01_DATA_MODEL_SCHEMA.md §1.2 ("Enum registry [LOCKED] ... An enum
value not in this registry is invalid"), ratified by 00_CANONICAL_PRD.md §26
("`01` §1.2 is the single canonical list of every enum value used anywhere
in the package ... extending it is an owner-approved edit to `01`, never a
silent addition by a subsystem doc.").

Do NOT duplicate these definitions elsewhere. Do NOT add a value here that
is not already present in 01 §1.2 without an explicit owner-approved edit to
that document (see module docstring warning below for the one place this
repo currently declines to do that).
"""

from __future__ import annotations

from enum import Enum


class Visibility(str, Enum):
    """RAUTH-003/004/005 — private-vs-graph-shared visibility triplet."""

    PRIVATE = "private"
    GRAPH = "graph"


class UserStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class DevicePlatform(str, Enum):
    ANDROID = "android"  # MVP; others [FUTURE]


class GraphType(str, Enum):
    PRIVATE = "private"
    SHARED = "shared"


class MembershipRole(str, Enum):
    """MVP roles only (OD-E1: richer roles are [FUTURE], enum extends
    without a schema break)."""

    OWNER = "owner"
    MEMBER = "member"


class FactType(str, Enum):
    """EMO-002 structural guard: a Mem0Fact needing a type outside this
    enum is rejected, not coerced — this is what keeps emotional/relationship
    content out of memory."""

    PREFERENCE = "preference"
    PAST_REQUEST = "past_request"
    STATED_GOAL = "stated_goal"


class AgentConfigScopeType(str, Enum):
    USER = "user"
    GRAPH = "graph"


class CapabilityScopeType(str, Enum):
    USER = "user"
    GRAPH = "graph"
    DEVICE = "device"
    SESSION = "session"
    TASK = "task"


class PermissionDecisionValue(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_CONFIRMATION = "require_confirmation"


class RiskCategory(str, Enum):
    LOW_READ = "low_read"
    LOW_WRITE = "low_write"
    CONSEQUENTIAL = "consequential"
    HIGH_IRREVERSIBLE = "high_irreversible"


# tool.risk_category is explicitly "same as risk_category" (01 §1.2) — reuse
# the same enum rather than declaring a duplicate registry entry.
ToolRiskCategory = RiskCategory


class SecretClass(str, Enum):
    MODEL_API_KEY = "model_api_key"
    OAUTH_TOKEN = "oauth_token"
    DEVICE_CREDENTIAL = "device_credential"
    MASTER_KEY = "master_key"
    OTHER = "other"


class SecretOwnerScopeType(str, Enum):
    USER = "user"
    GRAPH = "graph"
    SERVER = "server"


class AuditActor(str, Enum):
    USER = "user"
    AGENT = "agent"
    TOOL = "tool"
    SYSTEM = "system"
    SUPERUSER = "superuser"


class AuditResult(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    BLOCKED = "blocked"


class UsageKind(str, Enum):
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"

    # NOTE: `usage.kind: decision_call` is proposed by
    # 26_DECISION_PROVIDER.md §25A as a candidate registry extension, but
    # 00_CANONICAL_PRD.md §25A/§47 (OD-DP-9) is explicit that nothing in `26`
    # is ratified and the PRD does not apply the extension itself. Per this
    # branch's instructions ("DO NOT add a decision-model abstraction merely
    # because the document exists"), that value is deliberately NOT added
    # here. Surfacing, not silently choosing (see 00 §6/§19 discipline).


class ModelProvider(str, Enum):
    OLLAMA = "ollama"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    DEEPSEEK = "deepseek"
    GROQ = "groq"
    OPENAI_COMPATIBLE = "openai_compatible"
    CUSTOM = "custom"


class JobStatus(str, Enum):
    ACTIVE = "active"
    CANCELLED = "cancelled"
    FIRED = "fired"
