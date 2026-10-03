"""Resolve-then-classify — the one rule every egress path in `server/net` uses.

10 §4/§5: a destination is checked against the addresses its name
**resolves to**, never the name, and the connection then goes to exactly an
address that was checked. The egress client (`client.py`) and the external
runtime's CONNECT proxy (`egress_proxy.py`, Phase 6 slice 6B) both take the
address they connect to from here.

Two strengths of the same rule:

* `strict=False` — the egress client's long-standing rule: the first
  candidate that passes `policy.classify` (a blocked candidate is skipped,
  never used).
* `strict=True` — the proxy's, for an untrusted runtime: **every** candidate
  must pass; a name that resolves anywhere unsafe is refused outright (a
  name answering both a public and a private address is the shape of a
  rebinding attempt).

The resolver is passed in, so the caller decides how a name is looked up
and tests decide what it answers; this module decides only what is safe.
"""

from __future__ import annotations

import socket
from typing import Callable, Sequence

from server.net import policy

Resolver = Callable[[str, int], Sequence[str]]


def system_resolver(host: str, port: int) -> list[str]:
    """The system resolver's TCP answers for `host`, in its order."""

    return [info[4][0] for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)]


def checked_address(host: str, port: int, *, allow_private_net: bool, resolve: Resolver, strict: bool) -> str:
    """The address to connect to, or `policy.DestinationBlocked`."""

    candidates = [str(a) for a in resolve(host, port)]
    if not candidates:
        raise policy.DestinationBlocked("unresolved")
    chosen: str | None = None
    for candidate in candidates:
        try:
            policy.classify(candidate, allow_private_net=allow_private_net)
        except policy.DestinationBlocked:
            if strict:
                raise
            continue
        if chosen is None:
            chosen = candidate
    if chosen is None:
        raise policy.DestinationBlocked(
            f"no resolved address for {host!r} passes the egress policy (SSRF/metadata/private-range defense)")
    return chosen


__all__ = ["Resolver", "checked_address", "system_resolver"]
