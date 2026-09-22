"""Adapters that present this package's policy as the engine's ports.

`server/graph/ports.py` declares `RiskPolicy` and `FloorPolicy` in terms the
engine has to hand — a capability *name*, not a registry object — because the
engine cannot import this package (16 §5). These two classes are the translation,
and they live here rather than in the gateway so the lookup that turns a name
into its enumerated operation set stays inside the module that owns the registry.

Structural typing only: neither class inherits from or imports the Protocols
they satisfy, so the two halves of the engine remain mutually independent.
"""

from __future__ import annotations

from server.capabilities import floor as floor_module
from server.capabilities import risk as risk_module
from server.capabilities.registry import CapabilityDefinition, UnknownCapability, lookup
from shared.schemas.authorization import Operation, ResourceType
from shared.schemas.enums import RiskCategory


class RiskPolicyAdapter:
    """Satisfies `server.graph.ports.RiskPolicy`."""

    def risk_tier(
        self,
        *,
        resource_type: ResourceType,
        operation: Operation,
        capability_name: str | None,
        capability_operation: str | None,
    ) -> RiskCategory:
        """Resolve the deterministic tier for one action.

        An unregistered capability yields the resource-operation tier alone
        rather than raising: by the time the engine computes a tier, D5 has
        already denied any request whose capability is unregistered, so this
        path is only reachable for a request that needs no capability. Raising
        here would turn a fail-closed denial into a fail-closed *crash*, which
        the engine would then have to catch as "authorization_unavailable" and
        report less precisely.
        """

        definition: CapabilityDefinition | None = None
        if capability_name:
            try:
                definition = lookup(capability_name)
            except UnknownCapability:
                definition = None

        return risk_module.risk_tier(
            resource_type=resource_type,
            operation=operation,
            capability=definition,
            capability_operation=capability_operation if definition is not None else None,
        )

    def requires_confirmation(self, tier: RiskCategory) -> bool:
        return risk_module.requires_confirmation(tier)


class FloorPolicyAdapter:
    """Satisfies `server.graph.ports.FloorPolicy`."""

    def floor_category_for_request(
        self,
        *,
        capability: str | None,
        operation: Operation,
        resource_type: ResourceType,
    ) -> str | None:
        category = floor_module.floor_category_for_request(
            capability=capability,
            operation=operation,
            resource_type=resource_type,
        )
        return category.value if category is not None else None
