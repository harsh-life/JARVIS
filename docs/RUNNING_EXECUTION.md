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
    confinement_mode: "landlock"         # see §5 — fails closed where unavailable
    read_only_paths: []                  # extra read-only paths an allowed program needs
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

Where the kernel cannot do this (macOS, Linux before 5.13), `landlock` mode
refuses to run anything — `platform_unsupported` — rather than run it
unconfined. `confinement_mode: unconfined` is an explicit operator opt-out that
removes the boundary: a child can then read anything the server's OS user can
and open its own network connections, which on a multi-user server is a
cross-user data path through an *authorized* call (measured in
`docs/OD_A1_BR_T2.md` §3b). Whether that opt-out should exist on a multi-user
deployment at all is an owner decision (`docs/DECISION_REGISTER.md` §2B).

What confinement does **not** cover: CPU and memory (the rlimits do), a kernel
exploit, and — like every in-process boundary here — a compromised server
process itself (OD-A1).

## 6. Tests and checks

```bash
python3 -m pytest tests/ -q                             # the whole suite
python3 -m pytest tests/execution/ -q                    # fs/net/process/android/platforms
python3 -m pytest tests/runtime/test_execution_integration.py -q   # real runtime x real adapters
lint-imports --config pyproject.toml                     # 12 boundary contracts
```

All of these run in CI on every pull request, with no secrets and no
real device, host firewall, or mount-namespace privilege required.
