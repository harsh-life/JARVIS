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
