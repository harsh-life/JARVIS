"""The agent envelope gate (docs/29 §10.3).

`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]` (docs/29). Wired in Phase 2 into
`runtime.py` for agent runs only (`state.agent` set): `_request_capabilities`
(before anything else is checked), `_tool_call` (after the mode ceiling,
with the call's effective scope and its registry tier), and both approval
paths (`_approve_activation`, `_approve_tool_operation`); `_visible_tools`
shows the worker only the tools the envelope reaches. An agent run follows

    agent proposal → envelope gate → existing activation → existing 04
    authorization (D1–D5, floor, tier) → confirmation where required → execution

The gate is not a second authorization engine. It can only **remove**: it
answers "is this inside the agent's compiled ceiling?", and a `True` means
nothing more than "the existing path may now decide as it always does". The
owner's live grants, activation, the mode ceiling and the engine still apply
on top, so the effective authority of any agent call is

    owner's current grants ∩ template maximum ∩ envelope ceiling ∩ graph scope
      ∩ runtime/platform ∩ run restrictions ∩ mode ceiling ∩ risk ceiling ∩ floor

and nothing in a spec, a prompt, a runtime or a model can widen it.

Pure: shared schemas only (no engine, no grants — INV-8).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from shared.schemas.agent_factory import CompiledAgentSpec, EnvelopeEntry, risk_severity, unattended_refusal
from shared.schemas.enums import RiskCategory

# Belt and braces: the compiler never maps these (docs/29 §9.3); the gate
# refuses them even if a stored spec somehow named one.
_NEVER = ("agent.", "device.", "system.restricted", "app.interact")


@dataclass(frozen=True)
class Envelope:
    """What one agent run may *ever* attempt: the compiled ceiling and the
    template's risk ceiling. Built from the stored spec at run start and
    re-read at every step (docs/29 §10.3)."""

    entries: tuple[EnvelopeEntry, ...]
    risk_ceiling: RiskCategory
    spec_hash: str

    @classmethod
    def from_spec(cls, spec: CompiledAgentSpec) -> "Envelope":
        return cls(entries=tuple(spec.envelope_ceiling), risk_ceiling=spec.risk_ceiling, spec_hash=spec.spec_hash)


def _refused_name(capability: str) -> bool:
    name = capability.strip().lower()
    return any(name == n or name.startswith(n) for n in _NEVER)


def _narrows(entry_scope: Mapping[str, str], scope: Mapping[str, str] | None) -> bool:
    """A call's scope stays inside an entry's when it keeps every key of the
    entry's scope with the same value (it may add narrowing keys of its own)."""

    scope = scope or {}
    return all(scope.get(key) == value for key, value in entry_scope.items())


def within_envelope(
    envelope: Envelope | None,
    capability: str,
    operation: str,
    scope: Mapping[str, str] | None,
    tier: RiskCategory | None,
) -> bool:
    """`None` envelope = not an agent run (unchanged behaviour). An unknown
    tier is outside (fail-closed)."""

    if envelope is None:
        return True
    if tier is None or _refused_name(capability):
        return False
    if risk_severity(tier) > risk_severity(envelope.risk_ceiling):
        return False
    return any(
        entry.capability == capability and operation in entry.operations and _narrows(entry.scope, scope)
        for entry in envelope.entries
    )


def activation_within_envelope(
    envelope: Envelope | None, capability: str, scope: Mapping[str, str] | None
) -> bool:
    """For `request_capabilities`: a capability outside the envelope is
    refused as an observation and never offered for confirmation (the
    `modes.not_activated` pattern)."""

    if envelope is None:
        return True
    if _refused_name(capability):
        return False
    return any(entry.capability == capability and _narrows(entry.scope, scope) for entry in envelope.entries)


def unattended_capability_refused(capability: str) -> bool:
    """docs/29 §15.7: a capability no unattended run can ever activate,
    whatever its operations — anything on a device or an app, break-glass,
    the Agent Factory itself."""

    return unattended_refusal(capability, "*", RiskCategory.LOW_READ) in ("device_execution", "never_unattended")


def not_unattended(capability: str) -> str:
    return (f"{capability}: outside what an unattended run may ever do — refused, never offered for "
            "confirmation.")


def not_in_envelope(capability: str) -> str:
    return f"{capability}: outside this agent's approved abilities — not requested, never offered for confirmation."


__all__ = [
    "Envelope",
    "activation_within_envelope",
    "not_in_envelope",
    "not_unattended",
    "unattended_capability_refused",
    "within_envelope",
]
