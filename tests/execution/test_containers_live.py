"""The container boundary, for real — Phase 6 slice 6C (AGENT-T12, AGENT-T13).

These run the exact launch of `server/execution/containers.py` on a real
rootless Podman with gVisor `runsc`, as an unprivileged user, and probe the
boundary from inside the way a compromised runtime would.

**The stack.** Podman (rootless, run by a non-root user) and `runsc` must be
installed, and the probe image (`HYPERMIND_TEST_CONTAINER_IMAGE`, a
digest-pinned reference) present or pullable. Without the stack these tests
skip with the reason; with `HYPERMIND_REQUIRE_CONTAINER_STACK=1` — set in
CI's `containers` job — a missing stack is a failure instead, so AGENT-T12
and AGENT-T13 cannot pass by not running (the memory suite's precedent).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from server.execution.containers import ContainerEngine, ContainerSpec, EngineSettings, container_name
from server.net.egress_proxy import EgressProxy, ProxyPolicy

IMAGE = os.environ.get(
    "HYPERMIND_TEST_CONTAINER_IMAGE",
    "docker.io/library/python@sha256:2055081c860db8c842b663415b795b026e7e5cc63b59c5193fcc9997713ea5b8",
)
REQUIRE = os.environ.get("HYPERMIND_REQUIRE_CONTAINER_STACK") == "1"
IGNORE_CGROUPS = os.environ.get("HYPERMIND_CONTAINER_IGNORE_CGROUPS") == "1"
HOST = "advisories.example.org"
PUBLIC = "93.184.216.34"


def _stack_problem() -> str | None:
    podman = shutil.which("podman")
    if podman is None:
        return "podman is not installed"
    if shutil.which("runsc") is None:
        return "gVisor runsc is not installed"
    if os.geteuid() == 0:
        return "the container tests run as an unprivileged user (rootless engine)"
    probe = subprocess.run([podman, "image", "exists", IMAGE], capture_output=True)
    if probe.returncode != 0:
        pulled = subprocess.run([podman, "pull", "--quiet", IMAGE], capture_output=True)
        if pulled.returncode != 0:
            return f"the probe image {IMAGE} is not available"
    return None


_PROBLEM = _stack_problem()
if _PROBLEM is not None and REQUIRE:
    pytest.fail(f"HYPERMIND_REQUIRE_CONTAINER_STACK=1 but {_PROBLEM}", pytrace=False)
pytestmark = pytest.mark.skipif(_PROBLEM is not None, reason=f"container stack: {_PROBLEM}")


def _engine() -> ContainerEngine:
    return ContainerEngine(EngineSettings(podman=shutil.which("podman") or "podman", ignore_cgroups=IGNORE_CGROUPS,
                                          kill_grace_seconds=2))


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    sockets, scratch = tmp_path / "sockets", tmp_path / "scratch"
    for d in (sockets, scratch):
        d.mkdir(mode=0o700)
    return sockets, scratch


def _spec(sockets: Path, scratch: Path, run_id: uuid.UUID, *command: str, env=None) -> ContainerSpec:
    return ContainerSpec(run_id=run_id, agent_id=uuid.uuid4(), image=IMAGE, socket_dir=str(sockets),
                         scratch_dir=str(scratch), env=env or {"JARVIS_TASK_FILE": "/scratch/task.json"},
                         command=command)


async def _run(engine: ContainerEngine, spec: ContainerSpec, timeout: float = 120) -> int | None:
    name = await engine.start(spec)
    try:
        return await engine.wait(name, timeout=timeout)
    finally:
        await engine.stop(name)


PROBE = r'''
import json, os, socket
out = {}
def connect(addr):
    try:
        socket.create_connection(addr, timeout=3).close(); return "CONNECTED"
    except OSError as e:
        return f"{type(e).__name__}:{e.errno}"
out["direct"] = {f"{h}:{p}": connect((h, p)) for h, p in [
    ("1.1.1.1", 443), ("8.8.8.8", 53), ("10.0.0.1", 443), ("172.17.0.1", 443), ("192.168.1.1", 80),
    ("169.254.169.254", 80), ("100.64.0.1", 443), ("127.0.0.1", 8000), ("::1", 8000)]}
try:
    socket.getaddrinfo("example.com", 443); out["dns"] = "RESOLVED"
except OSError as e:
    out["dns"] = type(e).__name__
out["interfaces"] = [l.split(":")[0].strip() for l in open("/proc/net/dev").read().splitlines()[2:]]
def via_proxy(host):
    s = socket.socket(socket.AF_UNIX); s.connect("/run/jarvis/egress.sock")
    s.sendall(f"CONNECT {host}:443 HTTP/1.1\r\n\r\n".encode())
    head = s.recv(4096)
    code = int(head.split(b" ")[1])
    echo = None
    if code == 200:
        s.sendall(b"ping-through-tunnel"); echo = s.recv(100).decode()
    s.close(); return code, echo
out["proxy_allowed"] = via_proxy("advisories.example.org")
out["proxy_denied"] = via_proxy("evil.example.com")
out["proxy_metadata"] = via_proxy("169.254.169.254")
status = open("/proc/self/status").read()
out["uid"] = os.getuid()
out["capeff"] = status.split("CapEff:")[1].split()[0]
out["nonewprivs"] = status.split("NoNewPrivs:")[1].split()[0]
def writable(path):
    try:
        with open(path, "w") as f: f.write("x")
        return True
    except OSError:
        return False
out["write_root"] = writable("/etc/jarvis-probe")
out["write_tmp"] = writable("/tmp/jarvis-probe")
out["write_scratch"] = writable("/scratch/ok")
out["env_keys"] = sorted(os.environ)
out["environ_raw"] = open("/proc/self/environ").read()
out["visible"] = {p: os.path.exists(p) for p in ["/home/user/JARVIS", "/var/run/docker.sock",
                                                  "/run/podman", "/root/.ccr", "/etc/shadow-host"]}
out["socket_dir"] = sorted(os.listdir("/run/jarvis"))
json.dump(out, open("/scratch/probe.json", "w"))
'''


@pytest.fixture
async def echo_server():
    async def echo(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[1]
    server.close()


async def test_the_runtime_reaches_only_its_sockets_and_the_proxy_decides(tmp_path, echo_server) -> None:
    """AGENT-T12: from inside, every direct address — public, private,
    metadata, the host — is unreachable and DNS fails; through the run's
    proxy, the allowed host is reachable and nothing else is."""

    sockets, scratch = _workspace(tmp_path)
    (scratch / "probe.py").write_text(PROBE)

    async def connect(ip, port):
        reader, writer = await asyncio.open_connection("127.0.0.1", echo_server)
        return reader, writer, ip

    proxy = EgressProxy(binding=f"unix:{sockets / 'egress.sock'}", policy=ProxyPolicy(hosts=frozenset({HOST})),
                        resolve=lambda host, port: [PUBLIC], connect=connect)
    await proxy.start()
    try:
        code = await _run(_engine(), _spec(sockets, scratch, uuid.uuid4(), "python3", "/scratch/probe.py"))
    finally:
        await proxy.stop()
    assert code == 0
    out = json.loads((scratch / "probe.json").read_text())
    assert all(not result.startswith("CONNECTED") for result in out["direct"].values()), out["direct"]
    assert out["dns"] != "RESOLVED"
    assert set(out["interfaces"]) <= {"lo"}
    assert out["proxy_allowed"] == [200, "ping-through-tunnel"]
    assert out["proxy_denied"][0] == 403 and out["proxy_metadata"][0] in (400, 403)
    assert out["socket_dir"] == ["egress.sock"]


async def test_the_runtime_is_unprivileged_and_confined_to_its_scratch(tmp_path) -> None:
    sockets, scratch = _workspace(tmp_path)
    (scratch / "probe.py").write_text(PROBE.replace('out["proxy_allowed"] = via_proxy("advisories.example.org")\n'
                                                    'out["proxy_denied"] = via_proxy("evil.example.com")\n'
                                                    'out["proxy_metadata"] = via_proxy("169.254.169.254")\n', ""))
    code = await _run(_engine(), _spec(sockets, scratch, uuid.uuid4(), "python3", "/scratch/probe.py"))
    assert code == 0
    out = json.loads((scratch / "probe.json").read_text())
    assert out["uid"] != 0
    assert out["capeff"] == "0000000000000000"
    assert out["nonewprivs"] == "1"
    assert out["write_root"] is False and out["write_tmp"] is False and out["write_scratch"] is True
    assert not any(out["visible"].values()), out["visible"]
    assert set(out["env_keys"]) - {"PATH", "HOSTNAME", "HOME", "TERM", "container", "LANG", "GPG_KEY",
                                   "PYTHON_VERSION", "PYTHON_SHA256", "JARVIS_TASK_FILE"} == set()


async def test_no_provider_key_or_server_secret_reaches_the_container(tmp_path, monkeypatch) -> None:
    """AGENT-T13: the server's own environment holds a provider key, the KEK
    source and a proxy setting; none of it is in the container's environment,
    its inspect record or its scratch."""

    key, kek = f"TEST-ONLY-sk-{uuid.uuid4().hex}", f"TEST-ONLY-kek-{uuid.uuid4().hex}"
    monkeypatch.setenv("OPENAI_API_KEY", key)
    monkeypatch.setenv("HYPERMIND_KEK", kek)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    sockets, scratch = _workspace(tmp_path)
    (scratch / "probe.py").write_text(
        "import os, json; json.dump({'environ': open('/proc/self/environ').read()}, open('/scratch/env.json','w'))")
    engine = _engine()
    run_id = uuid.uuid4()
    name = await engine.start(_spec(sockets, scratch, run_id, "python3", "/scratch/probe.py"))
    try:
        await engine.wait(name, timeout=120)
        inspect = subprocess.run([shutil.which("podman"), "inspect", name], capture_output=True, text=True).stdout
    finally:
        await engine.stop(name)
    leaked_anywhere = inspect + "".join(p.read_text(errors="replace") for p in scratch.rglob("*") if p.is_file())
    for secret in (key, kek, "proxy.invalid", "OPENAI_API_KEY", "HYPERMIND_KEK"):
        assert secret not in leaked_anywhere, secret


async def test_cancellation_actually_terminates_the_runtime(tmp_path) -> None:
    sockets, scratch = _workspace(tmp_path)
    engine = _engine()
    run_id = uuid.uuid4()
    name = await engine.start(_spec(sockets, scratch, run_id, "python3", "-c",
                                    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(600)"))
    began = time.monotonic()
    await engine.stop(name)                         # SIGTERM is ignored: the kill after the grace ends it
    assert time.monotonic() - began < 60
    assert all(c.name != name for c in await engine.managed())


async def test_reconciliation_removes_a_container_that_is_no_live_runs(tmp_path) -> None:
    sockets, scratch = _workspace(tmp_path)
    engine = _engine()
    live_run, orphan_run = uuid.uuid4(), uuid.uuid4()
    live = await engine.start(_spec(sockets, scratch, live_run, "sleep", "600"))
    orphan = await engine.start(_spec(sockets, scratch, orphan_run, "sleep", "600"))
    try:
        removed = await engine.reconcile({live_run})
        names = {c.name for c in await engine.managed()}
        assert removed == [container_name(orphan_run)]
        assert orphan not in names and live in names
    finally:
        await engine.stop(live)
        await engine.stop(orphan)
