"""The filesystem sandbox (09_FILESYSTEM_SANDBOX.md) — execution branch.

Implements 09's containment guarantee: an operation reaches the host
filesystem only through `paths.resolve`'s TOCTOU-resistant, `dir_fd`-walking
containment check (see `server/fs/paths.py`'s module docstring for why a
plain `path.startswith(root)` check is explicitly *not* what this module
calls a sandbox, per 09 §8), and only under a root `paths.allocate_root`
derived from the request's own authorized `user_id`/`graph_id` — never a
value the request supplies.

**One deliberate, `[IMPL]` reading of 09 §1 worth stating plainly.** 09's own
illustrative example writes `"sandbox_root": "/…"` as if the scope value were
a path. This branch does not accept that literally: `resource_scope
["sandbox_root"]` is treated as an opaque **label**
(`paths.sanitize_label` — no path separators, no `.`/`..`), and the *actual*
physical root is always `allocate_root`'s own derivation under the operator's
configured `filesystem.base_root`. The alternative — trusting a
caller-supplied string as a real filesystem path, even indirectly — is
exactly the "physical location taken raw from a client/agent" 09 §1 forbids;
a resource_scope value is set by whichever human created the `CapabilityGrant`
(PRD §13), and nothing upstream of this module validates that it isn't
`/etc` or `../../root`. Treating it as a label rather than a path is what
makes 09 §6's "A's sandbox derivation cannot produce B's root" true by
construction instead of by convention.

What this package does **not** do: authorize anything. `04`/`07` have
already run before a `ToolInvocation` reaches an adapter; this package
enforces the physical boundary of an operation already decided to be
allowed, and returns `ExecutionError(FORBIDDEN_PATH | SANDBOX_VIOLATION |
RESOURCE_EXHAUSTED | ...)` — never a `403`/authorization-shaped decision —
when the physical boundary itself is violated.
"""

from __future__ import annotations

from server.fs.paths import (
    SandboxPath,
    allocate_root,
    cleanup_task_temp,
    open_root_fd,
    resolve,
    sanitize_label,
    task_temp_root,
)
from server.fs.sandbox import DirEntryInfo, FilesystemSandbox

__all__ = [
    "DirEntryInfo",
    "FilesystemSandbox",
    "SandboxPath",
    "allocate_root",
    "cleanup_task_temp",
    "open_root_fd",
    "resolve",
    "sanitize_label",
    "task_temp_root",
]
