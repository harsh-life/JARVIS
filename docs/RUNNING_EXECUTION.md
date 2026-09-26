# Running the `execution` branch

This extends `docs/RUNNING_RUNTIME.md` (install, KEK, bootstrap, login, the
task lifecycle are unchanged). What is new is the **execution layer** (`09`
filesystem sandbox, `10` network egress, `08`'s server-side Android/Shizuku
half, and the `system.restricted` process executor) — the mechanism through
which an authorized tool operation becomes an actual operation on a target
platform, described in `server/execution/__init__.py`'s module docstring.

Read `docs/CAPABILITY_MATRIX.md` and `docs/DECISION_REGISTER.md` (both updated
by this branch) before configuring it.

## 1. What changed for an operator

`build_application`'s default tool set changed: a fresh clone with no
`execution:` section in its config now registers real `files.read`,
`files.write`, `net.request`, and `system.shell` tools (plus two Android
tools using the only transport this branch ships — see §4), instead of no
execution tools at all. This is safe by construction, not merely by
intention:

- **`file.read`/`file.write` are immediately usable.** Every sandbox root is
  derived under `execution.filesystem.base_root` from the *authorized*
  principal's own `user_id`/`graph_id` (never a value a request supplies —
  `server/fs/__init__.py`), so granting the capability is what makes it
  reachable, not configuration.
- **`net.request` and `system.shell` are registered but inert by default.**
  An empty `execution.process.allowed_executables` list and an empty
  `execution.network.default_destinations` list mean granting either
  capability authorizes nothing to actually run until an operator opts
  specific executables/destinations in.

## 2. Configure the execution layer

All optional; every default is the most restrictive one.

```yaml
execution:
  filesystem:
    base_root: "./data/sandboxes"     # every sandbox root is allocated under this
    containment_mode: "mediated"      # the only mode this branch implements — see
                                        # server/fs/paths.py; "mount_isolated" (a real
                                        # deployment's stronger mechanism) refuses to
                                        # start rather than pretend to be configured
    max_file_bytes: 25000000
    max_sandbox_bytes: 250000000
    max_archive_entries: 10000
    max_archive_uncompressed_bytes: 250000000

  network:
    enforcement_mode: "mediated_proxy"   # the only mode this branch implements — see
                                          # server/net/client.py; "netns_filtered" likewise
                                          # refuses to start
    connect_timeout_seconds: 5.0
    read_timeout_seconds: 10.0
    max_response_bytes: 5000000
    max_redirects: 3
    default_destinations: []             # the generic net.request tool's allow-list —
    default_internet: false              # empty + false = no egress until you opt in
    default_private_net: false

  process:
    allowed_executables: []              # nothing runs until you list something here
    default_timeout_seconds: 10.0
    max_timeout_seconds: 60.0
    max_output_bytes: 1000000
    confinement_mode: "landlock"         # the only value — see §5; fails closed where unavailable
    read_only_paths: []                  # extra read-only paths an allowed program needs
    break_glass:                         # §5.1 — per-task, superuser-activated, off by default
      enabled: false
      allowed_executables: []            # a separate list; never widens allowed_executables
      max_window_minutes: 15             # ceiling on one activation's window (1–15)
      max_invocations: 1                 # ceiling on one activation's run count
```

## 3. What "mediated" containment/egress means, honestly

Both `containment_mode: mediated` and `enforcement_mode: mediated_proxy` are
real, syscall-level enforcement — `server/fs/paths.py`'s `dir_fd`-walking,
`O_NOFOLLOW`-at-every-hop path resolution, and `server/net/client.py`'s
resolve→classify→pin-the-checked-IP connection — proven against live
traversal/symlink/TOCTOU and SSRF/DNS-rebinding attempts in
`tests/execution/`. What they are **not** is a kernel-level guarantee against
a tool that bypasses this module entirely (opens a raw socket, or reads a
path via a different mechanism). That gap is closed today only for this
codebase's own adapters, mechanically, by an `import-linter` contract
(`pyproject.toml`) that forbids `server.tools`/`server.modeltools`/
`server.agent` from importing `socket`/`subprocess` directly. Real
containment against an arbitrary compromised process — mount-namespace
isolation, a network-namespace-filtered egress path (09 §8 / 10 §3's `[REC]`
mechanisms) — is real infrastructure work for a production deployment, not
shipped by this branch. Do not represent `mediated`/`mediated_proxy` as
having closed that gap; they have not, and `FilesystemSandbox`/`EgressClient`
refuse to start under a config value naming the stronger mode rather than
silently run the weaker one under it.

## 4. Android — server side only

`server/execution/android.py` is the full capability→operation→primitive
mapping (08 §2/AND-006) and dispatch contract. There is no `android/` client
in this repository yet (16 §1), so every registered Android tool uses
`UnavailableDeviceTransport`: every call fails deterministically with
`device_unavailable` rather than hanging or silently no-op'ing. Wiring a real
transport (a websocket/push channel to a connected device) is a
configuration change — implement `server.execution.android.DeviceTransport`
and pass it to `android_app_interact_tool`/`android_device_read_tool`
(`server/composition/execution_tools.py`) — not a rewrite of the
authorization or dispatch path.

## 5. `system.restricted` — still the highest-risk surface

`system.shell`'s `run_shell_command` is `high_irreversible`: it always pauses
for confirmation and step-up, and (07 §4/08 §6) is never folded into any
ordinary capability's grant. It runs only allow-listed executables
(`execution.process.allowed_executables`), exec'd by the absolute path the
allow-list entry resolved to — never a shell string, never a PATH search the
model's `env_overrides` could redirect — with a from-scratch environment
(loader variables such as `LD_PRELOAD` refused), POSIX resource limits
including a file-size cap, and whole-process-group cleanup on timeout or task
cancellation (`server/execution/process.py`). Leaving the allow-list empty (the
default) keeps the capability registered but practically unreachable.

**The allow-list is policy; confinement is the boundary.** An allow-listed
program is not this codebase — `server.fs`/`server.net` do not constrain what
it does. So every child is confined by the kernel before it `exec`s
(`server/execution/confinement.py`, `confinement_mode: landlock`, the default):

| A confined child … | because |
|---|---|
| can read/execute system directories (`/usr`, `/bin`, `/lib*`, a few `/etc` files libc needs) | Landlock read-only rules |
| can read/write only inside its working directory — the task's own temp root, removed when the task ends | Landlock read-write rule, no execute right there |
| cannot read other users' sandboxes, the database, the config, `/proc/<server>/environ` (where an `env:` KEK and the superuser token live), `/home`, `/root` | everything else is denied |
| cannot open any socket — TCP, UDP, Unix, `io_uring` | seccomp (`socket`/`io_uring_setup` → `EPERM`) and Landlock TCP rules |
| cannot signal, ptrace, or reach an abstract socket of the server process | Landlock scoping (kernel ABI ≥ 6) |
| cannot gain privileges through setuid binaries | `no_new_privs` |

Where the kernel cannot do this (macOS, Linux before 5.13), nothing runs —
`platform_unsupported` — rather than run unconfined. `landlock` is the only
`confinement_mode`: a config still saying `unconfined` fails to load, with a
message pointing at `break_glass` (20 §2.5). There is no global switch that
turns confinement off.

What confinement does **not** cover: CPU and memory (the rlimits do), a kernel
exploit, and — like every in-process boundary here — a compromised server
process itself (OD-A1).

### 5.1 Break-glass: unconfined execution for one task (20 §2, OD-EXEC-2)

For recovery when confinement itself is in the way. It removes **only** the
kernel layer (Landlock + seccomp) from `system.restricted` children of **one**
task — authentication, authorization, capability activation, the
`high_irreversible` confirmation and step-up, the executable allow-list, argv-
only exec, the from-scratch environment and loader-variable refusal, rlimits,
timeout, output caps, process-group kill, `/cancel`, metering, audit, rate
limits, and every other tool's boundary (`09` for `file.*`, `10` for
`net.request`, the SecretStore) all still apply.

Two keys, both required:

1. **Operator enablement** — `execution.process.break_glass.enabled: true`, with
   the executables it may cover in `break_glass.allowed_executables` (its own
   list: nothing here is added to `allowed_executables`). The server logs a
   warning at startup. On its own this runs nothing unconfined.
2. **Per-task activation, by the superuser** —

   ```
   POST /api/v1/admin/control/break-glass
   Authorization: Superuser <HYPERMIND_SUPERUSER_TOKEN>
   {"task_id": "…", "user_id": "<the task's owner>", "executables": ["strace"],
    "max_invocations": 1, "expires_in_seconds": 600, "reason": "repair_landlock_rule"}
   ```

   The task must be live in this server process (typically paused on the very
   `run_shell_command` confirmation it is meant for); `user_id` must be its
   owner; every executable must be on `break_glass.allowed_executables`;
   `max_invocations` (default 1) and the window (default and ceiling
   `max_window_minutes`) may not exceed the config, and the window never
   outlasts the task's own remaining time. `reason` is an identifier
   (`^[a-z][a-z0-9_]{0,31}$`) because it is written to the audit trail, which
   carries no free text. Users, workers, models, prompts, and recovery have no
   way to activate one; no proposal field or tool argument names confinement.

The owner then confirms the run as usual (with step-up). Only if a live record
matches that task, its owner, and the executable does the child start without
the kernel layer; each such run spends one invocation.

A record ends at the first of: its invocations spent, its window passing, the
task ending (completed, failed, cancelled), a breaker trip or operator stop, or
`POST /api/v1/admin/control/break-glass/revoke {"task_id", "reason"}`.
`GET /api/v1/admin/control/break-glass` lists live records, and the task's own
status carries `break_glass_active: true` while one exists. Records live in
server memory only: a restart ends them all.

Audit: `break_glass.activated` (superuser; record id, task, reason, limits,
executables), `break_glass.invoked` (system; record id, task, outcome, exit
code, duration, a SHA-256 prefix of the argv — never the argv — and the
executable), `break_glass.ended` (system; the reason), and
`control.break_glass` for every refused activation or revoke.

**Inside an active window the child can read anything the server's OS user can
— other users' sandboxes, `/proc/<server>/environ` — and open its own network
connections** (`docs/OD_A1_BR_T2.md` §3b rows 17a–17e). On a multi-user server
with real data every activation is a cross-user exposure event. Break-glass
does not make the server isolated; it is one more audited way to reach the
accepted OD-A1 residual.

**Hosts without Landlock (macOS, old kernels; OD-BG-2).** Normal
`system.restricted` is unsupported there — it fails closed. Development on such
a host uses Linux/WSL2, or a per-task break-glass activation on a disposable,
data-free deployment. The test suite follows the same rule: tests that need a
normal run to succeed are skipped with an explicit reason on such hosts, and
executor-mechanics tests run on the break-glass path through a clearly-named
test-only record store (`tests/support.py`). `JARVIS_TEST_SIMULATE_NO_LANDLOCK=1`
runs the suite as such a host would.

## 6. Tests and checks

```bash
python3 -m pytest tests/ -q                             # the whole suite
python3 -m pytest tests/execution/ -q                    # fs/net/process/android/platforms
python3 -m pytest tests/runtime/test_execution_integration.py -q   # real runtime x real adapters
lint-imports --config pyproject.toml                     # 12 boundary contracts
```

All of these run in CI on every pull request, with no secrets and no
real device, host firewall, or mount-namespace privilege required.
