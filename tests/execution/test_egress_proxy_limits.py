"""The egress proxy's limits under concurrency (server/net/egress_proxy.py).

The proxy used to check its counters *before* awaiting DNS and only update
them *after*, and its relays checked the byte budget, wrote, awaited `drain()`
and only then counted. Concurrent connections could therefore all pass the
same check. These tests launch far more simultaneous CONNECTs and streams than
the limits allow and prove the limits hold — and that every reservation is
released however a connection ends.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from server.net.egress_proxy import EgressProxy, ProxyPolicy
from tests.execution.test_egress_proxy import HOST, PUBLIC, Upstream, _code, _connect

pytestmark = pytest.mark.asyncio


class GatedResolver:
    """Resolution blocks until released, so every concurrent CONNECT is
    sitting in DNS at the same time — the window the old accounting missed."""

    def __init__(self, answer=(PUBLIC,), *, fail: bool = False) -> None:
        self.release = threading.Event()
        self.answer, self.fail = list(answer), fail
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, host: str, port: int) -> list[str]:
        with self._lock:
            self.calls += 1
        self.release.wait(10)
        if self.fail:
            raise OSError("no such host")
        return self.answer


@pytest.fixture
async def upstream():
    up = Upstream()
    await up.start()
    yield up
    await up.stop()


async def _proxy(tmp_path, upstream, resolver, **policy) -> EgressProxy:
    proxy = EgressProxy(binding=f"unix:{tmp_path / 'egress.sock'}",
                        policy=ProxyPolicy(hosts=frozenset({HOST}), **policy),
                        resolve=resolver, connect=upstream.connect)
    await proxy.start()
    return proxy


async def _open(tmp_path):
    reader, writer = await asyncio.open_unix_connection(str(tmp_path / "egress.sock"))
    writer.write(_connect())
    await writer.drain()
    return reader, writer


async def _status(reader) -> int:
    return _code(await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15))


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


# ── connection admission ──────────────────────────────────────────────────


async def test_concurrent_connects_cannot_exceed_the_concurrent_limit(tmp_path, upstream):
    resolver = GatedResolver()
    proxy = await _proxy(tmp_path, upstream, resolver, max_concurrent=5, max_connections=1000)
    try:
        clients = await asyncio.gather(*[_open(tmp_path) for _ in range(60)])
        # 55 are refused at once, while the admitted 5 are still in DNS.
        await _wait_until(lambda: proxy.stats.refused >= 55)
        assert proxy.stats.active == 5 and proxy.stats.connections == 5
        assert resolver.calls == 5
        resolver.release.set()
        codes = await asyncio.gather(*[_status(r) for r, _ in clients])
        assert codes.count(200) == 5 and codes.count(429) == 55
        assert len(upstream.asked) == 5
        for _, w in clients:
            w.close()
    finally:
        resolver.release.set()
        await proxy.stop()
    assert proxy.stats.active == 0


async def test_concurrent_connects_cannot_exceed_the_lifetime_limit(tmp_path, upstream):
    resolver = GatedResolver()
    proxy = await _proxy(tmp_path, upstream, resolver, max_concurrent=1000, max_connections=3)
    try:
        clients = await asyncio.gather(*[_open(tmp_path) for _ in range(40)])
        await _wait_until(lambda: proxy.stats.refused >= 37)
        resolver.release.set()
        codes = await asyncio.gather(*[_status(r) for r, _ in clients])
        assert codes.count(200) == 3 and codes.count(429) == 37
        assert proxy.stats.connections == 3 and len(upstream.asked) == 3
        for _, w in clients:
            w.close()
        # Lifetime, not concurrent: closing them frees no attempt.
        await _wait_until(lambda: proxy.stats.active == 0)
        reader, writer = await _open(tmp_path)
        assert await _status(reader) == 429
        writer.close()
    finally:
        resolver.release.set()
        await proxy.stop()


@pytest.mark.parametrize("case", ["dns_failure", "blocked_address", "connect_failure", "ip_mismatch"])
async def test_a_failed_attempt_releases_its_slot_but_spends_its_attempt(tmp_path, upstream, case):
    resolver = GatedResolver(("10.0.0.5",) if case == "blocked_address" else (PUBLIC,),
                             fail=case == "dns_failure")
    resolver.release.set()
    if case == "connect_failure":
        async def refuse(ip, port):
            raise OSError("connection refused")
        upstream.connect = refuse  # type: ignore[method-assign]
    if case == "ip_mismatch":
        upstream.peer_override = "93.184.216.99"
    proxy = await _proxy(tmp_path, upstream, resolver, max_concurrent=1, max_connections=5)
    try:
        for _ in range(3):
            reader, writer = await _open(tmp_path)
            assert await _status(reader) in (403, 502)
            writer.close()
            await _wait_until(lambda: proxy.stats.active == 0)
        assert proxy.stats.connections == 3
    finally:
        await proxy.stop()


async def test_a_client_that_disconnects_during_dns_releases_its_slot(tmp_path, upstream):
    resolver = GatedResolver()
    proxy = await _proxy(tmp_path, upstream, resolver, max_concurrent=2)
    try:
        clients = await asyncio.gather(*[_open(tmp_path) for _ in range(2)])
        await _wait_until(lambda: proxy.stats.active == 2)
        for _, writer in clients:
            writer.close()
        resolver.release.set()
        await _wait_until(lambda: proxy.stats.active == 0)
        reader, writer = await _open(tmp_path)
        assert await _status(reader) == 200
        writer.close()
    finally:
        resolver.release.set()
        await proxy.stop()


async def test_stopping_the_proxy_mid_dns_releases_every_slot(tmp_path, upstream):
    resolver = GatedResolver()
    proxy = await _proxy(tmp_path, upstream, resolver, max_concurrent=10)
    clients = await asyncio.gather(*[_open(tmp_path) for _ in range(10)])
    await _wait_until(lambda: proxy.stats.active == 10)
    stopping = asyncio.ensure_future(proxy.stop())
    await asyncio.sleep(0.1)
    resolver.release.set()
    await stopping
    assert proxy.stats.active == 0
    for _, writer in clients:
        writer.close()


# ── the run's byte budget ─────────────────────────────────────────────────


async def test_concurrent_bidirectional_streams_never_exceed_the_budget(tmp_path, upstream):
    resolver = GatedResolver()
    resolver.release.set()
    budget = 20_000
    proxy = await _proxy(tmp_path, upstream, resolver, max_bytes=budget, max_concurrent=32)
    received = []

    async def stream() -> None:
        # Interleaved: send a chunk, read its echo, repeat — so both directions
        # of every tunnel draw on the one budget at the same time.
        reader, writer = await _open(tmp_path)
        assert await _status(reader) == 200
        got = 0
        try:
            for _ in range(40):
                writer.write(b"x" * 500)
                await writer.drain()
                chunk = await asyncio.wait_for(reader.read(65536), 10)
                if not chunk:
                    break
                got += len(chunk)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            received.append(got)
            writer.close()

    try:
        await asyncio.gather(*[stream() for _ in range(16)])  # 16 × 20 000 B offered each way
    finally:
        await proxy.stop()
    assert proxy.stats.bytes_up + proxy.stats.bytes_down <= budget
    assert sum(received) <= proxy.stats.bytes_down <= budget
    assert proxy.stats.bytes_up > 0 and proxy.stats.bytes_down > 0
    assert proxy.stats.active == 0


async def test_spending_the_budget_closes_every_tunnel_and_refuses_new_ones(tmp_path, upstream):
    resolver = GatedResolver()
    resolver.release.set()
    proxy = await _proxy(tmp_path, upstream, resolver, max_bytes=4096)
    try:
        idle_reader, idle_writer = await _open(tmp_path)
        assert await _status(idle_reader) == 200
        busy_reader, busy_writer = await _open(tmp_path)
        assert await _status(busy_reader) == 200
        busy_writer.write(b"y" * 10_000)
        await busy_writer.drain()
        # The idle tunnel spent nothing, but the run's budget is gone: it closes too.
        assert await asyncio.wait_for(idle_reader.read(10), 5) == b""
        assert proxy.stats.bytes_up + proxy.stats.bytes_down == 4096
        reader, writer = await _open(tmp_path)
        assert await _status(reader) == 429
        for w in (idle_writer, busy_writer, writer):
            w.close()
    finally:
        await proxy.stop()
    assert proxy.stats.reasons.get("byte_budget_spent") == 1
