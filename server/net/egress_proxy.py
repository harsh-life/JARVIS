"""The external runtime's egress proxy — Phase 6 slice 6B (OD-AF-12/13).

A Browser Use container has no network interface (OD-AF-12). Its one way to
the internet is this proxy, on a per-run Unix socket mounted into it, and
this proxy is **authoritative** (OD-AF-13): Browser Use's own
`allowed_domains`, or anything else the runtime claims, is advisory at most.

Per connection, exactly one exchange is understood:

    CONNECT <host>:443 HTTP/1.1      (authority-form, nothing else)

and it is decided, in order, by deterministic JARVIS code:

1. the request head: bounded (8 KiB), on time (`header_timeout`), strictly
   `CONNECT host:port HTTP/1.1` — any other method or form is refused;
2. the run's limits: total and concurrent connections;
3. the host: a host name (an IP literal is refused, even a public one), and
   exactly one of the run's hosts (`ProxyPolicy.hosts`: exact names, no
   wildcard — built by JARVIS from the spec ∩ the operator's policy, never
   from the runtime);
4. the port: 443 only;
5. the address: the proxy resolves the name itself, and **every** answer
   must pass `policy.classify` (`resolve.checked_address`, strict) — one
   metadata, loopback, private, link-local, reserved or shared answer
   refuses the host;
6. the connection: to exactly the checked address, and its peer must be
   that address (**checked IP = connected IP**, 10 §5) — a connection that
   lands anywhere else is closed and refused.

Then `200 Connection Established`, and bytes are relayed and counted —
**no TLS interception** (OD-AF-13): what is inside the tunnel, methods and
forms included, is not inspected; the hosts it can reach are. The tunnel
ends on close, on the run's byte budget, on `idle_timeout`, or when the
proxy stops. Every decision is reported to `on_decision` (the audit trail);
a refusal carries a reason code and never echoes what the runtime sent.

Like the rest of `server/net`, the proxy authorizes nothing: the run's
hosts were decided before it started.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from server.net import policy
from server.net.destinations import hostname_allowed
from server.net.listen import internal_listen_socket, remove_internal_socket
from server.net.resolve import Resolver, checked_address, system_resolver

MAX_HEAD_BYTES = 8192
ALLOWED_PORTS = frozenset({443})
_HOST_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
_HOST = re.compile(rf"^(?:{_HOST_LABEL}\.)+{_HOST_LABEL}$")
_REQUEST_LINE = re.compile(rb"^CONNECT ([A-Za-z0-9.\-]{1,254}):([1-9][0-9]{0,4}) HTTP/1\.1$")

Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter, str]]]


def _normal_host(host: str) -> str:
    return host.strip().lower().removesuffix(".")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        # The URL standard's rule: a name whose last label is a number
        # (decimal, octal or `0x` hex) is an IPv4 address in another
        # spelling — `2130706433`, `0x7f.0.0.1`, `127.1` — never a host name.
        last = host.rstrip(".").rsplit(".", 1)[-1]
        return bool(re.fullmatch(r"0x[0-9a-f]*|[0-9]+", last))
    return True


@dataclass(frozen=True)
class ProxyPolicy:
    """One run's egress, fixed when the run starts. `hosts`: exact,
    non-empty host names (validated here; no wildcard, no IP literal)."""

    hosts: frozenset[str]
    ports: frozenset[int] = ALLOWED_PORTS
    allow_private_net: bool = False
    max_connections: int = 256
    max_concurrent: int = 16
    max_bytes: int = 256 * 1024 * 1024
    connect_timeout: float = 10.0
    header_timeout: float = 10.0
    idle_timeout: float = 60.0

    def __post_init__(self) -> None:
        if not self.hosts:
            raise ValueError("a run's egress needs at least one host")
        normal = set()
        for host in self.hosts:
            name = _normal_host(host)
            if not _HOST.fullmatch(name) or _is_ip_literal(name):
                raise ValueError(f"not an exact host name: {host!r}")
            normal.add(name)
        object.__setattr__(self, "hosts", frozenset(normal))
        if not self.ports or not self.ports <= ALLOWED_PORTS:
            raise ValueError("the egress proxy allows port 443 only")
        if min(self.max_connections, self.max_concurrent, self.max_bytes) < 1:
            raise ValueError("limits must be positive")


def policy_for_run(hosts, *, operator_destinations, operator_internet: bool, **limits) -> ProxyPolicy:
    """A run's egress, from deterministic JARVIS facts only: the exact hosts
    its spec names (OD-AF-15's `browser.session` scope), each of which must
    also be allowed by the operator's `EgressPolicy` (10 §2) — listed in its
    destinations, or the operator allows the internet. One host outside it
    refuses the whole run (`ValueError`): a run never gets a narrower policy
    than its spec claims, silently. Private networks are never reachable."""

    requested = [str(h) for h in hosts]
    if not requested:
        raise ValueError("a run's egress needs at least one host")
    built = ProxyPolicy(hosts=frozenset(requested), allow_private_net=False, **limits)
    for host in built.hosts:
        if not (operator_internet or hostname_allowed(host, operator_destinations)):
            raise ValueError(f"host {host!r} is outside the operator's egress policy")
    return built


@dataclass(frozen=True)
class ProxyDecision:
    host: str | None
    port: int | None
    allowed: bool
    reason: str
    ip: str | None = None


@dataclass
class ProxyStats:
    connections: int = 0
    active: int = 0
    refused: int = 0
    bytes_up: int = 0
    bytes_down: int = 0
    reasons: dict[str, int] = field(default_factory=dict)


class _Refused(Exception):
    def __init__(self, status: int, reason: str, host: str | None = None, port: int | None = None) -> None:
        super().__init__(reason)
        self.status, self.reason, self.host, self.port = status, reason, host, port


_STATUS_TEXT = {400: "Bad Request", 403: "Forbidden", 405: "Method Not Allowed", 408: "Request Timeout",
                429: "Too Many Requests", 502: "Bad Gateway"}


async def open_pinned(ip: str, port: int, *, timeout: float) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
    """Connect to exactly `ip` (no name is resolved here) and report the
    peer the socket actually reached."""

    reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    peer = writer.get_extra_info("peername")
    return reader, writer, str(peer[0]) if peer else ""


def _same_address(a: str, b: str) -> bool:
    try:
        left, right = ipaddress.ip_address(a), ipaddress.ip_address(b)
    except ValueError:
        return False
    left = getattr(left, "ipv4_mapped", None) or left
    right = getattr(right, "ipv4_mapped", None) or right
    return left == right


class EgressProxy:
    def __init__(self, *, binding: str, policy: ProxyPolicy, resolve: Resolver = system_resolver,
                 connect: Connector | None = None,
                 on_decision: Callable[[ProxyDecision], None] | None = None) -> None:
        if not binding.startswith("unix:"):
            raise ValueError("the egress proxy listens on a per-run Unix socket only")
        self.binding = binding
        self.policy = policy
        self._resolve = resolve
        self._connect = connect or (lambda ip, port: open_pinned(ip, port, timeout=policy.connect_timeout))
        self._on_decision = on_decision
        self.stats = ProxyStats()
        self._server: asyncio.base_events.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        sock = internal_listen_socket(self.binding)
        self._server = await asyncio.start_unix_server(self._handle, sock=sock)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for writer in list(self._writers):
            with contextlib.suppress(Exception):
                writer.close()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 5)
        remove_internal_socket(self.binding)
        self._server = None

    # ── one connection ───────────────────────────────────────────────────

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._writers.add(writer)
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            try:
                host, port, leftover = await self._read_head(reader)
                ip, up_reader, upstream_writer = await self._admit(host, port)
            except _Refused as refused:
                self._decide(ProxyDecision(refused.host, refused.port, False, refused.reason))
                await self._refuse(writer, refused.status)
                return
            self._decide(ProxyDecision(host, port, True, "allowed", ip))
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            self.stats.active += 1
            try:
                await self._relay(reader, writer, up_reader, upstream_writer, leftover)
            finally:
                self.stats.active -= 1
        except (ConnectionError, asyncio.CancelledError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            for w in (upstream_writer, writer):
                if w is not None:
                    with contextlib.suppress(Exception):
                        w.close()
            self._writers.discard(writer)
            if task is not None:
                self._tasks.discard(task)

    async def _read_head(self, reader: asyncio.StreamReader) -> tuple[str, int, bytes]:
        buffer = b""
        try:
            async with asyncio.timeout(self.policy.header_timeout):
                while b"\r\n\r\n" not in buffer:
                    if len(buffer) >= MAX_HEAD_BYTES:
                        raise _Refused(400, "request_too_large")
                    chunk = await reader.read(MAX_HEAD_BYTES - len(buffer))
                    if not chunk:
                        raise _Refused(400, "incomplete_request")
                    buffer += chunk
        except TimeoutError:
            raise _Refused(408, "request_timeout") from None
        head, _, leftover = buffer.partition(b"\r\n\r\n")
        request_line = head.split(b"\r\n", 1)[0]
        if not request_line.startswith(b"CONNECT "):
            raise _Refused(405, "not_connect")
        match = _REQUEST_LINE.fullmatch(request_line)
        if match is None:
            raise _Refused(400, "malformed_connect")
        return match.group(1).decode("ascii"), int(match.group(2)), leftover

    async def _admit(self, host: str, port: int):
        name = _normal_host(host)
        if _is_ip_literal(name) or not _HOST.fullmatch(name):
            raise _Refused(403, "not_a_host_name", None, port)
        if self.stats.connections >= self.policy.max_connections or self.stats.active >= self.policy.max_concurrent:
            raise _Refused(429, "connection_limit", name, port)
        if not hostname_allowed(name, self.policy.hosts):
            raise _Refused(403, "host_not_allowed", name, port)
        if port not in self.policy.ports:
            raise _Refused(403, "port_not_allowed", name, port)
        try:
            ip = await asyncio.wait_for(asyncio.to_thread(
                checked_address, name, port, allow_private_net=self.policy.allow_private_net,
                resolve=self._resolve, strict=True), self.policy.connect_timeout)
        except policy.DestinationBlocked as exc:
            raise _Refused(502 if exc.reason == "unresolved" else 403,
                           "unresolved" if exc.reason == "unresolved" else "destination_blocked", name, port) from None
        except (OSError, TimeoutError, ValueError):
            raise _Refused(502, "unresolved", name, port) from None
        self.stats.connections += 1
        try:
            up_reader, up_writer, peer = await asyncio.wait_for(self._connect(ip, port), self.policy.connect_timeout)
        except (OSError, TimeoutError):
            raise _Refused(502, "connect_failed", name, port) from None
        if not _same_address(peer, ip):
            # 10 §5: the connection must reach exactly the address checked.
            up_writer.close()
            raise _Refused(502, "ip_mismatch", name, port)
        return ip, up_reader, up_writer

    async def _relay(self, down_reader, down_writer, up_reader, up_writer, leftover: bytes) -> None:
        budget = self.policy

        def room() -> int:
            return budget.max_bytes - (self.stats.bytes_up + self.stats.bytes_down)

        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter, up: bool, first: bytes = b"") -> None:
            data = first
            while True:
                if data:
                    allowed = room()
                    if allowed <= 0:
                        return
                    data = data[:allowed]
                    dst.write(data)
                    await dst.drain()
                    if up:
                        self.stats.bytes_up += len(data)
                    else:
                        self.stats.bytes_down += len(data)
                    if room() <= 0:
                        return
                try:
                    data = await asyncio.wait_for(src.read(65536), budget.idle_timeout)
                except TimeoutError:
                    return
                if not data:
                    return

        tasks = [asyncio.ensure_future(pump(down_reader, up_writer, True, leftover)),
                 asyncio.ensure_future(pump(up_reader, down_writer, False))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _refuse(self, writer: asyncio.StreamWriter, status: int) -> None:
        self.stats.refused += 1
        with contextlib.suppress(Exception):
            writer.write(f"HTTP/1.1 {status} {_STATUS_TEXT.get(status, 'Refused')}\r\n"
                         "Connection: close\r\nContent-Length: 0\r\n\r\n".encode("ascii"))
            await writer.drain()

    def _decide(self, decision: ProxyDecision) -> None:
        self.stats.reasons[decision.reason] = self.stats.reasons.get(decision.reason, 0) + 1
        if self._on_decision is not None:
            with contextlib.suppress(Exception):
                self._on_decision(decision)


__all__ = ["ALLOWED_PORTS", "EgressProxy", "ProxyDecision", "ProxyPolicy", "ProxyStats", "open_pinned",
           "policy_for_run"]
