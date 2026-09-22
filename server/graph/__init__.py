"""The authorization engine and graph lifecycle — 04_AUTHORIZATION_GRAPH_RESOURCE.md.

Graph is not a conversation container; it is Track B's authorization boundary
(PRD §11/§11A). This package owns the deterministic decision half of the
engine: `authorization.py` is the single place resource access is decided, and
`readable()` inside it is the single implementation of RAUTH-004's read
predicate. Nothing else in the codebase may re-derive either one — later
subsystems ask this engine (§19 of the security-core scope).

The policy half (capability grants, risk tiers, the absolute floor,
confirmations) is `server/capabilities`, deliberately independent of this
package and consumed through `ports.py` (16 §5).
"""

from server.graph.authorization import (
    AccessRequest,
    AuthorizationEngine,
    AuthorizationOutcome,
    readable,
)
from server.graph.ports import ResourceDescriptor, ResourceLoader
from server.graph.repository import GraphRepository
from server.graph.resources import SecurityCoreResourceLoader
from server.graph.service import GraphOperationRefused, GraphService, VisibilityChange

__all__ = [
    "AccessRequest",
    "AuthorizationEngine",
    "AuthorizationOutcome",
    "GraphOperationRefused",
    "GraphRepository",
    "GraphService",
    "ResourceDescriptor",
    "ResourceLoader",
    "SecurityCoreResourceLoader",
    "VisibilityChange",
    "readable",
]
