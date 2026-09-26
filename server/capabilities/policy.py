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

from typing import Mapping

from server.capabilities import floor as floor_module
from server.capabilities.app_classification import AppClassification
from server.capabilities import risk as risk_module
from server.capabilities.registry import CapabilityDefinition, UnknownCapability, lookup
from shared.schemas.authorization import Operation, ResourceType
from shared.schemas.enums import RiskCategory


class RiskPolicyAdapter:
    """Satisfies `server.graph.ports.RiskPolicy`.

    `classification` is the operator's sensitive-app classification
    (`android.app_classification`); the default — nothing classified — denies
    every UI-acting device operation (docs/23 §5.5)."""

    def __init__(self, classification: AppClassification | None = None) -> None:
        self._classification = classification or AppClassification()

    def scope_denial(
        self,
        *,
        capability_name: str | None,
        capability_operation: str | None,
        resource_scope: Mapping[str, str] | None,
    ) -> str | None:
        return self._classification.constraint(
            capability=capability_name,
            capability_operation=capability_operation,
            resource_scope=resource_scope,
        ).denial

    def risk_tier(
        self,
        *,
        resource_type: ResourceType,
        operation: Operation,
        capability_name: str | None,
        capability_operation: str | None,
        resource_scope: Mapping[str, str] | None = None,
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

        tier = risk_module.risk_tier(
            resource_type=resource_type,
            operation=operation,
            capability=definition,
            capability_operation=capability_operation if definition is not None else None,
        )
        # The sensitive-app floor only ever raises a tier (the more restrictive
        # of the two, like every other axis in `risk.risk_tier`).
        minimum = self._classification.constraint(
            capability=capability_name,
            capability_operation=capability_operation,
            resource_scope=resource_scope,
        ).minimum_tier
        return risk_module.more_restrictive(tier, minimum) if minimum is not None else tier

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
