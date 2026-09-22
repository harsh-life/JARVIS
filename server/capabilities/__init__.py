"""Capability, risk, floor, and confirmation policy — 07_TOOL_CAPABILITY_EXECUTION.md.

This package is the *policy* half of the authorization engine; `server/graph`
is the *decision* half. 16 §5 treats them as one engine but the two are kept
mutually independent modules on purpose: the half that evaluates whether a
capability is granted must not be able to grant one. `server/graph` therefore
consumes this package through Protocols (`server/graph/ports.py`), wired at the
gateway composition root, and neither imports the other.

Nothing in this package executes anything. Tool registration, dispatch, and
the filesystem/network/device boundaries of 07 §5 belong to the branches that
own `09`/`10`/`08`; what is here is only the deterministic policy those
branches must pass through.
"""

from server.capabilities.confirmation import (
    DEFAULT_CONFIRMATION_TTL,
    ActionBinding,
    ConfirmationRefused,
    ConfirmationService,
    IssuedConfirmation,
)
from server.capabilities.floor import (
    AbsoluteFloorCategory,
    AbsoluteFloorViolation,
    assert_not_absolute_floor,
    floor_category_for_capability,
    floor_category_for_request,
)
from server.capabilities.grants import CapabilityGrantRefused, CapabilityGrantService
from server.capabilities.registry import (
    CAPABILITY_REGISTRY,
    CapabilityDefinition,
    UnknownCapability,
    UnknownOperation,
    is_registered,
    lookup,
)
from server.capabilities.risk import (
    Disposition,
    disposition,
    requires_confirmation,
    resource_operation_tier,
    risk_tier,
)
from shared.schemas.authorization import CapabilityCheckContext

__all__ = [
    "CAPABILITY_REGISTRY",
    "DEFAULT_CONFIRMATION_TTL",
    "AbsoluteFloorCategory",
    "AbsoluteFloorViolation",
    "ActionBinding",
    "CapabilityCheckContext",
    "CapabilityDefinition",
    "CapabilityGrantRefused",
    "CapabilityGrantService",
    "ConfirmationRefused",
    "ConfirmationService",
    "Disposition",
    "IssuedConfirmation",
    "UnknownCapability",
    "UnknownOperation",
    "assert_not_absolute_floor",
    "disposition",
    "floor_category_for_capability",
    "floor_category_for_request",
    "is_registered",
    "lookup",
    "requires_confirmation",
    "resource_operation_tier",
    "risk_tier",
]
