"""Authorization & capability entities — CapabilityGrant, PermissionDecision.

Source: 01_DATA_MODEL_SCHEMA.md §7.

Foundation represents these record shapes only. It does NOT implement:
  - what a capability string actually authorizes (07_TOOL_CAPABILITY_EXECUTION.md),
  - the deterministic risk-tier/confirmation policy that produces a
    PermissionDecision (07, 00_CANONICAL_PRD.md §15), or
  - the absolute-floor prohibition list.

OPEN AMBIGUITY — flagged rather than silently resolved (per this branch's
own instruction, §6/§19):
01_DATA_MODEL_SCHEMA.md §7.1 states DM-T9 as a required validation:
"creating a CapabilityGrant for an absolute-floor capability (PERM-006) is
a hard error." PERM-006's absolute-floor set is described only by *category*
in 00_CANONICAL_PRD.md §16 ("obtain superuser creds, read another user's
private graph/secrets, disable auth/audit, escape sandbox, obtain master
keys, self-escalate, exfiltrate credentials") — there is no ratified,
enumerated list of concrete capability *strings* anywhere in `00`/`01`/`07`.
`CapabilityGrant.capability` is typed as a bare `string` (01 §7.1), not an
enum, and no canonical registry maps strings to the absolute-floor category.
Implementing DM-T9 in foundation would therefore require *inventing* that
list — fabricating security semantics the instructions explicitly forbid
("Do not fabricate missing security semantics"). This is left as an
explicit, undone TODO for the branch that owns PERM-006/OD-TOOL-1
(07_TOOL_CAPABILITY_EXECUTION.md's "exact risk-tier assignment per
operation ... owner signs"), not silently implemented here.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import Field

from shared.schemas.common import ORMBase, utcnow
from shared.schemas.enums import CapabilityScopeType, PermissionDecisionValue, RiskCategory


class CapabilityGrant(ORMBase):
    """PRD PERM-001/002, RAUTH-001 dimension 5 (01 §7.1).

    See module docstring: absolute-floor rejection (DM-T9) is NOT
    implemented here — see the open-ambiguity note above.
    """

    grant_id: UUID = Field(default_factory=uuid4)
    principal_id: UUID
    scope_type: CapabilityScopeType
    capability: str
    resource_scope: dict | None = None
    granted_by: UUID
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class PermissionDecision(ORMBase):
    """PRD PERM-004/005 (01 §7.2) — the audited output of an authZ check.

    Foundation defines the record shape only. No code in this branch
    produces a PermissionDecision, because no authorization engine exists
    yet to produce one (04_AUTHORIZATION_GRAPH_RESOURCE.md's job). The
    table exists so that branch has somewhere to write to.
    """

    decision_id: UUID = Field(default_factory=uuid4)
    request_id: UUID
    principal_id: UUID
    capability: str
    resource_ref: str
    decision: PermissionDecisionValue
    risk_category: RiskCategory
    reason: str
    timestamp: datetime = Field(default_factory=utcnow)
