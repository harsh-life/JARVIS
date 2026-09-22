"""Canonical data contracts (00_CANONICAL_PRD.md §26, 01_DATA_MODEL_SCHEMA.md).

Every first-class Track B entity is defined here as a Pydantic model. This
package is pure data-shape + structural validation: it contains no
authorization, persistence, or business logic. Later branches (security-core
and beyond) build authorization/execution logic *on top of* these shapes;
they do not redefine them here (source-of-truth rule, TRACK_B_ARCHITECTURE_INDEX.md).
"""

from shared.schemas.agent_config import (
    AgentConfiguration,
    ModelConfiguration,
    ToolConfiguration,
    ToolContract,
)
from shared.schemas.capability import CapabilityGrant, PermissionDecision
from shared.schemas.common import ORMBase, ProvenanceLite, VisibilityTriplet
from shared.schemas.errors import ErrorCode, ErrorDetail, ErrorEnvelope
from shared.schemas.graph import Graph, GraphMembership
from shared.schemas.identity import (
    Device,
    Session,
    SessionUserDeviceMismatch,
    User,
    new_session_for_device,
)
from shared.schemas.memory import (
    Mem0Fact,
    VaultDocument,
    VaultQueryRequest,
    VaultQueryResponse,
    VaultQueryResultItem,
)
from shared.schemas.observability import AuditEvent, UsageEvent
from shared.schemas.resources import FileResource, ScheduledJob
from shared.schemas.secret import SecretReference
from shared.schemas.voice import SpeakerContext, VoiceEvent

__all__ = [
    "AgentConfiguration",
    "ModelConfiguration",
    "ToolConfiguration",
    "ToolContract",
    "CapabilityGrant",
    "PermissionDecision",
    "ORMBase",
    "ProvenanceLite",
    "VisibilityTriplet",
    "ErrorCode",
    "ErrorDetail",
    "ErrorEnvelope",
    "Graph",
    "GraphMembership",
    "Device",
    "Session",
    "SessionUserDeviceMismatch",
    "User",
    "new_session_for_device",
    "Mem0Fact",
    "VaultDocument",
    "VaultQueryRequest",
    "VaultQueryResponse",
    "VaultQueryResultItem",
    "AuditEvent",
    "UsageEvent",
    "FileResource",
    "ScheduledJob",
    "SecretReference",
    "SpeakerContext",
    "VoiceEvent",
]
