"""The internal tool registry (07 §1, TOOL-001..003).

Registration is where Track B asserts a tool's contract rather than trusting it
(TOOL-004: "Track B asserts its schema/capability/risk/boundaries — not the
[tool's] word"). A definition is refused unless:

* it carries a `ToolContract` (TOOL-003) and a unique `tool_id`;
* its `required_capability` is in the closed capability registry and is not an
  absolute-floor name (PERM-006);
* every operation it exposes is in that capability's **enumerated** mapping —
  a tool cannot widen a capability by naming an operation the capability does
  not list (07 §3, TL-T8);
* its declared `risk_category` / `confirmation_required` match what the
  deterministic tier table says about its operations — a contract that
  under-declares its own risk is refused rather than believed;
* each operation's resource binding is well-formed (an operation on an existing
  resource must name it, so D3/D4 apply);
* it has at least one platform adapter — a capability is never presented as
  available where nothing can execute it;
* it declares **no filesystem or network requirement**, unless it is a
  model-tool whose only egress is its configured provider endpoint. `09`/`10`
  do not exist yet, so a tool that needs a filesystem or network boundary would
  run without one. It is refused until the branch that enforces the boundary
  exists (NET-005, FS-T9: a boundary that is not enforced is not a boundary).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Protocol

from server.capabilities.floor import floor_category_for_capability
from server.capabilities.registry import UnknownCapability, UnknownOperation, lookup
from server.capabilities.risk import requires_confirmation
from shared.schemas.agent import (
    ExecutionPlatform,
    OperationSpec,
    ToolHandle,
    ToolInvocation,
    ToolOperationSummary,
    ToolOutput,
    ToolSummary,
)
from shared.schemas.agent_config import ToolContract
from shared.schemas.authorization import Operation, ResourceType
from shared.schemas.enums import RiskCategory

_SEVERITY = {
    RiskCategory.LOW_READ: 0,
    RiskCategory.LOW_WRITE: 1,
    RiskCategory.CONSEQUENTIAL: 2,
    RiskCategory.HIGH_IRREVERSIBLE: 3,
}


class ToolRegistrationError(Exception):
    """A definition failed validation; nothing was registered."""


class ToolAdapter(Protocol):
    """A platform-specific implementation of a tool's operations."""

    async def execute(self, invocation: ToolInvocation) -> ToolOutput: ...


@dataclass(frozen=True)
class ToolDefinition:
    contract: ToolContract
    operations: Mapping[str, OperationSpec]
    adapters: Mapping[ExecutionPlatform, ToolAdapter]
    is_model_tool: bool = False
    projected_cost_per_call: float = 0.0


@dataclass
class _Entry:
    definition: ToolDefinition
    handle: ToolHandle
    enabled: bool
    adapters: Mapping[ExecutionPlatform, ToolAdapter] = field(default_factory=dict)


def _declares_boundary(declaration: Mapping | None, *keys: str) -> bool:
    if not declaration:
        return False
    for key in keys:
        value = declaration.get(key)
        if value:
            return True
    return False


class ToolRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def register(self, definition: ToolDefinition, *, enabled: bool) -> ToolHandle:
        contract = definition.contract
        if not isinstance(contract, ToolContract):
            raise ToolRegistrationError("a tool must publish a ToolContract (TOOL-003)")

        tool_id = contract.tool_id
        if not tool_id or tool_id in self._entries:
            raise ToolRegistrationError(f"tool_id {tool_id!r} is empty or already registered")

        capability = contract.required_capability
        if floor_category_for_capability(capability) is not None:
            raise ToolRegistrationError(
                f"{tool_id}: capability {capability!r} is an absolute-floor action — no "
                "tool may expose it (PERM-006)"
            )
        try:
            capability_definition = lookup(capability)
        except UnknownCapability as exc:
            raise ToolRegistrationError(f"{tool_id}: {exc}") from exc

        if not definition.operations:
            raise ToolRegistrationError(f"{tool_id}: a tool must expose at least one operation")

        tiers: dict[str, RiskCategory] = {}
        for name, spec in definition.operations.items():
            try:
                tiers[name] = capability_definition.risk_for(name)
            except UnknownOperation as exc:
                raise ToolRegistrationError(f"{tool_id}: {exc}") from exc
            _validate_operation_spec(tool_id, name, spec)

        highest = max(tiers.values(), key=_SEVERITY.__getitem__)
        if contract.risk_category is not highest:
            raise ToolRegistrationError(
                f"{tool_id}: contract declares risk {contract.risk_category.value!r} but its "
                f"operations reach {highest.value!r} — a contract may not under- or "
                "mis-declare its own risk"
            )
        if contract.confirmation_required is not requires_confirmation(highest):
            raise ToolRegistrationError(
                f"{tool_id}: contract.confirmation_required disagrees with the "
                "deterministic tier table (PERM-005)"
            )

        if not definition.adapters:
            raise ToolRegistrationError(
                f"{tool_id}: no platform adapter — a capability is never presented as "
                "available where nothing can execute it"
            )
        for platform in definition.adapters:
            if not isinstance(platform, ExecutionPlatform):
                raise ToolRegistrationError(f"{tool_id}: unknown platform {platform!r}")

        if _declares_boundary(contract.filesystem, "roots", "read", "write"):
            raise ToolRegistrationError(
                f"{tool_id}: declares a filesystem requirement, but no filesystem sandbox "
                "(09) exists to enforce it — refused rather than run unbounded"
            )
        if _declares_boundary(
            contract.network, "required", "destinations", "internet", "private_net"
        ) and not definition.is_model_tool:
            raise ToolRegistrationError(
                f"{tool_id}: declares a network requirement, but no egress boundary (10) "
                "exists to enforce it — refused rather than run unbounded"
            )
        if _declares_boundary(contract.network, "may_send_credentials"):
            raise ToolRegistrationError(
                f"{tool_id}: may_send_credentials cannot be granted without 10's "
                "credential-exfiltration controls"
            )

        handle = ToolHandle(
            tool_id=tool_id,
            description=contract.description,
            required_capability=capability,
            operations=MappingProxyType(dict(definition.operations)),
            platforms=frozenset(definition.adapters),
            timeout_seconds=float(contract.timeout_seconds),
            is_model_tool=definition.is_model_tool,
            operation_tiers=MappingProxyType(tiers),
            natural_scope=(
                MappingProxyType({str(k): str(v) for k, v in contract.resource_scope.items()})
                if contract.resource_scope
                else None
            ),
            projected_cost_per_call=max(0.0, float(definition.projected_cost_per_call)),
        )
        self._entries[tool_id] = _Entry(
            definition=definition,
            handle=handle,
            enabled=enabled,
            adapters=MappingProxyType(dict(definition.adapters)),
        )
        return handle

    # ── the runtime's view (ToolCatalog port) ───────────────────────────

    def resolve(self, tool_id: str) -> ToolHandle | None:
        """A registered **and enabled** tool, or `None`. A registered-but-not-
        enabled tool is inert (07 §1)."""

        entry = self._entries.get(tool_id)
        if entry is None or not entry.enabled:
            return None
        return entry.handle

    def adapter(self, tool_id: str, platform: ExecutionPlatform) -> ToolAdapter | None:
        entry = self._entries.get(tool_id)
        if entry is None or not entry.enabled:
            return None
        return entry.adapters.get(platform)

    def enabled_handles(self) -> list[ToolHandle]:
        return [e.handle for e in self._entries.values() if e.enabled]

    def summaries(self) -> list[ToolSummary]:
        return [
            ToolSummary(
                tool_id=h.tool_id,
                description=h.description,
                required_capability=h.required_capability,
                platforms=sorted(h.platforms, key=lambda p: p.value),
                operations=[
                    ToolOperationSummary(operation=name, risk_category=tier)
                    for name, tier in sorted(h.operation_tiers.items())
                ],
            )
            for h in self.enabled_handles()
        ]

    async def run(
        self, tool_id: str, platform: ExecutionPlatform, invocation: ToolInvocation, *, timeout: float
    ) -> ToolOutput:
        from server.tools.executor import run_tool

        adapter = self.adapter(tool_id, platform)
        if adapter is None:
            return ToolOutput(ok=False, error="unsupported_platform")
        return await run_tool(adapter, invocation, timeout=timeout)


def _validate_operation_spec(tool_id: str, name: str, spec: OperationSpec) -> None:
    try:
        resource_type = ResourceType(spec.resource_type)
        operation = Operation(spec.resource_operation)
    except ValueError as exc:
        raise ToolRegistrationError(f"{tool_id}.{name}: {exc}") from exc

    if resource_type is ResourceType.SECRET_REFERENCE:
        raise ToolRegistrationError(
            f"{tool_id}.{name}: a secret reference is never a tool resource — secrets "
            "reach a tool only by handle at its boundary (12 §2)"
        )
    if operation in {Operation.SHARE, Operation.ADMINISTER}:
        raise ToolRegistrationError(
            f"{tool_id}.{name}: sharing and graph administration are owner acts through "
            "their own endpoints, never tool operations"
        )
    if resource_type is ResourceType.TOOL_ACTION and operation is not Operation.CREATE:
        raise ToolRegistrationError(
            f"{tool_id}.{name}: a resource-less tool action is authorized only as CREATE"
        )
    if (operation is Operation.CREATE) == spec.requires_resource_ref:
        raise ToolRegistrationError(
            f"{tool_id}.{name}: an operation on an existing resource must name it "
            "(requires_resource_ref), and a CREATE must not"
        )
