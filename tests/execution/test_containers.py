"""The external runtime's container boundary — Phase 6 slice 6C (unit).

OD-AF-11/12/14 (ratified 2026-10-02, register §2L): one disposable container
per run, launched by the JARVIS server's own unprivileged user through a
**rootless** Podman with **gVisor `runsc`** as the runtime:

* no network interface (Podman `--network=none`, gVisor `network=none` —
  loopback only) — the only ways out are the run's sockets, mounted in;
* gVisor `host-uds=open`: a host socket is reachable only if it was mounted;
* non-root, read-only root, no capabilities, `no-new-privileges`, the run's
  scratch directory the only writable mount, no tmpfs elsewhere;
* only a digest-pinned image, never pulled at run time;
* only the run's few environment values — podman itself starts from a
  from-scratch environment, so nothing of the server's (a provider key, a
  proxy setting, a session) can reach the container;
* a deterministic kill (stop after the grace, then removal) and a labelled
  container per run, so reconciliation can find what is not a live run's.

These tests check the exact launch against a fake engine that records what
it is asked; `test_containers_live.py` runs the same launch for real.
"""

from __future__ import annotations

import json
import os
import stat
import uuid

import pytest

from server.execution.containers import (
    CONTAINER_SCRATCH,
    CONTAINER_SOCKETS,
    ContainerEngine,
    ContainerError,
    ContainerSpec,
    EngineSettings,
    container_name,
    prepare_workspace,
    remove_workspace,
    run_argv,
)

DIGEST = "sha256:" + "a" * 64
IMAGE = f"ghcr.io/harsh-life/jarvis-browser-use@{DIGEST}"
RUN = uuid.UUID("11111111-2222-3333-4444-555555555555")
AGENT = uuid.UUID("66666666-7777-8888-9999-000000000000")


def _spec(tmp_path, **overrides) -> ContainerSpec:
    sockets, scratch = tmp_path / "sockets", tmp_path / "scratch"
    sockets.mkdir(exist_ok=True)
    scratch.mkdir(exist_ok=True)
    values = dict(run_id=RUN, agent_id=AGENT, image=IMAGE, socket_dir=str(sockets), scratch_dir=str(scratch),
                  env={"JARVIS_RUN_TOKEN": "t" * 43, "JARVIS_TASK_FILE": "/scratch/task.json"})
    values.update(overrides)
    return ContainerSpec(**values)


SETTINGS = EngineSettings(podman="/usr/bin/podman")


def _flags(argv: list[str]) -> list[str]:
    return argv[: argv.index(IMAGE)]


# ── the launch, exactly ─────────────────────────────────────────────────────


def test_the_launch_carries_every_isolation_flag(tmp_path) -> None:
    argv = run_argv(_spec(tmp_path), SETTINGS, uid=1000, gid=1000)
    flags = _flags(argv)
    assert argv[:3] == ["/usr/bin/podman", "run", "--detach"]
    for required in ("--runtime=runsc", "--runtime-flag=network=none", "--runtime-flag=host-uds=open",
                     "--network=none", "--read-only", "--read-only-tmpfs=false", "--cap-drop=ALL",
                     "--security-opt=no-new-privileges", "--userns=keep-id", "--user=1000:1000",
                     "--pull=never", "--http-proxy=false", "--log-driver=none", "--pids-limit=512",
                     "--memory=2048m", "--cpus=2.0", f"--name={container_name(RUN)}",
                     "--label=io.jarvis.managed=1", f"--label=io.jarvis.run_id={RUN}",
                     f"--label=io.jarvis.agent_id={AGENT}"):
        assert required in flags, required
    mounts = [f for f in flags if f.startswith("--mount=")]
    assert mounts == [
        f"--mount=type=bind,src={tmp_path / 'sockets'},dst={CONTAINER_SOCKETS}",
        f"--mount=type=bind,src={tmp_path / 'scratch'},dst={CONTAINER_SCRATCH}",
        # gVisor would otherwise give the sandbox a writable in-memory /tmp.
        "--mount=type=tmpfs,dst=/tmp,ro=true,tmpfs-size=1k",
    ]
    assert sorted(f for f in flags if f.startswith("--env=")) == [
        "--env=JARVIS_RUN_TOKEN=" + "t" * 43, "--env=JARVIS_TASK_FILE=/scratch/task.json"]
    assert argv[-1] == IMAGE


def test_nothing_that_widens_the_boundary_is_ever_in_the_launch(tmp_path) -> None:
    flags = " ".join(_flags(run_argv(_spec(tmp_path), SETTINGS, uid=1000, gid=1000)))
    for forbidden in ("--privileged", "--network=host", "--network=bridge", "slirp", "pasta", "--cap-add",
                      "--device", "seccomp=unconfined", "apparmor=unconfined", "--volume", "-v ", "--env-host",
                      "--env-file", "--pid=host", "--ipc=host", "--uts=host", "--userns=host", "--tmpfs",
                      "--security-opt=label=disable", "--rm", "--runtime=runc", "--runtime=crun",
                      "--user=0", "--user=root", "/var/run", "podman.sock", "docker.sock"):
        assert forbidden not in flags, forbidden


def test_ignore_cgroups_is_the_operators_explicit_choice(tmp_path) -> None:
    assert "--runtime-flag=ignore-cgroups" not in run_argv(_spec(tmp_path), SETTINGS, uid=1000, gid=1000)
    relaxed = EngineSettings(podman="/usr/bin/podman", ignore_cgroups=True)
    assert "--runtime-flag=ignore-cgroups" in run_argv(_spec(tmp_path), relaxed, uid=1000, gid=1000)


def test_the_container_never_runs_as_root(tmp_path) -> None:
    with pytest.raises(ContainerError, match="root"):
        run_argv(_spec(tmp_path), SETTINGS, uid=0, gid=0)


@pytest.mark.parametrize("image", [
    "ghcr.io/harsh-life/jarvis-browser-use:latest", "ghcr.io/harsh-life/jarvis-browser-use",
    "ghcr.io/harsh-life/jarvis-browser-use:0.13.10", "python:3.12-alpine",
    f"ghcr.io/x@sha256:{'a' * 63}", f"ghcr.io/x@sha256:{'A' * 64}", f"ghcr.io/x:tag@{DIGEST} --privileged",
    f"--privileged@{DIGEST}", "",
])
def test_only_a_digest_pinned_image_is_ever_launched(tmp_path, image) -> None:
    with pytest.raises(ValueError):
        _spec(tmp_path, image=image)


@pytest.mark.parametrize("env", [
    {"OPENAI_API_KEY": "sk-x"}, {"HTTPS_PROXY": "http://x"}, {"PATH": "/tmp"}, {"LD_PRELOAD": "/x.so"},
    {"JARVIS_RUN_TOKEN": "a\nb"}, {"JARVIS_RUN_TOKEN": "x\x00y"}, {"jarvis_run_token": "x"},
])
def test_only_the_runs_own_environment_values_are_accepted(tmp_path, env) -> None:
    with pytest.raises(ValueError):
        _spec(tmp_path, env=env)


@pytest.mark.parametrize("field,value", [
    ("socket_dir", "relative/sockets"), ("scratch_dir", "relative"), ("socket_dir", "/"),
    ("scratch_dir", "/home"), ("socket_dir", "/var/run/docker.sock"),
])
def test_mounts_are_the_runs_own_directories_only(tmp_path, field, value) -> None:
    with pytest.raises(ValueError):
        _spec(tmp_path, **{field: value})


# ── the per-run workspace ───────────────────────────────────────────────────


def test_a_runs_workspace_is_private_and_removed(tmp_path) -> None:
    sockets, scratch = prepare_workspace(tmp_path, RUN)
    for directory in (sockets, scratch, sockets.parent):
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    (scratch / "result.json").write_text("{}")
    remove_workspace(tmp_path, RUN)
    assert not sockets.parent.exists()
    remove_workspace(tmp_path, RUN)   # idempotent


def test_a_workspace_path_cannot_escape_its_base(tmp_path) -> None:
    sockets, _ = prepare_workspace(tmp_path, RUN)
    assert sockets.resolve().is_relative_to(tmp_path.resolve())
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / str(AGENT)).symlink_to(outside)
    with pytest.raises(ContainerError):
        prepare_workspace(tmp_path, AGENT)    # a symlink planted at the run's path is refused


# ── the engine, against a fake podman ───────────────────────────────────────


FAKE_PODMAN = r'''#!/usr/bin/env python3
import json, os, sys
log = os.environ["FAKE_LOG"]
with open(log, "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "env": dict(os.environ)}) + "\n")
state = json.load(open(os.environ["FAKE_STATE"]))
args = sys.argv[1:]
runtime = None
if args and args[0].startswith("--runtime="):
    runtime = args.pop(0).split("=", 1)[1]
cmd = args[0]
if cmd == "info":
    # `podman --runtime=X info` reports X only if podman can find it.
    found = runtime if runtime in state.get("available", ["runsc", "runc"]) else state.get("default", "crun")
    print(json.dumps({"host": {"security": {"rootless": state.get("rootless", True)},
                               "ociRuntime": {"name": found}}}))
elif cmd == "run":
    print("c0ffee" * 10)
elif cmd == "wait":
    print(state.get("exit", 0))
elif cmd == "ps":
    print(json.dumps(state.get("containers", [])))
sys.exit(state.get("rc", {}).get(cmd, 0))
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    script = tmp_path / "podman"
    script.write_text(FAKE_PODMAN)
    script.chmod(0o755)
    log, state = tmp_path / "log.jsonl", tmp_path / "state.json"
    state.write_text("{}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_STATE", str(state))

    class Fake:
        path = str(script)

        def calls(self):
            return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

        def set(self, **values):
            state.write_text(json.dumps(values))

    return Fake()


def _engine(fake, **settings) -> ContainerEngine:
    # The fake needs its two variables passed through; the real engine passes
    # nothing of the server's environment but the engine's own (see below).
    return ContainerEngine(EngineSettings(podman=fake.path, **settings), euid=lambda: 1000,
                           uid=1000, gid=1000, passthrough=("FAKE_LOG", "FAKE_STATE"))


async def test_the_engine_refuses_to_run_as_root(fake) -> None:
    engine = ContainerEngine(EngineSettings(podman=fake.path), euid=lambda: 0, uid=0, gid=0)
    with pytest.raises(ContainerError, match="root"):
        await engine.verify()


async def test_the_engine_must_be_rootless_with_runsc(fake) -> None:
    fake.set(rootless=False)
    with pytest.raises(ContainerError, match="rootless"):
        await _engine(fake).verify()
    fake.set(rootless=True, available=["runc", "crun"])
    with pytest.raises(ContainerError, match="runsc"):
        await _engine(fake).verify()
    fake.set(rootless=True)
    await _engine(fake).verify()


async def test_podman_gets_none_of_the_servers_environment(fake, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "TEST-ONLY-sk-provider-key")
    monkeypatch.setenv("HYPERMIND_KEK", "TEST-ONLY-kek")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    await _engine(fake).start(_spec(tmp_path))
    [call] = fake.calls()
    raw = json.dumps(call)
    for leaked in ("TEST-ONLY-sk-provider-key", "TEST-ONLY-kek", "proxy.invalid", "OPENAI_API_KEY", "HYPERMIND_KEK"):
        assert leaked not in raw, leaked
    assert set(call["env"]) <= {"PATH", "HOME", "XDG_RUNTIME_DIR", "LANG", "FAKE_LOG", "FAKE_STATE"}


async def test_start_launches_exactly_the_built_argv(fake, tmp_path) -> None:
    name = await _engine(fake).start(_spec(tmp_path))
    assert name == container_name(RUN)
    [call] = fake.calls()
    assert [fake.path, *call["argv"]] == run_argv(_spec(tmp_path), EngineSettings(podman=fake.path), uid=1000,
                                                    gid=1000)


async def test_a_failed_start_is_an_error_and_leaves_nothing(fake, tmp_path) -> None:
    fake.set(rc={"run": 125})
    with pytest.raises(ContainerError):
        await _engine(fake).start(_spec(tmp_path))
    assert [c["argv"][0] for c in fake.calls()] == ["run", "rm"]


async def test_the_kill_path_stops_after_the_grace_then_removes(fake) -> None:
    await _engine(fake, kill_grace_seconds=10).stop(container_name(RUN))
    argvs = [c["argv"] for c in fake.calls()]
    assert argvs == [["stop", "--time=10", container_name(RUN)], ["rm", "--force", container_name(RUN)]]


async def test_the_kill_path_removes_even_when_stop_fails(fake) -> None:
    fake.set(rc={"stop": 125})
    await _engine(fake).stop(container_name(RUN))
    assert [c["argv"][0] for c in fake.calls()] == ["stop", "rm"]


async def test_a_container_name_is_only_ever_a_runs(fake) -> None:
    for bad in ("other-container", "jarvis-run-../x", "--all", f"jarvis-run-{RUN};rm"):
        with pytest.raises(ContainerError):
            await _engine(fake).stop(bad)
    assert fake.calls() == []


async def test_wait_reports_the_exit_code(fake) -> None:
    fake.set(exit=3)
    assert await _engine(fake).wait(container_name(RUN), timeout=5) == 3


async def test_reconciliation_removes_every_managed_container_that_is_not_a_live_run(fake) -> None:
    live, orphan = uuid.uuid4(), uuid.uuid4()
    ids = {"live": "1" * 64, "orphan": "2" * 64, "forged": "3" * 64}
    fake.set(containers=[
        {"Id": ids["live"], "Names": [container_name(live)],
         "Labels": {"io.jarvis.managed": "1", "io.jarvis.run_id": str(live)}, "State": "running"},
        {"Id": ids["orphan"], "Names": [container_name(orphan)],
         "Labels": {"io.jarvis.managed": "1", "io.jarvis.run_id": str(orphan)}, "State": "running"},
        {"Id": ids["forged"], "Names": ["jarvis-run-forged"],
         "Labels": {"io.jarvis.managed": "1", "io.jarvis.run_id": "not-a-uuid"}, "State": "exited"},
    ])
    removed = await _engine(fake).reconcile({live})
    assert removed == [container_name(orphan), "jarvis-run-forged"]
    argvs = [c["argv"] for c in fake.calls()]
    assert argvs[0][:2] == ["ps", "--all"] and "--filter=label=io.jarvis.managed=1" in argvs[0]
    # Removal is by the engine's own container id — never by a name or label
    # the container could have chosen.
    assert ["stop", "--time=10", ids["orphan"]] in argvs and ["rm", "--force", ids["orphan"]] in argvs
    assert ["rm", "--force", ids["forged"]] in argvs
    assert not any(ids["live"] in argv for argv in argvs)


async def test_reconciliation_ignores_an_entry_without_a_proper_id(fake) -> None:
    fake.set(containers=[{"Id": "--all", "Names": ["x"], "Labels": {"io.jarvis.managed": "1"}, "State": "running"}])
    assert await _engine(fake).reconcile(set()) == []
    assert [c["argv"][0] for c in fake.calls()] == ["ps"]
