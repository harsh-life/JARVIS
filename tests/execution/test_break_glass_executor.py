"""The executor side of break-glass (20 §2.1, §2.3) and the config key that
enables it (§2.2, §2.5).

The executor never picks unconfined execution itself: it asks a record store,
through a claim-only Protocol, right before it spawns. These tests drive it
with the real `BreakGlassRegistry` and, where Landlock is present, prove the
two paths apart by what the child can reach; where it is not, by the confined
path failing closed (`platform_unsupported`).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from server.composition.break_glass import BreakGlassRegistry
from server.composition.execution_tools import build_execution_tools
from server.config.schema import AppConfig, BreakGlassConfig, ProcessExecutionConfig
from server.execution import confinement
from server.execution.break_glass import InvocationOutcome
from server.execution.process import ConstrainedProcessExecutor, argv_digest
from server.agent.proposals import ProposalError, parse_proposal
from server.fs import FilesystemSandbox
from server.tools.platforms import ShellCommandAdapter
from shared.schemas.agent import ExecutionPlatform, ToolInvocation
from shared.schemas.execution import ExecutionError, ExecutionErrorCode
from tests.execution.test_process import _process_is_running
from tests.support import DisposableHostBreakGlass, make_superuser, make_test_config

pytestmark = pytest.mark.asyncio

TASK, USER = uuid.uuid4(), uuid.uuid4()


@pytest.fixture
def no_landlock(monkeypatch):
    """A host that cannot confine: the confined path fails closed, so any
    child that runs at all ran on the break-glass path."""

    monkeypatch.setattr(confinement, "landlock_abi", lambda: 0)


@pytest.fixture
def superuser(monkeypatch):
    return make_superuser(monkeypatch)


def registry(**overrides) -> BreakGlassRegistry:
    cfg = dict(enabled=True, allowed_executables=["cat", "sh", "sleep", "python3"], max_invocations=5)
    cfg.update(overrides)
    return BreakGlassRegistry(BreakGlassConfig(**cfg))


def live(reg: BreakGlassRegistry, superuser, *, executables=("cat",), max_invocations=1, task=TASK,
         user=USER, window=60):
    record = reg.prepare(superuser, task_id=task, task_owner=user, user_id=user, executables=list(executables),
                         max_invocations=max_invocations, window_seconds=window, task_seconds_left=120,
                         reason="test")
    reg.install(superuser, record)
    return record


def executor(reg, *, normal=(), break_glass_list=("cat", "sh", "sleep", "python3"), **kwargs):
    return ConstrainedProcessExecutor(allowed_executables=list(normal), break_glass_executables=list(break_glass_list),
                                      break_glass=reg, default_timeout_seconds=5.0, max_timeout_seconds=10.0,
                                      **kwargs)


async def run(ex, argv, tmp_path, *, task=TASK, user=USER, **kwargs):
    return await ex.run(argv, cwd=str(tmp_path), task_id=task, user_id=user, **kwargs)


async def refused(ex, argv, tmp_path, **kwargs) -> ExecutionErrorCode:
    with pytest.raises(ExecutionError) as excinfo:
        await run(ex, argv, tmp_path, **kwargs)
    return excinfo.value.code


# ── BG-T2: configuration ───────────────────────────────────────────────────


async def test_a_config_asking_for_unconfined_fails_to_load():
    for mode in ("unconfined", "none", "container", ""):
        with pytest.raises(ValidationError) as excinfo:
            make_test_config(execution={"process": {"confinement_mode": mode}})
        assert "break_glass" in str(excinfo.value), mode
    assert make_test_config().execution.process.confinement_mode == "landlock"
    assert make_test_config(execution={"process": {"confinement_mode": "landlock"}})


async def test_break_glass_config_defaults_are_closed_and_bounded():
    cfg = ProcessExecutionConfig().break_glass
    assert (cfg.enabled, cfg.allowed_executables, cfg.max_window_minutes, cfg.max_invocations) == (False, [], 15, 1)
    for bad in ({"max_window_minutes": 16}, {"max_window_minutes": 0}, {"max_invocations": 0},
                {"surprise": True}):
        with pytest.raises(ValidationError):
            BreakGlassConfig(**bad)


async def test_the_config_file_template_is_still_valid():
    import yaml
    from pathlib import Path

    payload = yaml.safe_load((Path(__file__).resolve().parents[2] / "config.example.yaml").read_text())
    assert AppConfig.model_validate(payload).execution.process.break_glass.enabled is False


# ── BG-T1: disabled means no unconfined path at all ────────────────────────


async def test_without_a_lookup_every_child_is_confined(no_landlock, tmp_path):
    ex = ConstrainedProcessExecutor(allowed_executables=["cat"], break_glass_executables=["cat"])
    assert await refused(ex, ["cat", "/dev/null"], tmp_path) is ExecutionErrorCode.PLATFORM_UNSUPPORTED


async def test_disabled_config_hands_the_executor_no_break_glass(no_landlock, tmp_path):
    """Even given a store that would grant every claim, `enabled: false` wires
    none of it into the executor."""

    config = make_test_config(execution={
        "filesystem": {"base_root": str(tmp_path / "sandboxes")},
        "process": {"allowed_executables": ["cat"],
                    "break_glass": {"enabled": False, "allowed_executables": ["cat"]}},
    })
    granting = DisposableHostBreakGlass()
    [shell] = [t for t in build_execution_tools(config, break_glass=granting) if t.contract.tool_id == "system.shell"]
    out = await shell.adapters[ExecutionPlatform.SERVER].execute(ToolInvocation(
        tool_id="system.shell", operation="run_shell_command", arguments={"argv": ["cat", "/dev/null"]},
        user_id=USER, task_id=TASK, platform=ExecutionPlatform.SERVER,
    ))
    assert (out.ok, out.error) == (False, "platform_unsupported")
    assert granting.claims == []


async def test_enabled_config_wires_the_store_into_the_executor(no_landlock, tmp_path, superuser):
    config = make_test_config(execution={
        "filesystem": {"base_root": str(tmp_path / "sandboxes")},
        "process": {"break_glass": {"enabled": True, "allowed_executables": ["cat"]}},
    })
    reg = BreakGlassRegistry(config.execution.process.break_glass)
    [shell] = [t for t in build_execution_tools(config, break_glass=reg) if t.contract.tool_id == "system.shell"]
    live(reg, superuser)
    out = await shell.adapters[ExecutionPlatform.SERVER].execute(ToolInvocation(
        tool_id="system.shell", operation="run_shell_command", arguments={"argv": ["cat", "/dev/null"]},
        user_id=USER, task_id=TASK, platform=ExecutionPlatform.SERVER,
    ))
    assert out.ok, out.error


# ── the claim: task, user, executable, count, time ─────────────────────────


async def test_a_live_record_runs_its_executable_unconfined(no_landlock, tmp_path, superuser):
    reg = registry()
    record = live(reg, superuser)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    workdir = tmp_path / "work"
    workdir.mkdir()

    result = await run(executor(reg), ["cat", str(outside)], workdir)

    assert result.content == "outside"
    [invoked, ended] = reg.drain(TASK)
    assert invoked.resource.startswith(f"bg:{record.record_id}:{TASK}:exited:0:")
    assert invoked.resource.endswith(f":{argv_digest(['cat', str(outside)])}:cat")
    assert ended.resource == f"bg:{record.record_id}:{TASK}:exhausted"


@pytest.mark.parametrize("mismatch", ["task", "user", "executable"])
async def test_a_record_binds_task_user_and_executable(no_landlock, tmp_path, superuser, mismatch):
    reg = registry()
    live(reg, superuser, executables=("cat",))
    ex = executor(reg, normal=("sh",))
    kwargs = {"task": uuid.uuid4()} if mismatch == "task" else {"user": uuid.uuid4()} if mismatch == "user" else {}
    argv = ["sh", "-c", "true"] if mismatch == "executable" else ["cat", "/dev/null"]

    code = await refused(ex, argv, tmp_path, **kwargs)

    # `sh` is on the normal list → the confined path, which fails closed here;
    # `cat` is only on the break-glass list → not allowed at all without a record.
    expected = (ExecutionErrorCode.PLATFORM_UNSUPPORTED if mismatch == "executable"
                else ExecutionErrorCode.UNAUTHORIZED_EXECUTABLE)
    assert code is expected
    assert reg.active()[0].remaining == 1  # nothing was spent


async def test_bg_t6_an_executable_off_the_break_glass_list_never_runs_unconfined(no_landlock, tmp_path,
                                                                                  superuser):
    # The record cannot name it (activation refuses), and a lookup that would
    # grant anything is never even asked for an executable off the list.
    granting = DisposableHostBreakGlass()
    ex = ConstrainedProcessExecutor(allowed_executables=["sh"], break_glass_executables=["cat"],
                                    break_glass=granting)
    assert await refused(ex, ["sh", "-c", "true"], tmp_path) is ExecutionErrorCode.PLATFORM_UNSUPPORTED
    assert await refused(ex, ["id"], tmp_path) is ExecutionErrorCode.UNAUTHORIZED_EXECUTABLE
    assert granting.claims == []


async def test_the_break_glass_list_does_not_widen_the_normal_allow_list(tmp_path):
    ex = ConstrainedProcessExecutor(allowed_executables=[], break_glass_executables=["cat"],
                                    break_glass=registry())
    assert await refused(ex, ["cat", "/dev/null"], tmp_path) is ExecutionErrorCode.UNAUTHORIZED_EXECUTABLE
    assert ex._normal.entries == frozenset()


async def test_a_bare_break_glass_name_is_execd_by_its_resolved_path(no_landlock, tmp_path, superuser):
    decoy_dir = tmp_path / "decoy"
    decoy_dir.mkdir()
    (decoy_dir / "cat").write_text("#!/bin/sh\necho DECOY\n")
    (decoy_dir / "cat").chmod(0o755)
    (tmp_path / "f.txt").write_text("genuine")
    reg = registry()
    live(reg, superuser, max_invocations=2)

    result = await run(executor(reg), ["cat", "f.txt"], tmp_path, env_overrides={"PATH": str(decoy_dir)})
    assert result.content == "genuine"
    # ... and a different absolute path with the same basename is not `cat`.
    assert await refused(executor(reg), [str(decoy_dir / "cat")], tmp_path) is \
        ExecutionErrorCode.UNAUTHORIZED_EXECUTABLE


async def test_repeated_invocation_after_max_is_rejected(no_landlock, tmp_path, superuser):
    reg = registry()
    live(reg, superuser, max_invocations=2)
    ex = executor(reg)
    await run(ex, ["cat", "/dev/null"], tmp_path)
    await run(ex, ["cat", "/dev/null"], tmp_path)
    assert await refused(ex, ["cat", "/dev/null"], tmp_path) is ExecutionErrorCode.UNAUTHORIZED_EXECUTABLE
    assert [e.resource.rsplit(":", 1)[1] for e in reg.drain(TASK)] == ["cat", "cat", "exhausted"]


async def test_an_expired_record_is_rejected(no_landlock, tmp_path, superuser):
    now = [datetime.now(timezone.utc)]
    reg = BreakGlassRegistry(BreakGlassConfig(enabled=True, allowed_executables=["cat"]), clock=lambda: now[0])
    record = live(reg, superuser, window=30)
    now[0] += timedelta(seconds=30)
    assert await refused(executor(reg), ["cat", "/dev/null"], tmp_path) is \
        ExecutionErrorCode.UNAUTHORIZED_EXECUTABLE
    assert [e.resource for e in reg.drain(TASK)] == [f"bg:{record.record_id}:{TASK}:expired"]


async def test_a_call_refused_for_any_other_reason_spends_nothing(no_landlock, tmp_path, superuser):
    reg = registry()
    live(reg, superuser)
    ex = executor(reg)
    for argv, kwargs in ((["cat", "a\x00b"], {}), ([], {}), (["cat"] * 300, {}),
                         (["cat", "/dev/null"], {"env_overrides": {"LD_PRELOAD": "x.so"}}),
                         (["cat", "/dev/null"], {"env_overrides": {"API_TOKEN": "x"}}),
                         (["cat", "/dev/null"], {"timeout": 0})):
        assert await refused(ex, argv, tmp_path, **kwargs) is ExecutionErrorCode.INVALID_ARGUMENTS
    assert reg.active()[0].remaining == 1


async def test_without_a_task_identity_nothing_is_claimed(no_landlock, tmp_path, superuser):
    reg = registry()
    live(reg, superuser)
    ex = executor(reg, normal=("cat",))
    with pytest.raises(ExecutionError) as excinfo:
        await ex.run(["cat", "/dev/null"], cwd=str(tmp_path))
    assert excinfo.value.code is ExecutionErrorCode.PLATFORM_UNSUPPORTED
    assert reg.active()[0].remaining == 1


# ── BG-T8: every limit still applies ───────────────────────────────────────


async def test_the_environment_is_still_built_from_scratch(no_landlock, tmp_path, superuser, monkeypatch):
    monkeypatch.setenv("SECRET_FROM_THE_HOST", "TEST-ONLY-must-not-leak")
    reg = registry()
    live(reg, superuser, executables=("sh",))
    result = await run(executor(reg), ["sh", "-c", "env"], tmp_path)
    assert "TEST-ONLY-must-not-leak" not in result.content
    assert "PATH=/usr/bin:/bin" in result.content


async def test_output_caps_and_timeouts_still_apply(no_landlock, tmp_path, superuser):
    reg = registry()
    live(reg, superuser, executables=("python3", "sleep"), max_invocations=2)
    ex = executor(reg, max_output_bytes=1000)
    big = await run(ex, ["python3", "-c", "print('x' * 100000)"], tmp_path)
    assert len(big.content) <= 1000 and big.metadata["truncated"] is True
    slow = await run(ex, ["sleep", "30"], tmp_path, timeout=1)
    assert slow.metadata["timed_out"] is True
    invoked = [e.resource for e in reg.drain(TASK) if ":exhausted" not in e.resource]
    assert ":exited:0:" in invoked[0] and ":timed_out:-1:" in invoked[1]


async def test_rlimits_still_apply(no_landlock, tmp_path, superuser):
    reg = registry()
    live(reg, superuser, executables=("python3",))
    result = await run(executor(reg), ["python3", "-c", "import resource; "
                                       "print(resource.getrlimit(resource.RLIMIT_AS)[0], "
                                       "resource.getrlimit(resource.RLIMIT_NOFILE)[0])"], tmp_path)
    assert result.content.split() == [str(512 * 1024 * 1024), "64"]


async def test_cancellation_kills_the_unconfined_process_group(no_landlock, tmp_path, superuser):
    reg = registry()
    live(reg, superuser, executables=("sh",))
    pidfile = tmp_path / "grandchild.pid"
    running = asyncio.ensure_future(run(executor(reg), ["sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait"],
                                        tmp_path, timeout=10))
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.05)
    grandchild = int(pidfile.read_text())
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    for _ in range(40):
        if not _process_is_running(grandchild):  # gone, or a zombie nobody has reaped yet
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("the unconfined grandchild outlived the cancelled call")
    [invoked, _ended] = reg.drain(TASK)
    assert f":{InvocationOutcome.CANCELLED.value}:-:" in invoked.resource


async def test_a_run_that_never_starts_is_still_recorded(no_landlock, tmp_path, superuser):
    reg = BreakGlassRegistry(BreakGlassConfig(enabled=True, allowed_executables=["/nonexistent/tool"]))
    live(reg, superuser, executables=("/nonexistent/tool",))
    ex = executor(reg, break_glass_list=("/nonexistent/tool",))
    assert await refused(ex, ["/nonexistent/tool"], tmp_path) is ExecutionErrorCode.INVALID_ARGUMENTS
    [invoked, ended] = reg.drain(TASK)
    assert ":not_started:-:" in invoked.resource and ended.resource.endswith(":exhausted")


@pytest.mark.skipif(not confinement.available(), reason="needs Landlock to tell the two paths apart")
async def test_on_a_landlock_host_the_record_is_what_removes_confinement(tmp_path, superuser):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    workdir = tmp_path / "work"
    workdir.mkdir()
    reg = registry()
    ex = executor(reg, normal=("cat",))

    confined = await run(ex, ["cat", str(outside)], workdir)
    assert confined.content == "" and confined.metadata["exit_code"] != 0
    live(reg, superuser)
    loose = await run(ex, ["cat", str(outside)], workdir)
    assert loose.content == "outside"
    again = await run(ex, ["cat", str(outside)], workdir)  # spent → confined again
    assert again.content == ""


# ── BG-T4 at the parser, and the adapter ───────────────────────────────────


@pytest.mark.parametrize("payload", [
    {"arguments": {"argv": ["cat"], "confinement_mode": "unconfined"}},
    {"arguments": {"argv": ["cat"], "nested": [{"break-glass": True}]}},
    {"arguments": {"argv": ["cat"], "SECCOMP": "off"}},
    {"arguments": {"argv": ["cat"]}, "confinement": "none"},
    {"arguments": {"argv": ["cat"]}, "unconfined": True},
    {"arguments": {"argv": ["cat"]}, "scope": {"landlock": "off"}},
])
async def test_bg_t4_a_proposal_carrying_a_confinement_field_is_malformed(payload):
    import json

    proposal = {"type": "tool_call", "tool": "system.shell", "operation": "run_shell_command", **payload}
    with pytest.raises(ProposalError):
        parse_proposal(json.dumps(proposal))


async def test_bg_t4_capability_requests_cannot_name_confinement_either():
    import json

    with pytest.raises(ProposalError):
        parse_proposal(json.dumps({"type": "request_capabilities", "capabilities": [
            {"capability": "system.restricted", "resource_scope": {"confinement": "none"}}]}))
    with pytest.raises(ProposalError):
        parse_proposal(json.dumps({"type": "request_capabilities", "break_glass": True,
                                   "capabilities": [{"capability": "system.restricted"}]}))


async def test_values_that_merely_mention_confinement_are_still_fine():
    import json

    parsed = parse_proposal(json.dumps({"type": "tool_call", "tool": "system.shell",
                                        "operation": "run_shell_command",
                                        "arguments": {"argv": ["echo", "unconfined landlock break-glass"]}}))
    assert parsed.arguments["argv"][1] == "unconfined landlock break-glass"


async def test_the_shell_adapter_refuses_unknown_arguments_and_passes_its_task_identity(no_landlock, tmp_path):
    granting = DisposableHostBreakGlass()
    ex = ConstrainedProcessExecutor(break_glass_executables=["cat"], break_glass=granting)
    adapter = ShellCommandAdapter(ex, sandbox=FilesystemSandbox(base_root=str(tmp_path / "sandboxes")))

    def invocation(**arguments):
        return ToolInvocation(tool_id="system.shell", operation="run_shell_command", arguments=arguments,
                              user_id=USER, task_id=TASK, platform=ExecutionPlatform.SERVER)

    for extra in ({"confinement_mode": "unconfined"}, {"cwd": "/"}, {"task_id": str(uuid.uuid4())}):
        out = await adapter.execute(invocation(argv=["cat", "/dev/null"], **extra))
        assert (out.ok, out.error) == (False, "invalid_arguments"), extra
    assert granting.claims == []

    out = await adapter.execute(invocation(argv=["cat", "/dev/null"]))
    assert out.ok, out.error
    [claim] = granting.claims
    assert (claim.task_id, claim.user_id, claim.executable) == (TASK, USER, "cat")


async def test_without_a_task_identity_even_a_granting_store_is_not_asked(no_landlock, tmp_path):
    """The executor itself refuses to claim without the invocation's task and
    user — it does not rely on the store to reject a missing identity."""

    granting = DisposableHostBreakGlass()
    ex = ConstrainedProcessExecutor(allowed_executables=["cat"], break_glass_executables=["cat"],
                                    break_glass=granting)
    for task_id, user_id in ((None, USER), (TASK, None), (None, None)):
        with pytest.raises(ExecutionError) as excinfo:
            await ex.run(["cat", "/dev/null"], cwd=str(tmp_path), task_id=task_id, user_id=user_id)
        assert excinfo.value.code is ExecutionErrorCode.PLATFORM_UNSUPPORTED
    assert granting.claims == []


async def test_a_store_that_fails_to_record_does_not_break_the_run(no_landlock, tmp_path):
    class Failing(DisposableHostBreakGlass):
        def record_invocation(self, claim, **details):
            raise RuntimeError("store unavailable")

    ex = ConstrainedProcessExecutor(break_glass_executables=["cat"], break_glass=Failing())
    result = await run(ex, ["cat", "/dev/null"], tmp_path)
    assert result.metadata["exit_code"] == 0
