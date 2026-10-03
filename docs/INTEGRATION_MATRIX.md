# Cross-subsystem integration matrix (Phases 1–6)

Produced for the post-Phase-6 Architecture Consolidation + Integration
Hardening milestone. This treats the repository as one composed system and
asks, boundary by boundary, "does real cross-subsystem test coverage exist,
or only per-subsystem unit coverage that would miss a seam?" Each row names
the existing tests that already exercise the full boundary (not just one
side of it), and — where a genuine gap was found — the test and fix that
closed it.

Methodology: for each boundary, the actual code path was read end to end
(not inferred from names), and existing tests were checked for whether they
drive the *composed* system (real auth, real engine, real storage — the
`make_harness`/`h` fixture in `tests/runtime/conftest.py`) rather than a
mocked slice. A row is marked **gap found and fixed** only where a concrete,
reproducible failure was confirmed red before any fix.

| # | Boundary | Existing coverage | Verdict |
|---|---|---|---|
| 1 | Auth → principal → graph → grants → Agent Factory | `tests/runtime/conftest.py`'s harness runs the *real* OIDC → device → token chain for every single agent test (not a shortcut); `tests/agents/test_phase34_isolation.py::test_a_member_of_the_same_graph_reaches_nothing_of_anothers_agent`, `tests/agents/test_resource_authorization.py::test_agent_t9_another_user_sees_nothing` | **already covered** |
| 2 | Compiler → selector → compiled envelope → authorization engine | `tests/agents/test_compiler.py`, `tests/agents/test_selector.py`, `tests/agents/test_envelope_gate.py` (envelope-as-ceiling-vs-real-grants is the file's own subject) | **already covered** |
| 3 | Native runtime → Task → Tool Gateway → execution → usage/audit | `tests/agents/test_agent_runs.py`, `test_agent_tools.py`, `test_run_control.py`; `tests/integration/test_acceptance_e2e.py` cases A–J assert "no execution occurred" from the `UsageEvent`/`AuditEvent` ledgers, never from the model's narration | **already covered** |
| 4 | StandingDelegation → DelegatedPrincipal → trigger loop → run admission → execution → revocation | `tests/agents/test_delegation_grant.py`, `test_delegation_contracts.py`, `test_delegated_principal.py`, `test_trigger_planning.py`, `test_unattended_runs.py` (incl. `test_losing_any_ground_mid_run_stops_the_very_next_step`, parametrized over delegation-revoked/owner-suspended/agent-paused), `test_unattended_hardening.py`; `tests/integration/test_br_t2_unattended_rows.py` | **already covered** |
| 5 | Agent Gateway → run token → Model Gateway → model provider → metering | `tests/agents/test_gateway_runs.py`, `test_gateway_tokens.py`, `test_model_gateway.py`, `test_model_gateway_http.py`, `test_model_routing_runs.py` | **already covered** |
| 6 | External Browser Use runtime → Agent Gateway → Model Gateway → container → egress proxy → Browser Use → result → owner inbox | `tests/agents/test_browser_runs.py`, `test_browser_kill.py`, `test_browser_live.py` (real image, real rootless gVisor), `tests/integration/test_br_t2_container_rows.py` (new attacker model: "compromised runtime") — built this phase with the same test-first discipline | **already covered** |
| 7 | Scheduler → agent trigger planning; scheduler never an execution authority | `tests/scheduler/test_scheduler_boundaries.py` (`test_the_scheduler_never_names_the_runtime_or_an_operation`, `test_the_scheduler_imports_only_storage_audit_config_and_schemas`, a mutation-style contract-violation test), `tests/agents/test_agent_reminders.py`, `test_reminder_tap.py` | **already covered** |
| 8 | Memory → auth → owner/graph filtering → bounded hydration; notebook/Mem0/Vault stay separate | Agent-run hydration goes through the *same* `env.memory` path the ordinary user-facing runtime uses (`server/agent/runtime.py`), not a separate implementation; `assemble_input()` (`server/agents/instructions.py`) — the only thing an external (Browser Use) run ever receives as its task — is a pure function of the compiled spec, with no DB/session/memory access by construction, so an external runtime cannot reach memory/vault even in principle; `tests/agents/test_notebook.py`, `tests/memory/`'s MEM-T suite, `tests/integration/test_br_t2_memory_rows.py` | **already covered**, confirmed by reading `assemble_input`'s signature, not assumed |
| 9 | Judge → evaluation/observation; Judge cannot authorize/confirm/grant/escalate/execute | `import-linter` contract AF-C5 ("the Judge never imports the Agent Factory") is a *static*, CI-enforced guarantee — stronger than a runtime test for this specific claim, since it makes the violation a build failure, not a maybe-caught-by-a-test; `tests/agents/test_agent_judge_console.py`; `docs/OD_A1_BR_T2.md` row 38 (the one accepted Judge residual: approved guidance is global, not per-user) | **already covered** |
| 10 | Android → perception → task interaction → server authorization → confirmation/step-up → device execution | `tests/execution/test_android.py`, `test_device_channel_contract.py`, `test_device_conformance.py`, `test_device_hub.py`; `android/:contract`'s own JVM tests cross-check `shared/android/device_mapping.json` against the Kotlin side | **already covered** |
| 11 | Voice → transcription/model interaction → task/runtime; voice cannot confirm or bypass step-up | `tests/voice/test_voice_privacy_and_authority.py` — explicitly: `test_voi_t4_a_spoken_yes_does_not_confirm_a_pending_action`, `test_voi_t4_voice_cannot_stand_in_for_step_up`, `test_voi_t4_voice_code_cannot_reach_confirmation_or_step_up`, `test_voi_t4_there_is_no_voice_task_path` | **already covered**, and unusually explicit about it |
| 12 | Usage/budget → concurrent admission → execution → cancellation → refunds/zero-metering | The design principle (`Working Markdown/13_USAGE_RATE_BUDGET.md`) is "a call that returns without a `UsageEvent` is a defect" — i.e. metering happens only for real work, so there is no "refund" path to test by that name; the inverse risk (a cancelled call still metered for work it never did) is what `tests/integration/test_acceptance_e2e.py::test_case_i_cancelling_a_task_kills_its_running_process` and `test_case_i_cancelling_a_paused_task_drops_the_action` assert, from the ledger | **already covered**; "refund" is not a concept this system needed to build |
| 13 | Audit → every consequential lifecycle/security event | **Gap found and fixed** — see below | **gap found and fixed** |
| 14 | Stop/recovery across all named triggers | Browser runs: `tests/agents/test_browser_kill.py` (one kill path, 6E/6F this phase) exhaustively covers owner stop/pause/delete, every operator stop, deadline, budget/ceiling refusal, token revocation elsewhere, container crash, restart. Native runs: `tests/integration/test_hardening_regressions.py::test_a_user_suspended_mid_task_stops_at_the_next_step`; `tests/agents/test_run_control.py`; `tests/agents/test_unattended_runs.py::test_losing_any_ground_mid_run_stops_the_very_next_step` covers the delegated-native case specifically (confirmed by reading `server/agent/runtime.py`'s three `principal_active` checkpoints and `server/composition/agents.py::delegated_principal_active`) | **already covered** |
| 15 | PostgreSQL vs SQLite behavior for every integration-sensitive path | Every test in `tests/` (this file's new tests included) runs on both SQLite (CI's `checks` job) and PostgreSQL (CI's `postgres` job, trust auth, one fresh DB per test) on every push; no SQLite/PostgreSQL-specific divergence surfaced across Phases 5–6's development, each of which ran both suites before every commit this session | **covered by dual-DB CI**, not a per-feature unit test concern |
| 16 | Migration state and fresh-install state | `alembic upgrade head && alembic downgrade base && alembic upgrade head && alembic check` is run (and was run after every schema-touching commit this session); CI's `postgres` job round-trips migrations on a fresh database every run | **covered by CI** |
| 17 | Configuration defaults vs production behavior; dangerous features opt-in | Checked directly for every Phase 6 feature: `AgentContainersConfig.enabled: bool = False`, `agents.unattended_enabled` off by default, `BROWSER_USE_IMAGE: str | None = None` until a reviewed PR pins it, `ignore_cgroups: bool = False`; `tests/agents/test_container_reconciler.py::test_containers_are_off_by_default` and siblings | **already covered** |
| 18 | External runtime image: digest pin, launch, gVisor, rootless, proxy, telemetry, cleanup/reconciliation | Built and heavily tested this phase (6C/6D/6E/6F) and again when the digest was actually pinned (`tests/agents/test_registry.py::test_the_pinned_browser_use_image_is_an_immutable_digest`); `tests/execution/test_containers_live.py`, `tests/agents/test_browser_live.py` run for real against rootless gVisor in CI | **already covered** |

## The one gap: boundary 13, audit completeness

`server/auth/errors.py`'s own module docstring states a `[LOCKED]` contract:

> any validation failure → `401 unauthenticated`, an `AuditEvent`, **no
> session, no user mutation**

`server.auth` cannot write that `AuditEvent` itself — it sits below
`server.security` in the module layering (`16` §2) and never imports it, by
design, so the *writing* half of this promise belongs to its caller,
`server.gateway`. A mechanical sweep of every `AuditAction` enum member
against every place it is actually referenced in `server/` found two members
of the 144-entry registry that are **never emitted anywhere**:

- `ACCESS_TOKEN_REJECTED` (`"session.token.rejected"`) — `resolve_principal()`
  (`server/auth/sessions.py`) has nine distinct rejection paths (absent,
  unknown, revoked, expired, device revoked, user not active, session
  missing, session expired, session/device/user mismatch), none of which
  reached the `AuditLogger` anywhere. This runs on **every authenticated
  request in the system** — a sustained credential-stuffing or stolen/replayed
  token probe left zero trace in the durable audit log.
- `STEP_UP_REQUIRED` (`"session.step_up.required"`) — `require_step_up()`
  raising `StepUpRequired` for a stale-but-valid token attempting a
  sensitive operation (device credential rotation) was likewise silent.

Two other members a first grep flagged (`SECRET_SET`/`SECRET_RESOLVED`/
`SECRET_DELETED`/`SECRET_ROTATED`, and by extension the whole secret-audit
path) turned out to be a **false positive**: `server/secrets` speaks through
its own narrow `SecretAuditSink` port using plain strings (it cannot import
`AuditAction` either, for the same layering reason), and
`server/security/audit.py`'s `AuditLogger.record_secret_event` translates
those strings back to the real enum via `SECRET_ACTION_BY_NAME` — a
deliberate, working indirection, not a gap. Checking this by reading the
translation table rather than trusting the first grep is what told them
apart.

**Fix** (`server/gateway/deps.py`, `server/gateway/routers/auth.py`): both
rejection points now write the `AuditEvent` before re-raising, in the same
request transaction `server/gateway/deps.py`'s own docstring already
documents as the mechanism for this ("a security refusal... commits...
what commits is the audit trail and nothing else"). No identifying fields
are recorded before a principal exists to name one — the `[LOCKED]`
anti-enumeration rule (`02` §1.6) that kept the *client-facing* message
generic was never in question; what was missing was the *server-side*
record.

**Test** (`tests/integration/test_hardening_regressions.py`):
`test_a_rejected_access_token_is_audited` (parametrized over an unknown
bearer, a revoked token, and an expired token) and
`test_a_required_step_up_is_audited`. Both confirmed red before the fix.

**Mutation coverage**: M-AG251 (deps.py's audit call made dead code),
M-AG252 (auth.py's audit call made dead code) — both killed.
