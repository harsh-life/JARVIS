"""Network egress enforcement (10_NETWORK_EGRESS.md) — execution branch.

`server/net/policy.py` is the SSRF/DNS-rebinding defense: every candidate IP
a hostname resolves to is classified before anything connects to it, and
metadata/loopback are blocked unconditionally (never overridable by
`EgressPolicy.private_net`, unlike RFC1918/ULA ranges). `server/net/client.py`
is the enforcement boundary itself — see its module docstring for why it is a
small hand-built HTTP/1.1 client rather than a general-purpose one: the
"checked IP is the connected IP" guarantee (10 §5) is much easier to make
true by controlling resolve→classify→connect directly than by fighting a
general client's own internal (re-)resolution and connection pooling.

**`[IMPL]` honesty, same shape as `server/fs`'s (09 §8/OD-FS-1).** This ships
only the `mediated_proxy` enforcement mode: DNS resolution and destination
classification performed by this module before every connection, which holds
against any tool that goes through it. It does **not** hold against a tool
that opens a raw socket of its own instead — the `netns_filtered` mode 10 §3
recommends for that (kernel-level, physically unreachable) is real
infrastructure work for a production deployment, not shipped here.
Mechanically, `server.tools`/`server.modeltools`/`server.agent` are barred by
an import-linter contract from importing `socket` at all (`pyproject.toml`),
which is what makes "goes through it" actually true for this codebase's own
adapters, short of the kernel-level guarantee.

What this package does **not** do: authorize anything. It enforces the
*physical* network boundary of an operation `04`/`07` already authorized,
returning `ExecutionError(EGRESS_DENIED | DESTINATION_UNRESOLVED |
RESPONSE_TOO_LARGE | TIMEOUT)` on a boundary violation.
"""

from __future__ import annotations

from server.net.client import EgressClient
from server.net.destinations import hostname_allowed
from server.net.policy import DestinationBlocked, classify

__all__ = ["DestinationBlocked", "EgressClient", "classify", "hostname_allowed"]
