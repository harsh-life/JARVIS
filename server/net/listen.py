"""Internal listening sockets (docs/29 §21 item 8, Phase 6 slice 6A).

`server/net` is where the server's raw socket use is confined (the boundary
test in `tests/security_core/test_fail_closed_and_boundaries.py`). Egress is
the rest of this package; this module is its one inbound counterpart: the
socket an internal listener (the HTTP Model Gateway) serves on.

It accepts only a binding the configuration has already validated as
internal (`server/config/schema.py`, `_internal_listen`): a Unix socket at an
absolute path, or a loopback address. A Unix socket is created with its
permissions set before it accepts anything — only its owner can connect
(`0600`) — and an existing file at that path that is not a socket is
refused, never replaced. Like the rest of this package, it authorizes
nothing.

A host whose Python has no Unix-domain sockets (Windows) cannot serve a
`unix:` binding: it is refused with `UnixSocketsUnavailable`, never quietly
served on TCP instead. A TCP loopback binding is unaffected.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import stat

UNIX_PREFIX = "unix:"


class UnixSocketsUnavailable(RuntimeError):
    """This host cannot provide a Unix-domain listener (and nothing listens)."""


def unix_listener_missing() -> str | None:
    """What this host's Python lacks to serve a Unix-domain socket with asyncio,
    or None. Windows' CPython exposes neither `socket.AF_UNIX` nor
    `asyncio.start_unix_server`."""

    missing = [name for name, present in (
        ("socket.AF_UNIX", hasattr(socket, "AF_UNIX")),
        ("asyncio.start_unix_server", hasattr(asyncio, "start_unix_server")),
    ) if not present]
    return ", ".join(missing) or None


def require_unix_listener(binding: str) -> None:
    missing = unix_listener_missing()
    if missing is not None:
        raise UnixSocketsUnavailable(
            f"internal listener {binding!r} needs Unix-domain sockets, which this host lacks ({missing}); "
            "nothing is listening"
        )


def internal_listen_socket(binding: str) -> socket.socket:
    """A bound, not yet listening, stream socket for `binding`."""

    if binding.startswith(UNIX_PREFIX):
        require_unix_listener(binding)
        path = binding[len(UNIX_PREFIX):]
        with contextlib.suppress(FileNotFoundError):
            if not stat.S_ISSOCK(os.lstat(path).st_mode):
                raise RuntimeError(f"internal listener: {path} exists and is not a socket")
            os.unlink(path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous = os.umask(0o177)
        try:
            sock.bind(path)
        finally:
            os.umask(previous)
        os.chmod(path, 0o600)
        return sock
    host, _, port = binding.rpartition(":")
    host = host.strip("[]")
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, int(port)))
    return sock


def remove_internal_socket(binding: str) -> None:
    """Remove a Unix listener's socket file (nothing for a TCP binding)."""

    if binding.startswith(UNIX_PREFIX):
        path = binding[len(UNIX_PREFIX):]
        with contextlib.suppress(FileNotFoundError):
            if stat.S_ISSOCK(os.lstat(path).st_mode):
                os.unlink(path)


__all__ = [
    "UNIX_PREFIX",
    "UnixSocketsUnavailable",
    "internal_listen_socket",
    "remove_internal_socket",
    "require_unix_listener",
    "unix_listener_missing",
]
