"""The absolute floor — prohibition by absence (PERM-006, 07 §7, 04 §2).

PRD §16 `[LOCKED]`:

> the absolute-floor prohibitions (obtain superuser creds, read another
> user's private graph/secrets, disable auth/audit, escape sandbox, obtain
> master keys, self-escalate, exfiltrate credentials) exist **because no
> capability grants them and no tool exposes them** — there is nothing to
> bypass.

So the primary enforcement here is **not** this module's deny-list. It is
`server/capabilities/registry.py` being a *closed allow-list*: a capability
that is not in the registry cannot be granted and cannot gate an operation,
and no registry entry describes any of the seven categories. An attacker has
nothing to reach for.

This module is the named, auditable backstop behind that absence, and it does
two jobs the absence alone cannot:

1. **Grant-creation refusal (DM-T9).** `01` §7.1 requires creating a
   `CapabilityGrant` for an absolute-floor capability to be a hard error. A
   bare "unknown capability" rejection would satisfy the letter of that, but
   it would log an escalation attempt as a typo. Recognising the seven
   categories by name means the attempt is *classified and audited* as what
   it is.
2. **The 04 §2 gate.** The engine runs an absolute-floor check after all five
   dimensions pass, as defense in depth for a request that somehow arrives
   with a floor-shaped capability. 04 §2 expects this to be structurally
   unreachable; it is implemented anyway, and returns `prohibited`.

`[LOCKED]` (PRD §15, 07 §7, 05 §4) a floor action is **never** offered as
confirmable. It does not fall through to "are you sure?" — see
`server/capabilities/confirmation.py`, which refuses to mint a token for one.

**Provenance of the concrete strings, stated plainly.** The seven *categories*
below are ratified verbatim from PRD §16. The reserved capability *names* and
namespaces that map onto them are an `[IMPL]` choice made here, because no
canonical document enumerates concrete floor capability strings — `01` §7.1
types `CapabilityGrant.capability` as a bare string and `07`'s OD-TOOL-1
leaves the tier/name table for the owner to sign. Foundation flagged exactly
this gap rather than inventing a list
(`shared/schemas/capability.py`'s module docstring). This branch owns
PERM-006, so it fills the gap the only way that keeps the guarantee real — a
closed allow-list plus a named backstop — and flags the naming for owner
ratification under OD-TOOL-1 rather than presenting it as canonical.
"""

from __future__ import annotations

from enum import Enum

from shared.schemas.authorization import Operation, ResourceType


class AbsoluteFloorCategory(str, Enum):
    """The seven prohibitions, verbatim from PRD §16 / 07 §7 / 04 §2."""

    OBTAIN_SUPERUSER_CREDENTIALS = "obtain_superuser_credentials"
    READ_ANOTHER_USERS_PRIVATE_DATA = "read_another_users_private_data"
    DISABLE_AUTH_OR_AUDIT = "disable_auth_or_audit"
    ESCAPE_SANDBOX = "escape_sandbox"
    OBTAIN_MASTER_KEYS = "obtain_master_keys"
    SELF_ESCALATE = "self_escalate"
    EXFILTRATE_CREDENTIALS = "exfiltrate_credentials"


class AbsoluteFloorViolation(Exception):
    """A floor-category action was requested.

    `category` is recorded so the audit entry says which prohibition was
    attempted — 04 §2 calls for a "high-priority audit" on this path.
    """

    def __init__(self, category: AbsoluteFloorCategory, capability: str | None = None) -> None:
        super().__init__(
            f"prohibited: {category.value}"
            + (f" (via capability {capability!r})" if capability else "")
        )
        self.category = category
        self.capability = capability


# `[IMPL]`, pending owner ratification under OD-TOOL-1 — see module docstring.
# Reserved *exact* capability names. A grant for any of these is refused and
# classified, never created.
_RESERVED_CAPABILITY_NAMES: dict[str, AbsoluteFloorCategory] = {
    "superuser": AbsoluteFloorCategory.OBTAIN_SUPERUSER_CREDENTIALS,
    "superuser.assume": AbsoluteFloorCategory.OBTAIN_SUPERUSER_CREDENTIALS,
    "secret.master_key": AbsoluteFloorCategory.OBTAIN_MASTER_KEYS,
    "secret.read_raw": AbsoluteFloorCategory.EXFILTRATE_CREDENTIALS,
    "secret.export": AbsoluteFloorCategory.EXFILTRATE_CREDENTIALS,
    "audit.disable": AbsoluteFloorCategory.DISABLE_AUTH_OR_AUDIT,
    "auth.disable": AbsoluteFloorCategory.DISABLE_AUTH_OR_AUDIT,
    "authz.bypass": AbsoluteFloorCategory.DISABLE_AUTH_OR_AUDIT,
    "capability.self_grant": AbsoluteFloorCategory.SELF_ESCALATE,
    "capability.escalate": AbsoluteFloorCategory.SELF_ESCALATE,
    "sandbox.escape": AbsoluteFloorCategory.ESCAPE_SANDBOX,
    "fs.host_root": AbsoluteFloorCategory.ESCAPE_SANDBOX,
    "graph.read_private": AbsoluteFloorCategory.READ_ANOTHER_USERS_PRIVATE_DATA,
    "user.impersonate": AbsoluteFloorCategory.READ_ANOTHER_USERS_PRIVATE_DATA,
}

# Reserved *namespaces*: any capability under one of these prefixes is a floor
# action, so a near-miss spelling ("secret.master_key.read", "superuser.x")
# cannot slip past the exact-name table above.
_RESERVED_NAMESPACES: tuple[tuple[str, AbsoluteFloorCategory], ...] = (
    ("superuser.", AbsoluteFloorCategory.OBTAIN_SUPERUSER_CREDENTIALS),
    ("secret.master_key.", AbsoluteFloorCategory.OBTAIN_MASTER_KEYS),
    ("secret.raw", AbsoluteFloorCategory.EXFILTRATE_CREDENTIALS),
    ("audit.disable", AbsoluteFloorCategory.DISABLE_AUTH_OR_AUDIT),
    ("auth.disable", AbsoluteFloorCategory.DISABLE_AUTH_OR_AUDIT),
    ("authz.bypass", AbsoluteFloorCategory.DISABLE_AUTH_OR_AUDIT),
    ("sandbox.escape", AbsoluteFloorCategory.ESCAPE_SANDBOX),
    ("capability.self_grant", AbsoluteFloorCategory.SELF_ESCALATE),
    ("capability.escalate", AbsoluteFloorCategory.SELF_ESCALATE),
)


def floor_category_for_capability(capability: str | None) -> AbsoluteFloorCategory | None:
    """Classify a capability string, or `None` if it is not a floor action.

    Case- and whitespace-insensitive, because the comparison must not be
    defeatable by presentation.
    """

    if not capability:
        return None

    normalized = capability.strip().lower()
    if normalized in _RESERVED_CAPABILITY_NAMES:
        return _RESERVED_CAPABILITY_NAMES[normalized]

    for prefix, category in _RESERVED_NAMESPACES:
        if normalized.startswith(prefix):
            return category

    return None


def floor_category_for_request(
    *,
    capability: str | None,
    operation: Operation,
    resource_type: ResourceType,
) -> AbsoluteFloorCategory | None:
    """The 04 §2 absolute-floor gate.

    Two structural prohibitions are recognised here in addition to the
    capability-name classification:

    * **Sharing a secret reference** (GRAPH-009, 04 §5, AZ-T5). "Sharing a
      graph, or sharing a resource into a graph, never shares secrets"; the
      engine must refuse it as `prohibited`, not merely deny it.
    * **Any mutation of a secret reference through the resource-authorization
      path.** Secret lifecycle goes through the SecretStore's own mediated
      interface (12 §1/§2), which checks scope and class. A write/delete
      reaching a secret by way of the generic resource engine would be a
      second, unmediated door onto credential material.
    """

    category = floor_category_for_capability(capability)
    if category is not None:
        return category

    if resource_type is ResourceType.SECRET_REFERENCE and operation in {
        Operation.SHARE,
        Operation.WRITE,
        Operation.DELETE,
        Operation.CREATE,
        Operation.ADMINISTER,
    }:
        return AbsoluteFloorCategory.EXFILTRATE_CREDENTIALS

    return None


def assert_not_absolute_floor(capability: str | None) -> None:
    """Guard for grant creation (DM-T9, `01` §7.1, 02 §6).

    `[LOCKED]` no `CapabilityGrant` for a floor capability is ever created —
    `POST /capabilities` returns `prohibited` and writes nothing.
    """

    category = floor_category_for_capability(capability)
    if category is not None:
        raise AbsoluteFloorViolation(category, capability)
