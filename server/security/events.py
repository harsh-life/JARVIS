"""The canonical audit action registry.

Source: 01_DATA_MODEL_SCHEMA.md §11.1 (`AuditEvent`) plus the specific events
`03`/`04`/`07`/`12` require to be audited. 17 §0's rule — "a requirement is
met only when its test passes" — depends on audit actions being *stable
identifiers*, so they live here rather than being spelled inline at each call
site where a typo would silently create a second, unqueryable action name.

`[LOCKED]` (12 §6, SECRET-004) an action name is a fixed identifier from this
registry. `AuditEvent` has no free-form payload column, and `AuditLogger`
refuses an unregistered action — together that means there is no field in the
audit path capable of carrying a secret value, rather than merely a
convention against putting one there.
"""

from __future__ import annotations

from enum import Enum


class AuditAction(str, Enum):
    """Every security-sensitive action this branch can audit."""

    # ── authentication / identity (03) ──────────────────────────────────
    OIDC_LOGIN_STARTED = "auth.oidc.start"
    OIDC_LOGIN_SUCCEEDED = "auth.oidc.callback.success"
    OIDC_LOGIN_REJECTED = "auth.oidc.callback.rejected"
    USER_CREATED = "auth.user.created"
    BOOTSTRAP_TOKEN_ISSUED = "auth.bootstrap_token.issued"
    BOOTSTRAP_TOKEN_REJECTED = "auth.bootstrap_token.rejected"

    # ── device credential lifecycle (03 §4) ─────────────────────────────
    DEVICE_REGISTERED = "device.registered"
    DEVICE_CREDENTIAL_ROTATED = "device.credential.rotated"
    DEVICE_REVOKED = "device.revoked"
    DEVICE_PROOF_REJECTED = "device.proof.rejected"

    # ── sessions / access tokens (03 §5, §6) ────────────────────────────
    ACCESS_TOKEN_ISSUED = "session.token.issued"
    ACCESS_TOKEN_REJECTED = "session.token.rejected"
    SESSION_LOGGED_OUT = "session.logout"
    ACTIVE_GRAPH_CHANGED = "session.active_graph.changed"
    STEP_UP_REQUIRED = "session.step_up.required"

    # ── graph lifecycle & membership (04 §4) ────────────────────────────
    GRAPH_CREATED = "graph.created"
    GRAPH_ACCESS_REQUESTED = "graph.access_request.created"
    MEMBERSHIP_APPROVED = "graph.membership.approved"
    MEMBERSHIP_REVOKED = "graph.membership.revoked"
    GRAPH_OWNERSHIP_TRANSFERRED = "graph.ownership.transferred"

    # ── visibility (04 §5, RAUTH V2) ────────────────────────────────────
    RESOURCE_SHARED = "resource.visibility.shared"
    RESOURCE_UNSHARED = "resource.visibility.unshared"

    # ── authorization decisions (04 §3) ─────────────────────────────────
    AUTHORIZATION_DECIDED = "authz.decision"

    # ── capability grants (07 §2) ───────────────────────────────────────
    CAPABILITY_GRANTED = "capability.granted"
    CAPABILITY_REVOKED = "capability.revoked"
    CAPABILITY_GRANT_REFUSED = "capability.grant.refused"

    # ── confirmation (PERM-004, 05 §4) ──────────────────────────────────
    CONFIRMATION_ISSUED = "confirmation.issued"
    CONFIRMATION_ACCEPTED = "confirmation.accepted"
    CONFIRMATION_REJECTED = "confirmation.rejected"

    # ── secret lifecycle (12 §5) ────────────────────────────────────────
    SECRET_SET = "secret.set"
    SECRET_RESOLVED = "secret.get"
    SECRET_DELETED = "secret.delete"
    SECRET_ROTATED = "secret.rotate"

    # ── superuser (12 §4, SUPER-001) ────────────────────────────────────
    SUPERUSER_AUTHENTICATED = "superuser.authenticated"
    SUPERUSER_REJECTED = "superuser.rejected"

    # ── agent runtime (05) ──────────────────────────────────────────────
    AGENT_TASK_SUBMITTED = "agent.task.submitted"
    AGENT_TASK_PAUSED = "agent.task.paused"
    AGENT_TASK_COMPLETED = "agent.task.completed"
    AGENT_TASK_FAILED = "agent.task.failed"
    AGENT_TASK_CANCELLED = "agent.task.cancelled"
    AGENT_PROPOSAL_REJECTED = "agent.proposal.rejected"
    AGENT_TOOL_EXECUTED = "agent.tool.executed"
    AGENT_TOOL_FAILED = "agent.tool.failed"
    CAPABILITY_ACTIVATED = "capability.activated"
    CAPABILITY_ACTIVATION_REFUSED = "capability.activation.refused"
    CAPABILITY_DEACTIVATED = "capability.deactivated"

    # ── usage / rate / budget (13) ──────────────────────────────────────
    USAGE_LIMIT_EXCEEDED = "usage.limit.exceeded"

    # ── supervisory runtime (18 §4, §5, §7) ─────────────────────────────
    AGENT_WORKER_FAILED = "agent.worker.failed"
    AGENT_WORKER_SWITCHED = "agent.worker.switched"
    AGENT_STALL_DETECTED = "agent.stall.detected"
    AGENT_RECOVERY_EXHAUSTED = "agent.recovery.exhausted"
    BREAKER_TRIPPED = "breaker.tripped"
    BREAKER_GLOBAL_LATCHED = "breaker.global.latched"
    BREAKER_GLOBAL_CLEARED = "breaker.global.cleared"

    # ── superuser control requests (18 §5.4) — one row per privileged call,
    # whatever it changed; the state transitions above are recorded separately.
    CONTROL_STOP = "control.stop"
    CONTROL_GLOBAL_STOP = "control.global_stop"
    CONTROL_GLOBAL_CLEAR = "control.global_clear"
    # A break-glass activation or revoke the control path refused (20 §2.2).
    CONTROL_BREAK_GLASS = "control.break_glass"

    # ── break-glass / unconfined execution (20 §2.4) ────────────────────
    BREAK_GLASS_ACTIVATED = "break_glass.activated"
    BREAK_GLASS_INVOKED = "break_glass.invoked"
    BREAK_GLASS_ENDED = "break_glass.ended"


# 12 §5 emits through a port that speaks plain strings (server/secrets is
# below server/security and cannot import this enum). This is the one
# translation point between the two vocabularies; an unmapped action from the
# store is a programming error, not something to log under a made-up name.
SECRET_ACTION_BY_NAME: dict[str, AuditAction] = {
    "secret.set": AuditAction.SECRET_SET,
    "secret.get": AuditAction.SECRET_RESOLVED,
    "secret.delete": AuditAction.SECRET_DELETED,
    "secret.rotate": AuditAction.SECRET_ROTATED,
}
