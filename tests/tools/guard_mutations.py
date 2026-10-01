"""Guard mutation check — proves the security tests would notice a guard's removal.

    python tests/tools/guard_mutations.py            # every mutant
    python tests/tools/guard_mutations.py M01 M07    # a selection
    python tests/tools/guard_mutations.py --list

Each mutant disables exactly one deterministic guard with one exact source edit
(the "old" text must occur exactly once, or the mutant refuses to run), runs the
test selection that is supposed to defend that guard, and **expects it to
fail**. A mutant whose tests still pass is a *survivor*: the guard could be
deleted and nothing would notice. The file is restored byte-for-byte in a
`finally`, whatever happens.

This is Phase H's §21 evidence (docs/RELEASE_VALIDATION.md records the last
run). It is not wired into CI — a full run re-executes large suites once per
mutant — but `tests/foundation/test_guard_mutations_meta.py` keeps every
mutant's anchor text present, so the catalogue cannot silently rot as the code
moves.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", "-p", "no:logging", "-W", "ignore"]
GRADLE = ["./gradlew", "--offline", "-q", ":contract:test"]


@dataclass(frozen=True)
class Mutant:
    id: str
    guard: str
    path: str
    old: str
    new: str
    command: tuple[str, ...]
    cwd: str = "."


MUTANTS: tuple[Mutant, ...] = (
    Mutant("M01", "AuthZ D4 visibility: a private resource becomes readable by anyone who reaches it",
           "server/graph/predicate.py",
           "        and is_active_member_of_resource_graph\n    ):\n        return True\n    return False",
           "        and is_active_member_of_resource_graph\n    ):\n        return True\n    return True",
           (*PY, "tests/security_core/test_authorization.py")),
    Mutant("M02", "AuthZ D4 graph scope: graph_id alone authorizes (membership ignored)",
           "server/graph/predicate.py",
           "        and is_active_member_of_resource_graph\n    ):",
           "        and True\n    ):",
           (*PY, "tests/security_core/test_authorization.py")),
    Mutant("M03", "AuthZ D1 membership: a non-member passes the graph check",
           "server/graph/authorization.py",
           "            if role is None:\n                return _deny(\"not_a_member\", DenialSurface.NOT_FOUND)",
           "            if False:\n                return _deny(\"not_a_member\", DenialSurface.NOT_FOUND)",
           (*PY, "tests/security_core/test_authorization.py")),
    Mutant("M04", "Confirmation: the token no longer binds the exact arguments",
           "server/capabilities/confirmation.py",
           "        and row.arguments_hash == binding.arguments_hash()",
           "        and True",
           (*PY, "tests/security_core/test_confirmation.py")),
    Mutant("M05", "Confirmation: the token no longer binds the session",
           "server/capabilities/confirmation.py",
           "        and row.session_id == binding.session_id",
           "        and True",
           (*PY, "tests/security_core/test_confirmation.py")),
    Mutant("M06", "Step-up: a bad signature is accepted",
           "server/auth/step_up.py",
           "            except (InvalidSignature, ValueError, binascii.Error):\n                reason = \"bad_signature\"",
           "            except (InvalidSignature, ValueError, binascii.Error):\n                reason = None",
           (*PY, "tests/security_core/test_step_up.py")),
    Mutant("M07", "Step-up: an expired challenge is accepted",
           "server/auth/step_up.py",
           "        elif expires_at is None or now >= as_utc(expires_at):",
           "        elif False:",
           (*PY, "tests/security_core/test_step_up.py")),
    Mutant("M08", "Device routing: an operation goes to any connected device, not its exact device_id",
           "server/execution/device_hub.py",
           "        session = self._sessions.get(operation.device_id)\n        if session is None:\n"
           "            # Never queued.",
           "        session = next(iter(self._sessions.values()), None)\n        if session is None:\n"
           "            # Never queued.",
           (*PY, "tests/execution/test_device_hub.py")),
    Mutant("M09", "Absolute floor: a reserved capability name is no longer recognised",
           "server/capabilities/floor.py",
           "    normalized = capability.strip().lower()\n    if normalized in _RESERVED_CAPABILITY_NAMES:",
           "    normalized = capability.strip().lower()\n    return None\n    if normalized in _RESERVED_CAPABILITY_NAMES:",
           (*PY, "tests/security_core/test_capabilities.py", "tests/security_core/test_confirmation.py")),
    Mutant("M10", "SecretStore: the agent requester may resolve a secret",
           "server/secrets/store.py",
           "        # contract stopping server.agent from importing this package).\n"
           "        if requester.kind is RequesterKind.AGENT:",
           "        # contract stopping server.agent from importing this package).\n"
           "        if False:",
           (*PY, "tests/security_core/test_secretstore.py")),
    Mutant("M11", "Secret filter: nothing is ever detected as a secret",
           "server/security/secret_patterns.py",
           '    """The name of the first secret pattern `text` matches, else `None`."""\n',
           '    """The name of the first secret pattern `text` matches, else `None`."""\n    return None\n',
           (*PY, "tests/memory/test_write_gate.py")),
    Mutant("M12", "Egress: the cloud metadata address is no longer blocked",
           "server/net/policy.py",
           "    if str(parsed) in _METADATA_ADDRESSES or address_text in _METADATA_ADDRESSES:",
           "    if False:",
           (*PY, "tests/execution/test_net_egress.py")),
    Mutant("M13", "Egress: loopback is no longer blocked",
           "server/net/policy.py",
           "    if parsed.is_loopback:",
           "    if False:",
           (*PY, "tests/execution/test_net_egress.py")),
    Mutant("M14", "Sandbox: a read/listing follows a symlink swapped in after the check (TOCTOU)",
           "server/fs/sandbox.py",
           "        return os.open(name, flags | os.O_NOFOLLOW, dir_fd=dir_fd)",
           "        return os.open(name, flags, dir_fd=dir_fd)",
           (*PY, "tests/execution/test_fs_sandbox.py")),
    Mutant("M27", "Sandbox: a write follows a symlink at the leaf",
           "server/fs/sandbox.py",
           "            flags = os.O_WRONLY | os.O_NOFOLLOW | (os.O_CREAT",
           "            flags = os.O_WRONLY | (os.O_CREAT",
           (*PY, "tests/execution/test_fs_sandbox.py")),
    Mutant("M15", "Resource scope: an unclassified app may be UI-controlled",
           "server/capabilities/app_classification.py",
           "            return ScopeConstraint(denial=APP_NOT_CLASSIFIED)",
           "            return ScopeConstraint()",
           (*PY, "tests/runtime/test_sensitive_app_gate.py")),
    Mutant("M16", "Principal freshness: a revoked device's task keeps running",
           "server/composition/security_port.py",
           "        if device is None or device.revoked or device.user_id != principal.user_id:\n"
           "            return False\n        # The same link",
           "        if device is None or device.user_id != principal.user_id:\n"
           "            return False\n        # The same link",
           (*PY, "tests/runtime/test_platform_wait.py", "tests/runtime/test_lifecycle_and_blast_radius.py")),
    Mutant("M17", "Break-glass: activation no longer requires a verified superuser",
           "server/composition/break_glass.py",
           "    if not isinstance(principal, SuperuserPrincipal) or not principal.grant.is_valid():",
           "    if False:",
           (*PY, "tests/security_core/test_break_glass_registry.py")),
    Mutant("M18", "Mode ceiling: draft/suggest/observe tasks may execute above low_read",
           "server/agent/runtime.py",
           "        if modes.MODE_CEILING[state.mode] is None:\n            return True",
           "        if True:\n            return True",
           (*PY, "tests/runtime/test_task_modes.py")),
    Mutant("M19", "Memory hydration: the engine's readable() re-check is skipped",
           "server/memory/hydration.py",
           "            if not readable(",
           "            if False and not readable(",
           (*PY, "tests/runtime/test_memory_hydration.py")),
    Mutant("M20", "Idempotency: keys are no longer namespaced by user (cross-user replay)",
           "server/gateway/routers/agent.py",
           '            idempotency_key=f"{principal.user_id}:{idempotency_key}",\n'
           '            method="POST",\n            path="/api/v1/agent/tasks",',
           '            idempotency_key=idempotency_key,\n'
           '            method="POST",\n            path="/api/v1/agent/tasks",',
           (*PY, "tests/runtime/test_resource_control.py")),
    Mutant("M21", "Push: the wake payload carries a task id",
           "shared/schemas/push.py",
           '            "data": WakeData().model_dump(),',
           '            "data": {**WakeData().model_dump(), "task_id": "t-1"},',
           (*PY, "tests/security_core/test_push_wake.py")),
    Mutant("M22", "Voice: a speaker context may claim to be an authorization signal",
           "shared/schemas/voice.py",
           "    is_authorization_signal: Literal[False] = False",
           "    is_authorization_signal: bool = False",
           (*PY, "tests/voice/test_voice_privacy_and_authority.py", "tests/foundation/test_schemas.py")),
    Mutant("M23", "Scheduler: the firing path imports the agent runtime (a path to execution)",
           "server/scheduler/firing.py",
           "from __future__ import annotations\n",
           "from __future__ import annotations\n\nimport server.agent.runtime  # noqa: F401 — mutant\n",
           ("lint-imports", "--config", "pyproject.toml")),
    Mutant("M24", "Device guard (Kotlin): a toggled-off app is no longer refused on the phone",
           "android/contract/src/main/kotlin/com/hypermind/jarvis/contract/DeviceGuard.kt",
           "        if (!toggleOn) return GuardVerdict.Refused(RefusalReason.TOGGLE_OFF)",
           "        if (false) return GuardVerdict.Refused(RefusalReason.TOGGLE_OFF)",
           tuple(GRADLE), cwd="android"),
    Mutant("M25", "Device guard (Kotlin): an expired operation runs",
           "android/contract/src/main/kotlin/com/hypermind/jarvis/contract/DeviceGuard.kt",
           "            !now.isBefore(expires) -> RefusalReason.OPERATION_EXPIRED",
           "            false -> RefusalReason.OPERATION_EXPIRED",
           tuple(GRADLE), cwd="android"),
    Mutant("M26", "Device guard (Kotlin): an operation addressed to another device runs",
           "android/contract/src/main/kotlin/com/hypermind/jarvis/contract/DeviceGuard.kt",
           "        if (envelope.deviceId != deviceId) return RefusalReason.WRONG_DEVICE",
           "        if (false) return RefusalReason.WRONG_DEVICE",
           tuple(GRADLE), cwd="android"),
    # ── Real-data gate: Stage 5 surfaces and grant scoping ──────────────
    Mutant("M28", "Console (DSH-T4): user content is shown instead of redacted",
           "server/dashboard/redaction.py",
           '    return {"redacted": True, "chars": len(text)}',
           "    return text",
           (*PY, "tests/dashboard/test_console.py")),
    Mutant("M29", "Console (DSH-T3): a credential inside a setting is no longer masked",
           "server/dashboard/redaction.py",
           "    return redact_secrets(text)[0]",
           "    return text",
           (*PY, "tests/dashboard/test_console.py")),
    Mutant("M30", "Console (DSH-T4): the unredacted view is no longer audited before it reads",
           "server/gateway/routers/admin.py",
           "    await audit.record(actor=AuditActor.SUPERUSER, action=AuditAction.CONSOLE_UNREDACTED_VIEW,",
           "    False and await audit.record(actor=AuditActor.SUPERUSER, action=AuditAction.CONSOLE_UNREDACTED_VIEW,",
           (*PY, "tests/dashboard/test_console.py")),
    Mutant("M31", "Console (DSH-T2): a console view no longer requires the superuser principal",
           "server/gateway/routers/admin.py",
           "async def banner(request: Request, _: SuperuserPrincipal = Depends(get_superuser),",
           "async def banner(request: Request,",
           (*PY, "tests/dashboard/test_console.py")),
    Mutant("M32", "Judge (JDG-B2): a post-hoc evaluation may stop a task",
           "server/evaluation/service.py",
           "        if kind is not EvaluationKind.LIVE_WINDOW:",
           "        if False:",
           (*PY, "tests/evaluation/test_service.py")),
    Mutant("M33", "Judge (JDG-B2): a stop is honoured while the operator's stop switch is off",
           "server/evaluation/service.py",
           "        if self._breaker is None or not self._switches.stop_requests_enabled():",
           "        if self._breaker is None:",
           (*PY, "tests/evaluation/test_service.py")),
    Mutant("M34", "Judge (JDG-T6): a secret in the trace reaches the Judge unredacted",
           "server/evaluation/trace.py",
           "        clean, names = redact_secrets(text)",
           "        clean, names = text, []",
           (*PY, "tests/evaluation/test_judge_contract.py")),
    Mutant("M35", "Judge (JDG-T7): a candidate may cite another task (another user's) as evidence",
           "server/evaluation/candidates.py",
           "    if any(e != task_id for e in evidence):",
           "    if False:",
           (*PY, "tests/evaluation/test_judge_contract.py")),
    Mutant("M36", "Judge (JDG-T9): an improvement decision no longer requires a verified superuser",
           "server/composition/improvements.py",
           "    if not isinstance(principal, SuperuserPrincipal) or not principal.grant.is_valid():",
           "    if False:",
           (*PY, "tests/evaluation/test_improvements.py")),
    Mutant("M37", "Judge (JDG-T8): the Judge's own budget no longer refuses a paid call",
           "server/evaluation/service.py",
           "            if spent + projected_cost > self._settings.budget:",
           "            if False:",
           (*PY, "tests/evaluation/test_service.py", "tests/evaluation/test_judge_runtime.py")),
    Mutant("M38", "Judge (JDG-B4): the operator can switch on stop requests the config withholds",
           "server/composition/improvements.py",
           "        if stop_requests_enabled and not (config.enabled and config.may_request_stop):",
           "        if False:",
           (*PY, "tests/evaluation/test_improvements.py")),
    Mutant("M39", "AuthZ D5 capability: a request without the required grant passes",
           "server/graph/authorization.py",
           "            if not granted:\n                return _deny(\"capability_missing\", DenialSurface.FORBIDDEN)",
           "            if False:\n                return _deny(\"capability_missing\", DenialSurface.FORBIDDEN)",
           (*PY, "tests/runtime/test_authorization_and_capabilities.py")),
    Mutant("M40", "Task-scoped grant: usable by a principal other than the user who consented",
           "server/capabilities/grants.py",
           "            and row.granted_by == context.principal.user_id",
           "            and True",
           (*PY, "tests/security_core/test_runtime_security_extensions.py", "tests/security_core/test_capabilities.py",
            "tests/runtime/test_authorization_and_capabilities.py")),
    Mutant("M41", "Device-scoped grant (per-app grid): matches any device",
           "server/capabilities/grants.py",
           "        return row.principal_id == context.principal.device_id",
           "        return True",
           (*PY, "tests/security_core/test_grid_grants.py", "tests/security_core/test_capabilities.py")),
    # H-1: the guards that concurrent requests on a multi-writer store need.
    # Run them with HYPERMIND_TEST_DATABASE_URL set, as CI's `postgres` job does.
    Mutant("M42", "H-1 idempotency: a same-key retry while the original runs executes again",
           "server/storage/idempotency.py",
           "    if idempotency_key in _IN_FLIGHT:",
           "    if False:",
           (*PY, "tests/runtime/test_concurrent_store.py")),
    Mutant("M43", "H-1 usage admission: calls admitted but not yet in the ledger stop counting",
           "server/security/usage.py",
           "        return [a for a in self._admitted.values() if a.recorded_in is not own]",
           "        return []",
           (*PY, "tests/runtime/test_concurrent_store.py")),
    Mutant("M44", "H-1 transaction scope: a task's store transaction is held across its model and tool calls",
           "server/agent/runtime.py",
           "    if env.release_store is not None:\n        await env.release_store()",
           "    return None",
           (*PY, "tests/runtime/test_concurrent_store.py")),
    # ── docs/29 Agent Factory (M-AG*) ────────────────────────────────────
    Mutant("M-AG1", "Agent drafts: unknown fields (owner, graph, capability, tier, secret...) are ignored instead of rejected",
           "shared/schemas/agent_factory.py",
           '    model_config = ConfigDict(extra="forbid", frozen=True)',
           '    model_config = ConfigDict(extra="ignore", frozen=True)',
           (*PY, "tests/agents/test_schemas.py")),
    Mutant("M-AG2", "Agent envelope gate: every call is admitted (the ceiling no longer removes anything)",
           "server/agent/envelope.py",
           "    return any(\n        entry.capability == capability and operation in entry.operations",
           "    return True or any(\n        entry.capability == capability and operation in entry.operations",
           (*PY, "tests/agents/test_future_interfaces.py", "tests/agents/test_envelope_gate.py")),
    Mutant("M-AG15", "Native provider: a foreign runtime's handle is acted on as if it were native",
           "server/agents/providers/native.py",
           "    if runtime_id != NATIVE_RUNTIME_ID:\n        raise ValueError",
           "    if False:\n        raise ValueError",
           (*PY, "tests/agents/test_native_provider.py")),
    Mutant("M-AG16", "Agent run: a deleted, paused or revoked definition no longer stops its running run",
           "server/agents/service.py",
           "        if spec is None or definition.status != AgentStatus.ACTIVE.value:\n            return AgentFailureCode.AGENT_UNAVAILABLE\n        if definition.owner_user_id",
           "        if False:\n            return AgentFailureCode.AGENT_UNAVAILABLE\n        if definition.owner_user_id",
           (*PY, "tests/agents/test_agent_runs.py")),
    Mutant("M-AG17", "Agent run: a run keeps going on a spec version/hash the owner has since replaced",
           "server/agents/service.py",
           "        if definition.current_version != version or spec.spec_hash != spec_hash:\n",
           "        if False:\n",
           (*PY, "tests/agents/test_agent_runs.py")),
    Mutant("M-AG18", "Agent run: the runtime stops re-validating the definition at each step",
           "server/agent/runtime.py",
           "        code = await env.agent_runs.check(state.agent)\n",
           "        code = None\n",
           (*PY, "tests/agents/test_agent_runs.py")),
    Mutant("M-AG19", "Agent run: an agent runs outside its own graph",
           "server/composition/agents.py",
           "        if spec.graph_id != graph_id:\n",
           "        if False:\n",
           (*PY, "tests/agents/test_agent_runs.py")),
    Mutant("M-AG20", "Agent run: an agent that is not active (needs re-approval, paused) can start a run",
           "server/composition/agents.py",
           "        if definition.status != AgentStatus.ACTIVE.value:\n            raise await self._refuse_run(",
           "        if False:\n            raise await self._refuse_run(",
           (*PY, "tests/agents/test_agent_runs.py")),
    Mutant("M-AG21", "Agent run: a revoked (tampered) agent can start a run",
           "server/composition/agents.py",
           "        if spec is None:\n            raise await self._refuse_run(audit, principal, agent_id, \"agent_revoked\",\n"
           "                                         \"this agent is revoked and cannot run\")",
           "        if False:\n            raise await self._refuse_run(audit, principal, agent_id, \"agent_revoked\",\n"
           "                                         \"this agent is revoked and cannot run\")",
           (*PY, "tests/agents/test_agent_runs.py")),
    Mutant("M-AG22", "Agent run: a template bump no longer sends a run's spec back for re-approval",
           "server/composition/agents.py",
           "        if revalidation_required(spec, registries.enabled_templates):\n",
           "        if False:\n",
           (*PY, "tests/agents/test_agent_runs.py")),
    Mutant("M-AG23", "Agent run: a model profile no longer configured or permitted still runs",
           "server/composition/agents.py",
           "                or profile.profile.version != selection.model_profile_version\n"
           "                or (profile.profile_id not in registries.open_to_all\n",
           "                or False\n"
           "                or (False\n",
           (*PY, "tests/agents/test_agent_runs.py")),
    Mutant("M-AG24", "Agent run: the spec's own bounds no longer tighten the runtime's",
           "server/agent/runtime.py",
           "        return min(self._bounds.max_model_calls, state.agent.max_model_calls)\n",
           "        return self._bounds.max_model_calls\n",
           (*PY, "tests/agents/test_agent_runs.py")),
    Mutant("M-AG25", "Agent run: a run id is read under an agent it does not belong to",
           "server/composition/agents.py",
           "        if run is None or run.agent_id != agent_id or run.owner_user_id != principal.user_id:\n",
           "        if run is None:\n",
           (*PY, "tests/agents/test_agent_runs.py")),
    Mutant("M-AG26", "Agent run: a model profile the operator disabled still runs",
           "server/composition/agents.py",
           "        if (profile is None or not profile.profile.enabled\n",
           "        if (profile is None or False\n",
           (*PY, "tests/agents/test_agent_runs.py")),
    Mutant("M-AG27", "Envelope gate: a capability outside the agent's envelope can be activated or offered",
           "server/agent/runtime.py",
           "            if not agent_envelope.activation_within_envelope(self._envelope(state), capability, scope):\n",
           "            if False:\n",
           (*PY, "tests/agents/test_envelope_gate.py")),
    Mutant("M-AG28", "Envelope gate: a tool call outside the envelope reaches the engine",
           "server/agent/runtime.py",
           "        if not self._within_envelope(env, state, handle.required_capability, call.operation,\n",
           "        if False and self._within_envelope(env, state, handle.required_capability, call.operation,\n",
           (*PY, "tests/agents/test_envelope_gate.py")),
    Mutant("M-AG29", "Envelope gate: an approved activation is no longer re-checked against the envelope",
           "server/agent/runtime.py",
           "        if not agent_envelope.activation_within_envelope(self._envelope(state), pending.capability,\n",
           "        if False and agent_envelope.activation_within_envelope(self._envelope(state), pending.capability,\n",
           (*PY, "tests/agents/test_envelope_gate.py")),
    Mutant("M-AG30", "Envelope gate: the agent's worker is shown tools outside its envelope",
           "server/agent/runtime.py",
           "            handles = [h for h in handles if h.required_capability in reachable and not h.is_model_tool]\n",
           "            handles = [h for h in handles if not h.is_model_tool]\n",
           (*PY, "tests/agents/test_envelope_gate.py")),
    Mutant("M-AG31", "Envelope gate: an entry's scope no longer bounds the call's scope",
           "server/agent/envelope.py",
           "    return all(scope.get(key) == value for key, value in entry_scope.items())",
           "    return True",
           (*PY, "tests/agents/test_envelope_gate.py")),
    Mutant("M-AG32", "Agent run: an owner removed from the agent's graph keeps running it",
           "server/agents/service.py",
           "        if definition.graph_id is not None and not await is_member(definition.graph_id, run.owner_user_id):\n",
           "        if False:\n",
           (*PY, "tests/agents/test_envelope_gate.py")),
    Mutant("M-AG33", "Envelope gate: agent.*/system.restricted/device names are no longer refused by name",
           "server/agent/envelope.py",
           "    return any(name == n or name.startswith(n) for n in _NEVER)",
           "    return False",
           (*PY, "tests/agents/test_future_interfaces.py", "tests/agents/test_envelope_gate.py")),
    Mutant("M-AG34", "Model routing: a model tool can be called directly in an agent run, around the routing",
           "server/agent/runtime.py",
           "        if state.agent is not None and handle.is_model_tool and not routed:\n",
           "        if False:\n",
           (*PY, "tests/agents/test_model_routing_runs.py")),
    Mutant("M-AG35", "Model routing: agent.model is routed for an agent whose envelope has no model.invoke",
           "server/agent/runtime.py",
           "        if not agent.model_tools:\n            await self._envelope_denied(env, state, resource)\n",
           "        if False:\n            await self._envelope_denied(env, state, resource)\n",
           (*PY, "tests/agents/test_model_routing_runs.py")),
    Mutant("M-AG36", "Model routing: a request naming a provider/model/endpoint/key is accepted (schema bypass)",
           "server/composition/agents.py",
           "            request = ModelCallRequest.model_validate(dict(arguments))\n",
           "            request = ModelCallRequest.model_validate({k: v for k, v in dict(arguments).items() "
           "if k in ModelCallRequest.model_fields})\n",
           (*PY, "tests/agents/test_model_routing_runs.py")),
    Mutant("M-AG37", "Model routing: a profile the owner may not use can be chosen",
           "server/agents/gateway/model_routing.py",
           "        if p.profile_id not in registries.open_to_all and p.model_ref != owner_primary_model_ref:\n",
           "        if False:\n",
           (*PY, "tests/agents/test_future_interfaces.py", "tests/agents/test_model_routing_runs.py")),
    Mutant("M-AG38", "Model routing: a model over the run's budget can be chosen",
           "server/agents/gateway/model_routing.py",
           "        if m.projected_cost(1) > spec.budget.per_run:\n",
           "        if False:\n",
           (*PY, "tests/agents/test_future_interfaces.py", "tests/agents/test_model_routing_runs.py")),
    Mutant("M-AG39", "Model routing: the routed call skips the envelope gate and the engine (routing authorizes)",
           "server/agent/runtime.py",
           "            call, routed = rewritten, True\n            resource = f\"tool:{call.tool}.{call.operation}\"\n",
           "            call, routed = rewritten, True\n            resource = f\"tool:{call.tool}.{call.operation}\"\n"
           "            _h = self._tools.resolve(call.tool)\n"
           "            await self._execute(env, state, _h, call.operation, ExecutionPlatform.SERVER, "
           "dict(call.arguments), None, {\"model_tool_id\": call.tool}, remaining)\n"
           "            return None\n",
           (*PY, "tests/agents/test_model_routing_runs.py")),
    Mutant("M-AG40", "Agent run budget: the agent's per-run budget no longer tightens the task budget",
           "server/agent/runtime.py",
           "        return min(self._bounds.per_task_budget, state.agent.budget_per_run)\n",
           "        return self._bounds.per_task_budget\n",
           (*PY, "tests/agents/test_model_routing_runs.py")),
    Mutant("M-AG41", "Run stop: cancelling a run no longer stops its task (the paused action stays confirmable)",
           "server/composition/agents.py",
           "                await self._tasks.cancel(self._session, principal=self._principal, task_id=run.task_id,\n",
           "                pass; await self._tasks.get(self._session, principal=self._principal, task_id=run.task_id,\n",
           (*PY, "tests/agents/test_run_control.py")),
    Mutant("M-AG49", "Agent run: a tool call proposed before a stop/delete/change is not re-validated",
           "server/agent/runtime.py",
           "        # the model was thinking applies to the call it proposed.\n        await self._check_agent(env, state)\n",
           "        # the model was thinking applies to the call it proposed.\n",
           (*PY, "tests/agents/test_run_control.py")),
    Mutant("M-AG42", "Run stop: a cancelled run's record is not closed (its re-validation still passes)",
           "server/composition/agents.py",
           "        await self._service.run_cancelled(self._session, run_id, reason=reason.value)\n",
           "        pass\n",
           (*PY, "tests/agents/test_run_control.py")),
    Mutant("M-AG43", "Pause: pausing an agent leaves its live runs running",
           "server/composition/agents.py",
           "        await self._stop_runs(session, audit, principal, agent_id, CancelReason.OWNER_STOP)\n",
           "        pass\n",
           (*PY, "tests/agents/test_run_control.py")),
    Mutant("M-AG44", "Delete: deleting an agent leaves its live runs running",
           "server/composition/agents.py",
           "            await self._stop_runs(session, audit, principal, agent_id, CancelReason.DELETED)\n",
           "            pass\n",
           (*PY, "tests/agents/test_run_control.py")),
    Mutant("M-AG45", "Delete (service path): a deleted agent's run records stay open",
           "server/agents/service.py",
           "            await self.run_cancelled(session, run.run_id, reason=\"deleted\")\n",
           "            pass\n",
           (*PY, "tests/agents/test_run_control.py")),
    Mutant("M-AG46", "Resume: an agent is resumed without the owner's confirmation",
           "server/composition/agents.py",
           "        await self._confirm_or_refuse(session, outcome, action=\"resume_agent\",\n",
           "        dict(s=session, o=outcome, action=\"resume_agent\",\n",
           (*PY, "tests/agents/test_run_control.py")),
    Mutant("M-AG47", "Resume: an outdated template is resumed without re-approval",
           "server/composition/agents.py",
           "        await self._still_runnable(session, audit, principal, definition, spec)\n        outcome",
           "        outcome",
           (*PY, "tests/agents/test_run_control.py")),
    Mutant("M-AG48", "Run stop: another user can stop a run (owner check removed)",
           "server/composition/agents.py",
           "        run = await self._owned_run(session, audit, principal, agent_id, run_id)\n        service = self._factory.service\n        if run.finished_at is None:",
           "        run = await self._factory.service.get_run(session, run_id)\n        service = self._factory.service\n        if run.finished_at is None:",
           (*PY, "tests/agents/test_run_control.py")),
    Mutant("M-AG50", "Notebook: an agent whose spec does not enable the notebook can use it",
           "server/agent/runtime.py",
           "        if not agent.notebook:\n            await self._envelope_denied(env, state, resource)\n"
           "            state.messages.append(ctx.observation(agent_envelope.not_in_envelope(NOTEBOOK_TOOL),",
           "        if False:\n            await self._envelope_denied(env, state, resource)\n"
           "            state.messages.append(ctx.observation(agent_envelope.not_in_envelope(NOTEBOOK_TOOL),",
           (*PY, "tests/agents/test_notebook.py")),
    Mutant("M-AG51", "Notebook: a credential-shaped note is stored",
           "server/composition/agents.py",
           "        if find_secret(f\"{key}\\n{value}\") is not None:\n",
           "        if False:\n",
           (*PY, "tests/agents/test_notebook.py")),
    Mutant("M-AG52", "Notebook: an emotional/relational note is stored",
           "server/composition/agents.py",
           "        if _EMOTION.flags(f\"{key} {value}\"):\n",
           "        if False:\n",
           (*PY, "tests/agents/test_notebook.py")),
    Mutant("M-AG53", "Notebook: the per-agent entry bound is not enforced",
           "server/agents/service.py",
           "            if count >= NOTEBOOK_MAX_ENTRIES:\n",
           "            if False:\n",
           (*PY, "tests/agents/test_notebook.py")),
    Mutant("M-AG54", "Notebook: deleting the agent leaves its notebook behind",
           "server/agents/service.py",
           "        await self.notebook_clear(session, agent_id)\n        await session.execute(delete(AgentInboxItemRow)",
           "        await session.execute(delete(AgentInboxItemRow)",
           (*PY, "tests/agents/test_notebook.py")),
    Mutant("M-AG55", "Notebook: keys are not validated (path-like or overlong keys are stored)",
           "server/agents/service.py",
           "        if not _NOTEBOOK_KEY.fullmatch(key):\n",
           "        if False:\n",
           (*PY, "tests/agents/test_notebook.py")),
    Mutant("M-AG56", "Notebook: the owner API shows another owner's agent's notebook",
           "server/composition/agents.py",
           "        await self._owned(session, audit, principal, agent_id, Operation.READ)\n"
           "        rows = await self._factory.service.notebook_entries(session, agent_id, principal.user_id)\n",
           "        rows = await self._factory.service.notebook_entries(session, agent_id, principal.user_id)\n",
           (*PY, "tests/agents/test_notebook.py")),
    Mutant("M-AG57", "Inbox: a credential-shaped result is stored and shown",
           "server/composition/agents.py",
           "    if find_secret(text) is not None:\n        return \"\", True, False\n",
           "    if False:\n        return \"\", True, False\n",
           (*PY, "tests/agents/test_inbox.py")),
    Mutant("M-AG58", "Inbox: control characters / terminal escapes reach the owner's client",
           "server/composition/agents.py",
           "    text = _UNSAFE_CHARS.sub(\"\", response or \"\")\n",
           "    text = response or \"\"\n",
           (*PY, "tests/agents/test_inbox.py")),
    Mutant("M-AG59", "Inbox: an item is not bounded",
           "server/composition/agents.py",
           "    if len(text) > INBOX_MAX_BODY_CHARS:\n",
           "    if False:\n",
           (*PY, "tests/agents/test_inbox.py")),
    Mutant("M-AG60", "Inbox: deleting the agent leaves its items behind",
           "server/agents/service.py",
           "        await session.execute(delete(AgentInboxItemRow).where(AgentInboxItemRow.agent_id == agent_id))\n",
           "        pass\n",
           (*PY, "tests/agents/test_inbox.py")),
    # Needs the memory stack: run with HYPERMIND_REQUIRE_MEMORY_STACK=1 (else the test skips).
    Mutant("M-AG61", "Memory: an agent run's output is extracted into the owner's Mem0 memory",
           "server/agent/runtime.py",
           "        if state.agent is not None:\n            # docs/29 §16.2 (AGENT-T17)",
           "        if False:\n            # docs/29 §16.2 (AGENT-T17)",
           (*PY, "tests/memory/test_agent_memory_exclusion.py")),
    Mutant("M-AG62", "Attribution: an agent run's tool calls are not attributed to it",
           "server/agent/runtime.py",
           "        await self._attribute(env, state, usage_id, output.estimated_cost)\n",
           "        pass\n",
           (*PY, "tests/agents/test_attribution_export.py")),
    Mutant("M-AG63", "Attribution: an agent run's model calls are not attributed to it",
           "server/agent/runtime.py",
           "        await self._attribute(env, state, usage_id, cost)\n",
           "        pass\n",
           (*PY, "tests/agents/test_attribution_export.py")),
    Mutant("M-AG64", "Budget: a run starts although the agent's month is spent",
           "server/composition/agents.py",
           "        if spec.budget.per_month > 0 and spent >= spec.budget.per_month:\n",
           "        if False:\n",
           (*PY, "tests/agents/test_attribution_export.py")),
    Mutant("M-AG65", "Budget: a run's ceiling is not capped by what is left of the month",
           "server/composition/agents.py",
           "        run_budget = min(spec.budget.per_run, max(0.0, spec.budget.per_month - spent))\n",
           "        run_budget = spec.budget.per_run\n",
           (*PY, "tests/agents/test_attribution_export.py")),
    Mutant("M-AG66", "Export: another user can export an agent (owner check removed)",
           "server/composition/agents.py",
           "        definition, spec = await self._owned(session, audit, principal, agent_id, Operation.READ)\n"
           "        service = self._factory.service\n        port = _PresentUserRun(",
           "        definition, spec = await self._factory.service.load(session, agent_id)\n"
           "        service = self._factory.service\n        port = _PresentUserRun(",
           (*PY, "tests/agents/test_attribution_export.py")),
    Mutant("M-AG67", "Agent run: inside the envelope an activation needs no owner grant (envelope treated as a grant)",
           "server/agent/runtime.py",
           "            if await env.security.holds_standing_grant(\n",
           "            if state.agent is not None or await env.security.holds_standing_grant(\n",
           (*PY, "tests/agents/test_envelope_gate.py", "tests/agents/test_model_routing_runs.py")),
    Mutant("M-AG68", "Agent run: an authorization decision is cached and reused (revocation no longer applies)",
           "server/agent/runtime.py",
           "        verdict = await env.security.authorize_action(request)\n",
           "        _cache = state.__dict__.setdefault(\"_authz_cache\", {})\n"
           "        _key = (handle.required_capability, call.operation)\n"
           "        verdict = _cache.get(_key) or await env.security.authorize_action(request)\n"
           "        _cache[_key] = verdict\n",
           (*PY, "tests/agents/test_envelope_gate.py", "tests/agents/test_model_routing_runs.py")),
    Mutant("M-AG69", "Envelope gate: an operation above the template's risk ceiling passes",
           "server/agent/envelope.py",
           "    if risk_severity(tier) > risk_severity(envelope.risk_ceiling):\n",
           "    if False:\n",
           (*PY, "tests/agents/test_future_interfaces.py", "tests/agents/test_envelope_gate.py")),
    Mutant("M-AG3", "Agent selector: a runtime the operator did not enable can be selected",
           "server/agents/selector.py",
           "        if not runtime.enabled:\n",
           "        if False:\n",
           (*PY, "tests/agents/test_selector.py")),
    Mutant("M-AG4", "Agent selector: a runtime below the template's minimum isolation can be selected",
           "server/agents/selector.py",
           "        if ISOLATION_ORDER[runtime.isolation_mode] < ISOLATION_ORDER[template.min_isolation]:",
           "        if False:",
           (*PY, "tests/agents/test_selector.py")),
    Mutant("M-AG5", "Agent compiler: a never-mappable capability (agent.*, system.restricted, device.*) reaches an envelope",
           "server/agents/compiler.py",
           "        if abilities.never_mappable(mapping.capability):\n            return None, [\"never_mappable\"]",
           "        if False:\n            return None, [\"never_mappable\"]",
           (*PY, "tests/agents/test_compiler.py")),
    Mutant("M-AG13", "Agent revalidation: a template version bump no longer sends older specs through revalidation",
           "server/agents/compiler.py",
           "    return current is None or spec.template_version < current.version",
           "    return current is None or spec.template_version > current.version",
           (*PY, "tests/agents/test_compiler.py")),
    Mutant("M-AG14", "Agent store: a spec altered at rest is loaded without verifying its hash",
           "server/agents/service.py",
           "            verify_spec_hash(spec)\n            and spec.spec_hash == row.spec_hash\n",
           "            True\n            and spec.spec_hash == row.spec_hash\n",
           (*PY, "tests/agents/test_service.py")),
)


def anchor_count(mutant: Mutant) -> int:
    return (ROOT / mutant.path).read_text().count(mutant.old)


def _verdict(mutant: Mutant, done: subprocess.CompletedProcess) -> str:
    """Killed means *the defending tests failed* — not that something else went
    wrong. A collection error, a compile error or an empty selection is
    reported as such, never counted as a kill."""

    output = done.stdout + done.stderr
    if done.returncode == 0:
        return "SURVIVED"
    tool = mutant.command[0]
    if tool == "lint-imports":
        return "killed" if "BROKEN" in output else f"ERROR rc={done.returncode}"
    if tool == "./gradlew":
        failed_test = "FAILED" in output and ("Test" in output or "tests completed" in output)
        return "killed" if failed_test and "Compilation error" not in output else f"ERROR rc={done.returncode}"
    return "killed" if done.returncode == 1 else f"ERROR rc={done.returncode}"


def run(mutant: Mutant) -> tuple[str, float]:
    path = ROOT / mutant.path
    original = path.read_bytes()
    text = original.decode()
    if text.count(mutant.old) != 1:
        return "ANCHOR MISSING", 0.0
    started = time.monotonic()
    try:
        path.write_text(text.replace(mutant.old, mutant.new, 1))
        env = {**os.environ, "HYPERMIND_REQUIRE_MEMORY_STACK": os.environ.get("HYPERMIND_REQUIRE_MEMORY_STACK", "1")}
        done = subprocess.run(list(mutant.command), cwd=ROOT / mutant.cwd, capture_output=True, text=True,
                              env=env, timeout=1800)
        verdict = _verdict(mutant, done)
    except subprocess.TimeoutExpired:
        verdict = "killed (timeout)"
    finally:
        path.write_bytes(original)
    return verdict, time.monotonic() - started


def main(argv: list[str]) -> int:
    if "--list" in argv:
        for m in MUTANTS:
            print(f"{m.id}  {m.guard}  [{m.path}]")
        return 0
    chosen = [m for m in MUTANTS if not argv or m.id in argv]
    survivors = 0
    for m in chosen:
        verdict, seconds = run(m)
        if not verdict.startswith("killed"):
            survivors += 1
        print(f"{m.id}  {verdict:<16} {seconds:6.1f}s  {m.guard}", flush=True)
    print(f"{len(chosen) - survivors}/{len(chosen)} mutants killed")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
