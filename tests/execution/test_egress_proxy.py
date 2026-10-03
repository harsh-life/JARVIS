"""The egress boundary of an external agent runtime — Phase 6 slice 6B.

OD-AF-12/13 (ratified 2026-10-02, register §2L): the Browser Use container
has no network interface. Its only way out is a per-run Unix socket to
JARVIS's own CONNECT proxy (`server/net/egress_proxy.py`), which is
**authoritative** for where it may go:

* one `CONNECT host:443` per connection — nothing else is spoken;
* the host must be exactly one of the run's hosts (no wildcard, no IP
  literal) — the run's list is JARVIS's (the spec ∩ the operator's policy),
  never the runtime's;
* the proxy resolves the host itself and classifies **every** resolved
  address with `server/net/policy.py` (metadata, loopback, private,
  link-local, reserved, ... refused); one bad address refuses the host;
* it connects to exactly the address it checked, and refuses a connection
  whose peer is any other (**checked IP = connected IP**, 10 §5);
* no TLS interception: after `200`, bytes are relayed and counted, nothing
  more; per-run connection, concurrency, byte and idle limits; default deny.

The runtime on the other side is untrusted: these tests speak to the proxy
the way a compromised one would.
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from server.net.egress_proxy import EgressProxy, ProxyPolicy, open_pinned
from server.net.resolve import checked_address
from server.net.policy import DestinationBlocked

PUBLIC = "93.184.216.34"
PUBLIC_2 = "93.184.216.35"
HOST = "advisories.example.org"


class Upstream:
    """A stand-in for the internet: whatever the proxy connects to lands on a
    local echo server, and the address the proxy *asked* for is recorded."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, int]] = []
        self.server: asyncio.base_events.Server | None = None
        self.port = 0
        self.peer_override: str | None = None

    async def start(self) -> None:
        async def echo(reader, writer):
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()

        self.server = await asyncio.start_server(echo, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def connect(self, ip: str, port: int):
        self.asked.append((ip, port))
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        # An honest fake reports the peer it was asked for; a test that wants
        # the connection to land elsewhere sets `peer_override`.
        return reader, writer, self.peer_override or ip

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


class Resolver:
    def __init__(self, *answers: list[str]) -> None:
        self.answers = list(answers)
        self.calls: list[str] = []

    def __call__(self, host: str, port: int) -> list[str]:
        self.calls.append(host)
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


@pytest.fixture
async def upstream():
    up = Upstream()
    await up.start()
    yield up
    await up.stop()


@pytest.fixture
def sock_path(tmp_path) -> Path:
    return tmp_path / "egress.sock"


async def _proxy(sock_path, upstream, *, resolver=None, decisions=None, **policy) -> EgressProxy:
    proxy = EgressProxy(
        binding=f"unix:{sock_path}",
        policy=ProxyPolicy(hosts=frozenset(policy.pop("hosts", {HOST})), **policy),
        resolve=resolver or Resolver([PUBLIC]),
        connect=upstream.connect,
        on_decision=(decisions.append if decisions is not None else None),
    )
    await proxy.start()
    return proxy


async def _send(sock_path: Path, head: bytes, *, then: bytes = b"", read: int = 4096, timeout: float = 5.0):
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    writer.write(head)
    await writer.drain()
    status = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
    echoed = b""
    if then and status.startswith(b"HTTP/1.1 200"):
        writer.write(then)
        await writer.drain()
        echoed = await asyncio.wait_for(reader.readexactly(len(then)), timeout)
    writer.close()
    return status, echoed


def _connect(host: str = HOST, port: int = 443) -> bytes:
    return f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode()


def _code(status: bytes) -> int:
    return int(status.split(b" ", 2)[1])


# ── the shared, reused resolution rule (server/net/resolve.py) ──────────────


def test_checked_address_classifies_every_candidate_when_strict() -> None:
    assert checked_address(HOST, 443, allow_private_net=False, resolve=lambda h, p: [PUBLIC], strict=True) == PUBLIC
    for bad in (["10.0.0.5"], ["169.254.169.254"], ["127.0.0.1"], [PUBLIC, "10.0.0.5"], ["::1"],
                ["::ffff:169.254.169.254"], ["100.64.0.1"], ["0.0.0.0"], []):
        with pytest.raises(DestinationBlocked):
            checked_address(HOST, 443, allow_private_net=False, resolve=lambda h, p, a=bad: a, strict=True)


def test_checked_address_lenient_mode_keeps_the_clients_first_passing_rule() -> None:
    """The existing egress client's rule (its own tests are unchanged): the
    first candidate that passes; never a blocked one."""

    assert checked_address(HOST, 443, allow_private_net=False,
                           resolve=lambda h, p: ["10.0.0.5", PUBLIC], strict=False) == PUBLIC
    with pytest.raises(DestinationBlocked):
        checked_address(HOST, 443, allow_private_net=False, resolve=lambda h, p: ["10.0.0.5"], strict=False)


def test_a_policy_names_exact_hosts_only() -> None:
    ProxyPolicy(hosts=frozenset({HOST, "status.example.org"}))
    for bad in ({".example.org"}, {"*.example.org"}, {"93.184.216.34"}, {"[::1]"}, {""}, {"exa mple.org"},
                {"0x7f.0.0.1"}, {"127.1"}, {"2130706433"}, {"host.0x10"},
                {"example.org:443"}, {"user@example.org"}, set()):
        with pytest.raises(ValueError):
            ProxyPolicy(hosts=frozenset(bad))
    with pytest.raises(ValueError):
        ProxyPolicy(hosts=frozenset({HOST}), ports=frozenset({443, 22}))


# ── the allowed path ────────────────────────────────────────────────────────


async def test_an_allowed_host_is_tunnelled_to_exactly_the_checked_address(sock_path, upstream) -> None:
    decisions: list = []
    proxy = await _proxy(sock_path, upstream, decisions=decisions)
    try:
        status, echoed = await _send(sock_path, _connect(), then=b"\x16\x03\x01 client hello")
    finally:
        await proxy.stop()
    assert _code(status) == 200
    assert echoed == b"\x16\x03\x01 client hello"     # bytes relayed as they are: no TLS interception
    assert upstream.asked == [(PUBLIC, 443)]
    [decision] = decisions
    assert (decision.host, decision.allowed, decision.ip) == (HOST, True, PUBLIC)
    assert proxy.stats.bytes_up >= len(b"\x16\x03\x01 client hello")


async def test_the_socket_is_its_owners_alone_and_removed_on_stop(sock_path, upstream) -> None:
    proxy = await _proxy(sock_path, upstream)
    assert stat.S_IMODE(os.lstat(sock_path).st_mode) == 0o600
    await proxy.stop()
    assert not sock_path.exists()


async def test_host_matching_is_case_and_trailing_dot_insensitive(sock_path, upstream) -> None:
    proxy = await _proxy(sock_path, upstream)
    try:
        for host in ("ADVISORIES.example.org", "advisories.example.org."):
            status, _ = await _send(sock_path, _connect(host))
            assert _code(status) == 200, host
    finally:
        await proxy.stop()


# ── default deny ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("host", [
    "evil.example.com", "example.org", "status.example.org", "advisories.example.org.evil.com",
    "xadvisories.example.org", "metadata.google.internal",
])
async def test_a_host_not_in_the_runs_list_is_refused(sock_path, upstream, host) -> None:
    decisions: list = []
    resolver = Resolver([PUBLIC])
    proxy = await _proxy(sock_path, upstream, resolver=resolver, decisions=decisions)
    try:
        status, _ = await _send(sock_path, _connect(host))
    finally:
        await proxy.stop()
    assert _code(status) == 403
    assert upstream.asked == [] and resolver.calls == []     # never even resolved
    assert decisions[-1].allowed is False and decisions[-1].reason == "host_not_allowed"


@pytest.mark.parametrize("target", [
    "93.184.216.34:443", "127.0.0.1:443", "[::1]:443", "169.254.169.254:443", "[::ffff:169.254.169.254]:443",
    "0x7f.0.0.1:443", "2130706433:443", "10.0.0.1:443", "127.1:443", "0177.0.0.1:443", "a.b.0x7f000001:443",
])
async def test_an_ip_literal_is_refused_even_a_public_one(sock_path, upstream, target) -> None:
    decisions: list = []
    proxy = await _proxy(sock_path, upstream, decisions=decisions)
    try:
        status, _ = await _send(sock_path, f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
    finally:
        await proxy.stop()
    assert _code(status) in (400, 403)
    assert upstream.asked == []
    # Refused as an address, before any host-list lookup could matter.
    assert decisions[-1].reason in ("not_a_host_name", "malformed_connect")


@pytest.mark.parametrize("port", [80, 22, 8443, 65535, 4433])
async def test_only_port_443(sock_path, upstream, port) -> None:
    proxy = await _proxy(sock_path, upstream)
    try:
        status, _ = await _send(sock_path, _connect(port=port))
    finally:
        await proxy.stop()
    assert _code(status) == 403
    assert upstream.asked == []


@pytest.mark.parametrize("answers,reason", [
    (["10.0.0.5"], "destination_blocked"),
    (["169.254.169.254"], "destination_blocked"),
    (["127.0.0.1"], "destination_blocked"),
    (["::ffff:127.0.0.1"], "destination_blocked"),
    ([PUBLIC, "192.168.1.1"], "destination_blocked"),
    (["100.64.1.1"], "destination_blocked"),
    ([], "unresolved"),
])
async def test_an_allowed_host_resolving_anywhere_unsafe_is_refused(sock_path, upstream, answers, reason) -> None:
    decisions: list = []
    proxy = await _proxy(sock_path, upstream, resolver=Resolver(answers), decisions=decisions)
    try:
        status, _ = await _send(sock_path, _connect())
    finally:
        await proxy.stop()
    assert _code(status) in (403, 502)
    assert upstream.asked == []
    assert decisions[-1].reason == reason


async def test_resolution_failure_is_refused(sock_path, upstream) -> None:
    def broken(host, port):
        raise OSError("no such host")

    proxy = await _proxy(sock_path, upstream, resolver=broken)
    try:
        status, _ = await _send(sock_path, _connect())
    finally:
        await proxy.stop()
    assert _code(status) == 502 and upstream.asked == []


# ── DNS rebinding and checked IP = connected IP ─────────────────────────────


async def test_dns_rebinding_cannot_move_the_connection(sock_path, upstream) -> None:
    """The name resolves to a public address when checked and to the metadata
    endpoint on every later lookup: the proxy resolves once and connects to
    the address it checked, so the rebound answer is never used."""

    resolver = Resolver([PUBLIC], ["169.254.169.254"], ["169.254.169.254"])
    proxy = await _proxy(sock_path, upstream, resolver=resolver)
    try:
        status, _ = await _send(sock_path, _connect())
    finally:
        await proxy.stop()
    assert _code(status) == 200
    assert resolver.calls == [HOST]
    assert upstream.asked == [(PUBLIC, 443)]


async def test_each_connect_is_resolved_and_checked_afresh(sock_path, upstream) -> None:
    resolver = Resolver([PUBLIC], ["10.0.0.9"])
    proxy = await _proxy(sock_path, upstream, resolver=resolver)
    try:
        first, _ = await _send(sock_path, _connect())
        second, _ = await _send(sock_path, _connect())
    finally:
        await proxy.stop()
    assert (_code(first), _code(second)) == (200, 403)
    assert upstream.asked == [(PUBLIC, 443)]


async def test_a_connection_that_lands_on_another_address_is_refused(sock_path, upstream) -> None:
    upstream.peer_override = PUBLIC_2
    decisions: list = []
    proxy = await _proxy(sock_path, upstream, decisions=decisions)
    try:
        status, _ = await _send(sock_path, _connect())
    finally:
        await proxy.stop()
    assert _code(status) == 502
    assert decisions[-1].reason == "ip_mismatch"


async def test_the_real_connector_reports_the_actual_peer(upstream) -> None:
    reader, writer, peer = await open_pinned("127.0.0.1", upstream.port, timeout=5)
    writer.close()
    assert peer == "127.0.0.1"


# ── a compromised runtime trying to escape the proxy path (AGENT-T12-style) ─


@pytest.mark.parametrize("head", [
    b"GET http://evil.example.com/ HTTP/1.1\r\nHost: evil.example.com\r\n\r\n",          # absolute-form proxying
    b"GET / HTTP/1.1\r\nHost: " + HOST.encode() + b"\r\n\r\n",                             # origin-form
    b"POST http://" + HOST.encode() + b"/ HTTP/1.1\r\nHost: x\r\n\r\n",
    b"connect " + HOST.encode() + b":443 HTTP/1.1\r\n\r\n",                                 # method case
    b"CONNECT " + HOST.encode() + b":443@evil.example.com:443 HTTP/1.1\r\n\r\n",          # userinfo smuggling
    b"CONNECT user@" + HOST.encode() + b":443 HTTP/1.1\r\n\r\n",
    b"CONNECT " + HOST.encode() + b"/x:443 HTTP/1.1\r\n\r\n",
    b"CONNECT " + HOST.encode() + b" :443 HTTP/1.1\r\n\r\n",
    b"CONNECT " + HOST.encode() + b":443 HTTP/2.0\r\n\r\n",
    b"CONNECT " + HOST.encode() + b"\r\n\r\n",                                            # no port
    b"CONNECT " + HOST.encode() + b":0443 HTTP/1.1\r\n\r\n",
    b"CONNECT " + HOST.encode() + b":443 HTTP/1.1 extra\r\n\r\n",
    b"CONNECT unix:/run/jarvis/model.sock HTTP/1.1\r\n\r\n",
    b"CONNECT localhost:443 HTTP/1.1\r\n\r\n",                                           # not a dotted host name
    b"CONNECT " + HOST.encode() + b":0 HTTP/1.1\r\n\r\n",                                 # port 0
    b"CONNECT " + HOST.encode() + b":99999 HTTP/1.1\r\n\r\n",
    b"CONNECT xn--80ak6aa92e.com:443 HTTP/1.1\r\n\r\n",
    b"CONNECT adv\xc3\xadsories.example.org:443 HTTP/1.1\r\n\r\n",
    b"\x16\x03\x01\x00\x05hello\r\n\r\n",                                                 # raw TLS, no CONNECT
])
async def test_anything_but_a_well_formed_connect_to_an_allowed_host_is_refused(sock_path, upstream, head) -> None:
    proxy = await _proxy(sock_path, upstream)
    try:
        status, _ = await _send(sock_path, head)
    finally:
        await proxy.stop()
    assert _code(status) in (400, 403, 405)
    assert upstream.asked == []


async def test_an_oversized_request_head_is_refused(sock_path, upstream) -> None:
    decisions: list = []
    proxy = await _proxy(sock_path, upstream, decisions=decisions)
    try:
        status, _ = await _send(sock_path, _connect()[:-2] + b"X-Pad: " + b"a" * 20_000 + b"\r\n\r\n")
    finally:
        await proxy.stop()
    assert _code(status) == 400 and upstream.asked == []
    assert decisions[-1].reason == "request_too_large"


async def test_a_slow_request_head_is_cut_off(sock_path, upstream) -> None:
    proxy = await _proxy(sock_path, upstream, header_timeout=0.3)
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        writer.write(b"CONNECT adv")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(4096), 5)
        writer.close()
    finally:
        await proxy.stop()
    assert data == b"" or _code(data) == 408
    assert upstream.asked == []


async def test_the_runs_connection_limit_holds(sock_path, upstream) -> None:
    proxy = await _proxy(sock_path, upstream, max_connections=2)
    try:
        codes = [_code((await _send(sock_path, _connect()))[0]) for _ in range(4)]
    finally:
        await proxy.stop()
    assert codes == [200, 200, 429, 429]
    assert len(upstream.asked) == 2


async def test_the_runs_byte_budget_ends_the_tunnel(sock_path, upstream) -> None:
    proxy = await _proxy(sock_path, upstream, max_bytes=1024)
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        writer.write(_connect())
        await writer.drain()
        assert _code(await reader.readuntil(b"\r\n\r\n")) == 200
        writer.write(b"x" * 4096)
        await writer.drain()
        received = b""
        with pytest.raises((asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError)):
            while True:
                chunk = await asyncio.wait_for(reader.read(65536), 5)
                if not chunk:
                    raise asyncio.IncompleteReadError(received, None)
                received += chunk
        writer.close()
    finally:
        await proxy.stop()
    assert len(received) <= 1024
    assert proxy.stats.bytes_up + proxy.stats.bytes_down <= 2048


async def test_an_idle_tunnel_is_closed(sock_path, upstream) -> None:
    proxy = await _proxy(sock_path, upstream, idle_timeout=0.3)
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        writer.write(_connect())
        await writer.drain()
        assert _code(await reader.readuntil(b"\r\n\r\n")) == 200
        assert await asyncio.wait_for(reader.read(10), 5) == b""
        writer.close()
    finally:
        await proxy.stop()


async def test_stop_closes_live_tunnels(sock_path, upstream) -> None:
    proxy = await _proxy(sock_path, upstream)
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    writer.write(_connect())
    await writer.drain()
    assert _code(await reader.readuntil(b"\r\n\r\n")) == 200
    await proxy.stop()
    assert await asyncio.wait_for(reader.read(10), 5) == b""
    writer.close()


async def test_refusals_do_not_echo_what_the_runtime_sent(sock_path, upstream) -> None:
    proxy = await _proxy(sock_path, upstream)
    try:
        status, _ = await _send(sock_path, _connect("secret-exfil-TOKEN123.evil.example.com"))
    finally:
        await proxy.stop()
    assert b"TOKEN123" not in status


# ── where a run's hosts come from ───────────────────────────────────────────


def test_a_runs_policy_is_the_specs_hosts_within_the_operators_policy() -> None:
    from server.net.egress_proxy import policy_for_run

    built = policy_for_run([HOST, "Status.Example.org"], operator_destinations=[HOST, ".example.org"],
                           operator_internet=False)
    assert built.hosts == frozenset({HOST, "status.example.org"})
    assert built.ports == frozenset({443}) and built.allow_private_net is False
    # With the operator's internet allowance, any exact host may be listed.
    assert policy_for_run([HOST], operator_destinations=[], operator_internet=True).hosts == {HOST}


@pytest.mark.parametrize("requested,destinations,internet", [
    ([HOST, "evil.example.com"], [HOST], False),   # one host outside the operator's policy refuses the run
    ([HOST], [], False),                           # the operator allows no egress at all
    ([], [HOST], True),                            # no hosts: nothing to browse
    ([".example.org"], [".example.org"], False),   # a wildcard is never a run's host
    (["93.184.216.34"], [], True),                 # nor an IP literal
])
def test_a_run_whose_hosts_are_not_all_permitted_gets_no_policy(requested, destinations, internet) -> None:
    from server.net.egress_proxy import policy_for_run

    with pytest.raises(ValueError):
        policy_for_run(requested, operator_destinations=destinations, operator_internet=internet)
