# ADR — Reducing the application-level RCE blast radius (multi-user isolation)

**Status:** PROPOSED — nothing in this document is implemented.
**Date:** 2026-11-27 · **Context:** security hardening pass (docs/DECISION_REGISTER.md §2P)
**Relates to:** OD-A1 (accepted residual for the pilot, option (a)); options (b)
per-user process isolation and (c) per-user store isolation, "deferred, not
rejected"; INV-20; `docs/OD_A1_BR_T2.md` (the measured blast radius).

## 1. The problem, stated honestly

All users share **one server process**. Service-layer authorization (the
five-dimension engine, visibility, handle-only secrets, the sandbox, egress)
is enforced *by code in that process*. If an attacker achieves code execution
inside it — a deserialization bug, a dependency compromise, a parser flaw — the
measured BR-T2 rows show what becomes reachable (accepted class, OD-A1 (a)):

* every user's rows in the relational store (it holds one DB connection pool);
* forging a `SecretRequester` for any user, and the SecretStore's unwrapped data
  key while the store is unlocked (it is in process memory);
* constructing a process-local `SuperuserGrant` (`server/secrets/requester.py`);
* every user's sandbox directory (one OS user owns them all);
* raw network capability (the process can open sockets; egress policy is
  enforced on the code paths that use `server.net`, not on the process).

**Passing the authorization and sandbox test suites does not change this.**
Those tests prove the code paths enforce policy; they cannot prove anything
once the code itself is the attacker's. The single-process architecture is a
**known application-level RCE blast-radius limitation**, and every document
must keep saying so until a boundary below is built and measured.

What already narrows it: child processes are kernel-confined (Landlock +
seccomp; they cannot read the DB, other sandboxes, `/proc/<server>/environ`,
or open sockets); contained runtimes (Browser Use) run in gVisor containers
with no network interface. Those boundaries contain *tool* compromise, not
*server* compromise.

## 2. Goals and non-goals

Goals: an RCE in the component that handles untrusted input or runs agent work
should not, by itself, yield (1) other users' data, (2) secret plaintext or the
KEK-derived key, (3) superuser authority, (4) unrestricted network access.

Non-goals: defending against a kernel exploit or a malicious host operator;
a microservice rewrite; changing the authorization model (it is sound — the
problem is where it runs).

## 3. Candidate boundaries

Each is evaluated on: threat addressed · new trust boundary · what crosses it
· containment · how it is tested · what remains possible after compromise.

### B1. Separate worker processes for untrusted agent work

* **Threat:** an RCE through model output handling, tool output parsing,
  document/vault parsing, or a dependency used only by agent work.
* **Boundary:** the agent loop (proposal parsing, observation handling, model
  adapters) runs in a worker process per task (or per user), started by the
  core, with **no DB credentials, no KEK, no superuser token** in its
  environment, under its own OS user or at least Landlock/seccomp.
* **Crosses:** a task-scoped, authenticated RPC channel (Unix socket pair
  created by the core, never a listening socket) carrying *proposals* out and
  *authorized results* in. Every action is still authorized in the core.
* **Containment:** a compromised worker can lie in its proposals (already the
  threat model: model output is never authority) and see its own task's data.
  It cannot read the DB, resolve secrets, or reach other tasks.
* **Tests:** the BR-T2 harness gains an "in-worker RCE" attacker model: code
  run inside the worker tries every row of §1; each must be contained.
  Import-linter + a runtime check that the worker's environment has no
  credential.
* **After compromise:** that task's context, its model-provider key *if* the
  worker calls the model directly (prefer: model calls proxied by the core, as
  the Model Gateway already does for external runtimes), and anything the user
  confirms.

### B2. Isolated SecretStore / key operations

* **Threat:** secret plaintext and the unwrapped data key in the main process.
* **Boundary:** a small key-service process (or the OS keyring / a TPM/KMS
  where available) holds the KEK-derived key; the core asks it to decrypt *a
  specific handle for a specific requester class*, and it enforces class
  restrictions and audits independently.
* **Crosses:** handle + requester assertion, authenticated by a per-boot
  socket credential (SO_PEERCRED: only the core's UID/PID may connect).
* **Containment:** a core compromise can still *request* decryptions as the
  core may (so the core must not be the component handling untrusted input —
  pair with B1), but cannot dump the key or bulk-decrypt silently: rate limits
  and audit live outside the compromised process.
* **Tests:** BR-T2 rows 7–8 re-measured: key material absent from the core's
  memory (scan of `/proc/self/mem` in a test build), decrypt requests audited
  by the key service.
* **After compromise of the core:** on-demand decryption of secrets the core is
  entitled to; not the key; not undetected bulk access.

### B3. Database access through narrowly scoped service interfaces

* **Threat:** one connection pool with access to every user's rows.
* **Boundary:** PostgreSQL row-level security keyed on a per-transaction
  `app.user_id` set by a trusted core, *or* per-component DB roles (the worker
  has none; the scheduler has only `scheduled_jobs`; the console role is
  read-only).
* **Crosses:** SQL from components to PostgreSQL, under distinct roles.
* **Containment:** RLS stops a confused-deputy bug and limits a compromised
  component to its role's grants; it does not stop a compromised core that can
  set `app.user_id` arbitrarily — hence B1 first.
* **Tests:** per-role permission tests (a deliberate cross-user SELECT under
  the worker/scheduler role fails); RLS policy tests on PostgreSQL in CI.
* **After compromise:** whatever that role can do.

### B4. Process-local administrative authority and superuser grants

* **Threat:** `SuperuserGrant` is a process-local object; RCE can construct one.
* **Boundary:** superuser actions move to a separate, short-lived admin
  process (or CLI) that verifies the superuser token itself and talks to the
  core over an authenticated channel; the long-running core never holds the
  token or a grant.
* **Crosses:** signed, single-use admin commands.
* **Containment:** core RCE cannot mint operator authority.
* **Tests:** the existing contract "Only the gateway reaches superuser
  authority" plus a test that the core process has no superuser token in its
  environment and no code path that constructs a grant.
* **After compromise:** none of the operator controls, unless the admin process
  itself is compromised.

### B5. Per-user filesystem boundaries

* **Threat:** one OS user owns every sandbox root.
* **Boundary:** per-user sandbox directories owned by per-user (or per-task)
  UIDs, or per-task mount namespaces; the worker (B1) gets only its user's
  directory.
* **Containment:** a worker or confined child can never open another user's
  files, even by path.
* **Tests:** extend `test_process_confinement.py` / FS-T9 with a worker-RCE
  attacker reading another user's sandbox.
* **After compromise:** the user's own files.

### B6. Network capability isolation

* **Threat:** the server process can open any socket.
* **Boundary:** workers run with no network namespace access (seccomp
  `socket()` refusal as for confined children); all egress goes through an
  egress process enforcing `EgressPolicy` (like the per-run CONNECT proxy).
* **Containment:** a worker compromise cannot exfiltrate except through policy.
* **Tests:** NET-T* re-run with an in-worker attacker.
* **After compromise:** allowed destinations only (and see
  `docs/ADR_EGRESS_TLS_ORIGIN.md` for what "allowed" means at the TLS layer).

### B7. Crash and compromise containment

Supervisor (systemd) restarts components independently; a worker crash fails
one task closed; the core's state is not shared memory. Compromise detection:
the key service and DB roles audit outside the compromised process.

### B8. Communication authentication between processes

Unix socket pairs created by the parent (no listener), or listeners checked
with SO_PEERCRED (UID + PID), plus per-boot random tokens. Messages are
schema-validated on both sides (the existing Pydantic contracts in `shared/`).
No TCP between local components.

### B9. Operational complexity

More units to deploy and monitor; a supervisor tree; per-user UIDs need root
at install time (not at run time). Mitigate with one systemd template unit per
component and the existing `deploy/` bootstrap.

### B10. Migration cost and compatibility

The composition root already separates ports from implementations (the agent
reaches authorization and secrets only through `server/agent/ports.py`), so B1
is mostly "satisfy those ports over RPC". B2 replaces the in-process
`EncryptedLocalSecretStore` behind the same `SecretStore` interface. B3 needs
PostgreSQL (already the pilot store, H-1). API and Android contracts do not
change.

## 4. Recommendation — phased

1. **Phase I — worker isolation (B1 + B6 for workers).** Highest value: moves
   the code that touches untrusted model/tool output out of the process that
   holds credentials. Model calls proxied by the core (reuse the Model Gateway).
2. **Phase II — key service (B2) and admin authority out of the core (B4).**
3. **Phase III — DB roles / RLS (B3) and per-user filesystem UIDs (B5).**
4. Re-run BR-T2 after each phase with the new attacker models; only rows that
   become *contained* in that measurement may be described as fixed.

Until Phase I is built and measured, OD-A1 (a) remains the accepted residual,
the real-data gate stays closed for multi-user deployments, and documentation
must describe the single-process architecture as a known blast-radius
limitation.
