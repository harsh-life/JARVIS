"""server/net — 10_NETWORK_EGRESS.md's acceptance hooks (NET-T1..NET-T10).

Two kinds of test here, deliberately kept apart:

* **Policy/blocking tests** call the real `EgressClient`/`classify` with no
  patching at all. Every one of these — loopback, private ranges, metadata,
  denied-by-default — is refused *before* any socket connects (the
  destination/IP check runs first), so a real listening server is never
  needed to prove a block.
* **Plumbing tests** (a request actually succeeding, following a redirect,
  reading a chunked/oversized/truncated body, a timeout) need a real
  connection to prove the read/redirect/cap machinery genuinely works. This
  sandboxed environment's only bindable local address is loopback — which
  `classify` correctly refuses unconditionally (10 §4) — so these tests
  patch `server.net.client.policy.classify` to allow the test server's own
  loopback address while everything else about the request goes through the
  real client unmodified. The classifier itself is never patched in a test
  that is trying to prove the classifier's behavior.
"""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager

import pytest

from server.net import DestinationBlocked, classify, hostname_allowed
from server.net.client import EgressClient
from shared.schemas.execution import EgressPolicy, ExecutionError, ExecutionErrorCode


def make_policy(**overrides) -> EgressPolicy:
    defaults = dict(
        destinations=frozenset(),
        internet=False,
        private_net=False,
        may_send_credentials=False,
        allowed_ports=frozenset({80, 443}),
        max_response_bytes=5_000_000,
        connect_timeout_seconds=2.0,
        read_timeout_seconds=2.0,
        max_redirects=3,
    )
    defaults.update(overrides)
    return EgressPolicy(**defaults)


@pytest.fixture
def client() -> EgressClient:
    return EgressClient()


# ── NET-T4/T5/T6: destination classification (no socket touched) ────────


def test_loopback_ipv4_is_blocked():
    with pytest.raises(DestinationBlocked):
        classify("127.0.0.1", allow_private_net=True)


def test_loopback_ipv6_is_blocked():
    with pytest.raises(DestinationBlocked):
        classify("::1", allow_private_net=True)


@pytest.mark.parametrize("private_ip", ["10.0.0.5", "172.16.0.5", "192.168.1.5"])
def test_private_ranges_blocked_by_default_allowed_when_declared(private_ip):
    with pytest.raises(DestinationBlocked):
        classify(private_ip, allow_private_net=False)
    classify(private_ip, allow_private_net=True)  # must not raise


def test_metadata_ipv4_is_never_allowed_even_with_private_net():
    with pytest.raises(DestinationBlocked) as excinfo:
        classify("169.254.169.254", allow_private_net=True)
    assert "metadata" in excinfo.value.reason


def test_metadata_ipv6_form_is_never_allowed():
    with pytest.raises(DestinationBlocked):
        classify("fd00:ec2::254", allow_private_net=True)


def test_ipv4_mapped_ipv6_metadata_address_is_still_caught():
    """Alternate IP representation (§4 of the brief): the IPv4-mapped IPv6
    spelling of the metadata address must not slip past classification just
    because it looks like a different address family."""

    with pytest.raises(DestinationBlocked) as excinfo:
        classify("::ffff:169.254.169.254", allow_private_net=True)
    assert "metadata" in excinfo.value.reason


def test_link_local_is_blocked():
    with pytest.raises(DestinationBlocked):
        classify("169.254.1.1", allow_private_net=True)
    with pytest.raises(DestinationBlocked):
        classify("fe80::1", allow_private_net=True)


def test_public_address_is_allowed():
    classify("8.8.8.8", allow_private_net=False)  # must not raise


# ── NET-T1: no declared network = no egress at all ───────────────────────


async def test_tool_with_no_declared_network_gets_zero_egress(client):
    empty_policy = make_policy(destinations=frozenset(), internet=False)
    with pytest.raises(ExecutionError) as excinfo:
        await client.arequest("http://example.com/", egress_policy=empty_policy)
    assert excinfo.value.code == ExecutionErrorCode.EGRESS_DENIED


# ── NET-T4/T5 via the full client (still no server needed — blocked pre-connect) ──


async def test_full_client_refuses_loopback_before_connecting(client):
    egress_policy = make_policy(destinations=frozenset({"127.0.0.1"}), private_net=True)
    with pytest.raises(ExecutionError) as excinfo:
        await client.arequest("http://127.0.0.1:1/", egress_policy=egress_policy)
    assert excinfo.value.code == ExecutionErrorCode.EGRESS_DENIED


async def test_full_client_refuses_undeclared_destination(client):
    egress_policy = make_policy(destinations=frozenset({"api.example.com"}))
    with pytest.raises(ExecutionError) as excinfo:
        await client.arequest("http://evil.example.org/", egress_policy=egress_policy)
    assert excinfo.value.code == ExecutionErrorCode.EGRESS_DENIED


async def test_full_client_refuses_disallowed_port(client):
    egress_policy = make_policy(destinations=frozenset({"example.com"}), allowed_ports=frozenset({443}))
    with pytest.raises(ExecutionError):
        await client.arequest("http://example.com:8080/", egress_policy=egress_policy)


async def test_full_client_refuses_non_http_scheme(client):
    egress_policy = make_policy(internet=True)
    with pytest.raises(ExecutionError):
        await client.arequest("file:///etc/passwd", egress_policy=egress_policy)


async def test_unresolvable_hostname_is_a_deterministic_failure(client):
    egress_policy = make_policy(internet=True)
    with pytest.raises(ExecutionError) as excinfo:
        await client.arequest("http://this-host-does-not-exist.invalid/", egress_policy=egress_policy)
    assert excinfo.value.code == ExecutionErrorCode.DESTINATION_UNRESOLVED


# ── hostname allow-list matching ─────────────────────────────────────────


def test_hostname_allowed_exact_match():
    assert hostname_allowed("api.example.com", {"api.example.com"})
    assert not hostname_allowed("evil.com", {"api.example.com"})


def test_hostname_allowed_subdomain_wildcard():
    assert hostname_allowed("foo.example.com", {".example.com"})
    assert not hostname_allowed("example.com", {".example.com"})
    assert not hostname_allowed("notexample.com", {".example.com"})


# ── plumbing: a real connection, with classify patched for loopback ──────


@contextmanager
def _raw_server(handler):
    """A minimal TCP server this test fully controls the response bytes
    for — needed to exercise chunked encoding / oversized-response /
    truncation precisely, which no public test server can be relied on to
    reproduce deterministically."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    port = sock.getsockname()[1]
    stop = threading.Event()

    def serve():
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            try:
                conn.settimeout(5.0)
                request = conn.recv(65536)
                handler(conn, request)
            except OSError:
                pass
            finally:
                conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        stop.set()
        thread.join(timeout=2.0)
        sock.close()


@pytest.fixture
def allow_loopback(monkeypatch):
    """Patch only the classifier this one test suite needs bypassed to
    reach its own local test server — see module docstring."""

    import server.net.client as client_module

    def _permissive_classify(address_text, *, allow_private_net):
        return None

    monkeypatch.setattr(client_module.policy, "classify", _permissive_classify)


async def test_successful_request_reads_body_and_status(client, allow_loopback):
    def handler(conn, request):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello")

    with _raw_server(handler) as port:
        egress_policy = make_policy(destinations=frozenset({"127.0.0.1"}), allowed_ports=frozenset({port}))
        result = await client.arequest(f"http://127.0.0.1:{port}/", egress_policy=egress_policy)
        assert result.content == "hello"
        assert result.metadata["status"] == 200


async def test_chunked_response_is_decoded(client, allow_loopback):
    def handler(conn, request):
        body = b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
        conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + body)

    with _raw_server(handler) as port:
        egress_policy = make_policy(destinations=frozenset({"127.0.0.1"}), allowed_ports=frozenset({port}))
        result = await client.arequest(f"http://127.0.0.1:{port}/", egress_policy=egress_policy)
        assert result.content == "hello world"


async def test_redirect_is_followed_and_revalidated(client, allow_loopback):
    hits = []

    def handler(conn, request):
        hits.append(request.split(b" ")[1])
        if request.startswith(b"GET /start"):
            conn.sendall(b"HTTP/1.1 302 Found\r\nLocation: /final\r\nContent-Length: 0\r\n\r\n")
        else:
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\nredirect!")

    with _raw_server(handler) as port:
        egress_policy = make_policy(destinations=frozenset({"127.0.0.1"}), allowed_ports=frozenset({port}))
        result = await client.arequest(f"http://127.0.0.1:{port}/start", egress_policy=egress_policy)
        assert result.content == "redirect!"
        assert hits == [b"/start", b"/final"]


async def test_redirect_loop_is_capped_by_max_redirects(client, allow_loopback):
    def handler(conn, request):
        conn.sendall(b"HTTP/1.1 302 Found\r\nLocation: /loop\r\nContent-Length: 0\r\n\r\n")

    with _raw_server(handler) as port:
        egress_policy = make_policy(
            destinations=frozenset({"127.0.0.1"}), allowed_ports=frozenset({port}), max_redirects=2,
        )
        with pytest.raises(ExecutionError) as excinfo:
            await client.arequest(f"http://127.0.0.1:{port}/loop", egress_policy=egress_policy)
        assert excinfo.value.code == ExecutionErrorCode.EGRESS_DENIED


async def test_oversized_response_is_rejected_not_buffered_unbounded(client, allow_loopback):
    def handler(conn, request):
        big = b"x" * 200_000
        conn.sendall(f"HTTP/1.1 200 OK\r\nContent-Length: {len(big)}\r\n\r\n".encode() + big)

    with _raw_server(handler) as port:
        egress_policy = make_policy(
            destinations=frozenset({"127.0.0.1"}), allowed_ports=frozenset({port}), max_response_bytes=1000,
        )
        with pytest.raises(ExecutionError) as excinfo:
            await client.arequest(f"http://127.0.0.1:{port}/", egress_policy=egress_policy)
        assert excinfo.value.code == ExecutionErrorCode.RESPONSE_TOO_LARGE


async def test_oversized_chunked_response_is_also_capped(client, allow_loopback):
    def handler(conn, request):
        chunk = b"a" * 50_000
        size_hex = format(len(chunk), "x").encode()
        body = size_hex + b"\r\n" + chunk + b"\r\n" + size_hex + b"\r\n" + chunk + b"\r\n0\r\n\r\n"
        conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + body)

    with _raw_server(handler) as port:
        egress_policy = make_policy(
            destinations=frozenset({"127.0.0.1"}), allowed_ports=frozenset({port}), max_response_bytes=1000,
        )
        with pytest.raises(ExecutionError) as excinfo:
            await client.arequest(f"http://127.0.0.1:{port}/", egress_policy=egress_policy)
        assert excinfo.value.code == ExecutionErrorCode.RESPONSE_TOO_LARGE


async def test_slow_server_triggers_read_timeout(client, allow_loopback):
    def handler(conn, request):
        time.sleep(2.0)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nhi")

    with _raw_server(handler) as port:
        egress_policy = make_policy(
            destinations=frozenset({"127.0.0.1"}), allowed_ports=frozenset({port}), read_timeout_seconds=0.3,
        )
        with pytest.raises(ExecutionError) as excinfo:
            await client.arequest(f"http://127.0.0.1:{port}/", egress_policy=egress_policy)
        assert excinfo.value.code == ExecutionErrorCode.TIMEOUT


async def test_connection_refused_is_egress_denied_not_a_crash(client, allow_loopback):
    egress_policy = make_policy(destinations=frozenset({"127.0.0.1"}), allowed_ports=frozenset({1}))
    with pytest.raises(ExecutionError) as excinfo:
        await client.arequest("http://127.0.0.1:1/", egress_policy=egress_policy)
    assert excinfo.value.code == ExecutionErrorCode.EGRESS_DENIED


# ── construction-time honesty (mirrors FilesystemSandbox's containment_mode) ──


def test_unimplemented_enforcement_mode_refused_at_construction():
    with pytest.raises(ExecutionError):
        EgressClient(enforcement_mode="netns_filtered")


# ── integration-hardening regressions ───────────────────────────────────


@pytest.mark.parametrize("cgnat_ip", ["100.64.0.1", "100.100.100.100", "100.127.255.254"])
def test_shared_address_space_is_not_the_internet(cgnat_ip):
    """RFC 6598 (100.64.0.0/10) is carrier/overlay space, not the public
    internet: `internet=True` alone must not reach it, `private_net` may."""

    with pytest.raises(DestinationBlocked):
        classify(cgnat_ip, allow_private_net=False)
    classify(cgnat_ip, allow_private_net=True)


async def test_a_huge_declared_chunk_is_refused_before_it_is_buffered(client, allow_loopback):
    """A chunk header claiming far more than the cap must fail on the header,
    not after the client has buffered toward it."""

    def handler(conn, request):
        conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nFFFFFFFFFF\r\n")
        conn.sendall(b"a" * 4096)  # never enough to complete the chunk

    with _raw_server(handler) as port:
        egress_policy = make_policy(
            destinations=frozenset({"127.0.0.1"}), allowed_ports=frozenset({port}), max_response_bytes=1000,
        )
        with pytest.raises(ExecutionError) as excinfo:
            await client.arequest(f"http://127.0.0.1:{port}/", egress_policy=egress_policy)
        assert excinfo.value.code == ExecutionErrorCode.RESPONSE_TOO_LARGE


async def test_a_negative_chunk_size_is_malformed(client, allow_loopback):
    def handler(conn, request):
        conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n-5\r\nhello\r\n0\r\n\r\n")

    with _raw_server(handler) as port:
        egress_policy = make_policy(destinations=frozenset({"127.0.0.1"}), allowed_ports=frozenset({port}))
        with pytest.raises(ExecutionError) as excinfo:
            await client.arequest(f"http://127.0.0.1:{port}/", egress_policy=egress_policy)
        assert excinfo.value.code == ExecutionErrorCode.EGRESS_DENIED


async def test_a_slow_drip_cannot_outlast_the_total_request_budget(client, allow_loopback):
    """Each byte arrives inside the per-read timeout, so only a total deadline
    stops this; without one the worker thread is held indefinitely."""

    def handler(conn, request):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n")
        for _ in range(100):
            time.sleep(0.1)
            try:
                conn.sendall(b"x")
            except OSError:
                return

    with _raw_server(handler) as port:
        egress_policy = make_policy(
            destinations=frozenset({"127.0.0.1"}), allowed_ports=frozenset({port}),
            connect_timeout_seconds=0.3, read_timeout_seconds=0.5,
        )
        started = time.monotonic()
        with pytest.raises(ExecutionError) as excinfo:
            await client.arequest(f"http://127.0.0.1:{port}/", egress_policy=egress_policy)
        assert excinfo.value.code == ExecutionErrorCode.TIMEOUT
        assert time.monotonic() - started < 3.0
