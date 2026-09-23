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
  available where nothing can execute it.

**Execution branch update (09/10 now exist).** An earlier version of this
module refused *any* tool declaring a filesystem or network requirement,
because no boundary existed yet to enforce what it declared (NET-005, FS-T9:
"a boundary that is not enforced is not a boundary"). `server/fs` and
`server/net` are that boundary now, so the blanket refusal is gone — but the
principle it encoded has not: this module still only *asserts a contract*,
never enforcement. A registered `filesystem`/`network`-declaring tool's
adapter is what must actually route through `server.fs`/`server.net`
(`server/tools/platforms.py` does, for every adapter this branch ships); the
registry has no way to verify that mechanically beyond what the
`import-linter` contract already guarantees (`server.tools` cannot import
`subprocess`/`socket` directly — see `pyproject.toml`), so getting a new
adapter's plumbing right is still a code-review responsibility, not
something registration proves for you.
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger("hypermind.tools.registry")

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

        # A filesystem/network-declaring tool may register now that `server.fs`
        # /`server.net` exist to back the declaration (see this module's
        # docstring) — the blanket refusal that used to sit here is gone.
        # `may_send_credentials` stays refused on its own: `server/net`'s
        # client has no mechanism at all for attaching caller-supplied
        # credentials to a request (10 §6's exfiltration controls are not
        # built), so declaring it would assert a capability nothing can
        # actually back, the same "refused rather than believed" principle
        # TOOL-004 applies to every other over-claimed contract field.
        if _declares_boundary(contract.network, "may_send_credentials"):
            raise ToolRegistrationError(
                f"{tool_id}: may_send_credentials cannot be granted — server.net has no "
                "mechanism to attach a credential to an outbound request (10 §6)"
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

    def release_task(self, task_id) -> None:
        """Give every adapter that holds per-task state the chance to drop it.

        An adapter opts in by defining `release_task(task_id)`. A failure here is
        logged and swallowed: cleanup must never turn a finished task into a
        crashed one, and whatever was left behind is still inside the sandbox
        base root (09 §7's periodic sweep is the backstop).
        """

        seen: set[int] = set()
        for entry in self._entries.values():
            for adapter in entry.adapters.values():
                if id(adapter) in seen:
                    continue
                seen.add(id(adapter))
                release = getattr(adapter, "release_task", None)
                if release is None:
                    continue
                try:
                    release(task_id)
                except Exception:  # noqa: BLE001 — see docstring
                    logger.warning("tool %s: releasing task state failed", entry.handle.tool_id)

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
