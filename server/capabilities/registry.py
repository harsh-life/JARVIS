"""The capability registry — a closed allow-list of what can be granted.

Source: PRD §16 (`PERM-001..003`, `TOOL-001..003`, `AND-006`), 07 §2/§3.

The rule this module exists to make true (07 §3, `[LOCKED]`):

> capability names that *sound* like universal CRUD (`file.read`) do **not**
> grant universal CRUD — they grant exactly the operations their mapping
> enumerates. An operation not in the mapping is not executable, even with
> the capability.

So a capability is not a string that gets compared; it is a registry entry
carrying an **enumerated** operation set, and every operation carries its own
deterministic risk tier. Two consequences that matter more than the data:

* **The registry is closed.** An unregistered capability cannot be granted and
  cannot gate an operation — it is denied, not defaulted. That closure is what
  makes PERM-006's "prohibition by absence" real (see `floor.py`).
* **Nothing here is a privilege.** A registry entry describes what *could* be
  granted. Authority comes only from an explicit `CapabilityGrant`
  (`grants.py`) created by a user's consent, and even then never bypasses
  visibility (07 §2, 04 §2 D5).

**`[IMPL]`, pending owner ratification (OD-TOOL-1).** 07 §9 leaves "exact tier
assignment per operation" to the implementer with the note *"owner signs the
tier table"*, and `[LOCKED]`s only the properties: the policy is a
deterministic table, every operation has a tier, and consequential/
irreversible always require confirmation. The six capability *names* below are
the ones that appear verbatim in the canonical documents (PRD §16:
`app.interact`, `file.read`, `file.write`, `device.read`, `device.ui_control`,
`system.restricted`), and `app.interact`'s operation set is transcribed from
07 §3's own example. The remaining operation sets and every tier assignment
are this branch's proposal, presented for the owner's signature — not as
ratified canon. Tool families named in 07 §1 that have no canonical capability
string yet (memory, vault, scheduler, network) are deliberately **absent**
rather than invented; the branch that owns each one adds its entry with the
owner's tier sign-off.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from shared.schemas.enums import RiskCategory


class UnknownCapability(Exception):
    """The capability is not in the registry.

    Fail-closed (FAIL-CORE-003): an unrecognised capability is a denial, never
    an allow and never a "default tier". This is also what an attempt at an
    unenumerated operation reaches.
    """


class UnknownOperation(Exception):
    """The operation is not in this capability's enumerated mapping (07 §3)."""


@dataclass(frozen=True)
class CapabilityDefinition:
    """One grantable capability and its complete operation mapping."""

    name: str
    description: str
    # 07 §3: the enumerated operation set. Each entry maps an operation name to
    # its deterministic risk tier (PERM-004/005).
    operations: Mapping[str, RiskCategory]
    # 07 §2: the `resource_scope` keys a grant of this capability may narrow
    # itself by (e.g. *which* app, *which* sandbox root). A grant carrying a
    # scope key not listed here is rejected — an unrecognised narrowing
    # dimension is not silently ignored, because ignoring it would widen the
    # grant beyond what the user consented to.
    scope_keys: frozenset[str] = frozenset()

    def risk_for(self, operation: str) -> RiskCategory:
        try:
            return self.operations[operation]
        except KeyError as exc:
            raise UnknownOperation(
                f"operation {operation!r} is not in the enumerated mapping for "
                f"capability {self.name!r} (07 §3) — holding the capability does "
                f"not make it executable"
            ) from exc


def _registry() -> Mapping[str, CapabilityDefinition]:
    definitions = (
        CapabilityDefinition(
            name="app.interact",
            description=(
                "Compose UI interactions within a named app (PRD §13's per-app "
                "grid). Operation set transcribed from 07 §3."
            ),
            # 07 §3: "{ tap, swipe, input_text, read_screen_element,
            # launch_activity }". Tiers per PRD §14: launching an app is
            # automatic; entering text on someone's behalf is not, because it
            # is the step that composes into "send this message".
            operations=MappingProxyType(
                {
                    "tap": RiskCategory.LOW_WRITE,
                    "swipe": RiskCategory.LOW_WRITE,
                    "input_text": RiskCategory.CONSEQUENTIAL,
                    "read_screen_element": RiskCategory.LOW_READ,
                    "launch_activity": RiskCategory.LOW_WRITE,
                }
            ),
            scope_keys=frozenset({"package_name"}),
        ),
        CapabilityDefinition(
            name="device.read",
            description="Read device state the user has exposed (PRD §13 'screen-read').",
            operations=MappingProxyType(
                {
                    "read_screen": RiskCategory.LOW_READ,
                    "read_battery": RiskCategory.LOW_READ,
                    "read_notification": RiskCategory.LOW_READ,
                }
            ),
            scope_keys=frozenset({"package_name"}),
        ),
        CapabilityDefinition(
            name="device.ui_control",
            description="Drive device UI outside a single app's scope (PRD §13 'UI-interaction').",
            operations=MappingProxyType(
                {
                    "tap": RiskCategory.LOW_WRITE,
                    "swipe": RiskCategory.LOW_WRITE,
                    "input_text": RiskCategory.CONSEQUENTIAL,
                    "global_action": RiskCategory.CONSEQUENTIAL,
                }
            ),
            scope_keys=frozenset({"package_name"}),
        ),
        CapabilityDefinition(
            name="file.read",
            description=(
                "Read files inside a granted sandbox root. Never a bypass of "
                "visibility — 07 §2/04 D4 still gate every path (AZ-T7/TL-T3)."
            ),
            operations=MappingProxyType(
                {
                    "read_file": RiskCategory.LOW_READ,
                    "list_directory": RiskCategory.LOW_READ,
                    "stat": RiskCategory.LOW_READ,
                }
            ),
            scope_keys=frozenset({"sandbox_root"}),
        ),
        CapabilityDefinition(
            name="file.write",
            description="Create/modify files inside a granted sandbox root.",
            operations=MappingProxyType(
                {
                    "write_file": RiskCategory.LOW_WRITE,
                    "create_file": RiskCategory.LOW_WRITE,
                    # A delete is not recoverable from inside the sandbox, so it
                    # is never automatic even though its sibling writes are.
                    "delete_file": RiskCategory.CONSEQUENTIAL,
                    "bulk_delete": RiskCategory.HIGH_IRREVERSIBLE,
                }
            ),
            scope_keys=frozenset({"sandbox_root"}),
        ),
        CapabilityDefinition(
            name="system.restricted",
            description=(
                "The high-risk family kept deliberately separate (07 §2, 08's "
                "'shell as a separate isolated high-risk capability'). Every "
                "operation is irreversible-tier; no operation in it is ever "
                "automatic."
            ),
            operations=MappingProxyType(
                {
                    "run_shell_command": RiskCategory.HIGH_IRREVERSIBLE,
                }
            ),
            scope_keys=frozenset(),
        ),
    )
    return MappingProxyType({d.name: d for d in definitions})


CAPABILITY_REGISTRY: Mapping[str, CapabilityDefinition] = _registry()


def lookup(capability: str) -> CapabilityDefinition:
    """Resolve a capability, or fail closed.

    Note the ordering relationship with `floor.py`: a floor-shaped capability
    is *also* absent from this registry, so both the closed allow-list and the
    named backstop independently refuse it.
    """

    try:
        return CAPABILITY_REGISTRY[capability]
    except KeyError as exc:
        raise UnknownCapability(
            f"capability {capability!r} is not in the registry — an unregistered "
            f"capability grants nothing (07 §1/§7)"
        ) from exc


def is_registered(capability: str) -> bool:
    return capability in CAPABILITY_REGISTRY


def validate_resource_scope(capability: str, resource_scope: Mapping | None) -> None:
    """Reject a grant whose `resource_scope` narrows by an unknown dimension.

    Silently ignoring an unrecognised key would grant *more* than the user
    asked for: a client that means "only this app" and misspells the key would
    otherwise receive an unscoped grant.
    """

    if not resource_scope:
        return

    definition = lookup(capability)
    unknown = set(resource_scope) - set(definition.scope_keys)
    if unknown:
        raise UnknownOperation(
            f"capability {capability!r} cannot be scoped by {sorted(unknown)!r}; "
            f"it accepts {sorted(definition.scope_keys)!r}"
        )
