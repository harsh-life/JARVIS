"""End-to-end acceptance — the integration-hardening branch's cases A–J.

Every case runs the production composition root with the **real** execution
tools (`server.fs`, `server.net`, `server.execution`), the real Security Core,
and real confirmation tokens. The model is the only substitute, because it is
the one component the architecture treats as untrusted: each script plays it
either cooperatively or as the attacker.

    agent proposes → deterministic infrastructure authorizes → tools execute
        → human confirms where the risk model requires

"No execution occurred" is asserted from the ledgers, not from the model's
narration: a tool execution always leaves exactly one `tool_call` UsageEvent
and an `agent.tool.executed`/`agent.tool.failed` AuditEvent (USAGE-001), so an
empty ledger is evidence, not an absence of logging.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path

import httpx
import pytest

from server.execution import confinement
from server.models.provider import ModelUnavailable
from server.secrets.requester import SecretRequester
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.storage.models import (
    AgentTask,
    AuditEvent,
    CapabilityGrant,
    ConfirmationToken,
    IdempotencyKey,
    PermissionDecision,
    UsageEvent,
)
from shared.schemas.enums import SecretClass, SecretOwnerScopeType, UsageKind
from tests.runtime.conftest import ask, call, failure_of, final, pending_of, say
from tests.support import requires_landlock

pytestmark = pytest.mark.asyncio

API = "/api/v1"


# ── helpers ────────────────────────────────────────────────────────────────


def real_tools(tmp_path: Path, *, process: dict | None = None, network: dict | None = None) -> dict:
    return {
        "execution": {
            "filesystem": {"base_root": str(tmp_path / "sandboxes")},
            "process": {**(process or {})},
            "network": network or {},
        }
    }


def sandbox(tmp_path: Path, user_id: uuid.UUID, label: str) -> Path:
    return (tmp_path / "sandboxes").resolve() / "users" / str(user_id) / "private" / label


def completed(resp: httpx.Response) -> dict:
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed", body
    return body


async def tool_executions(h) -> list[UsageEvent]:
    return [u for u in await h.rows(UsageEvent) if u.kind is UsageKind.TOOL_CALL]


async def actions(h) -> list[str]:
    return [row.action for row in await h.rows(AuditEvent)]


# ── CASE A — ordinary low-risk request ────────────────────────────────────


async def test_case_a_low_risk_request_runs_automatically_end_to_end(make_harness, tmp_path):
    h = await make_harness(config=real_tools(tmp_path), use_real_execution_tools=True)
    alice = await h.user("alice")
    notes = {"sandbox_root": "notes"}
    await h.grant(alice, "file.write", resource_scope=notes)
    await h.grant(alice, "file.read", resource_scope=notes)

    h.model.push(
        ask("file.write", "file.read", scope=notes),
        call("files.write", "create_file", args={"relative_path": "todo.txt", "content": "buy milk"}),
        call("files.read", "read_file", args={"relative_path": "todo.txt"}),
        final("saved and read back"),
    )
    body = completed(await h.submit(alice, "remember to buy milk"))

    assert body["response"] == "saved and read back"
    assert body["counters"]["tool_calls"] == 2
    # The real sandbox, derived from the server-side user id — not a path the
    # model supplied.
    assert (sandbox(tmp_path, alice.user_id, "notes") / "todo.txt").read_text() == "buy milk"
    # The read result reached the model as an observation (data, not authority).
    assert "buy milk" in h.model.all_text()

    assert len(await tool_executions(h)) == 2
    recorded = await actions(h)
    assert recorded.count(AuditAction.AGENT_TOOL_EXECUTED.value) == 2
    assert AuditAction.AGENT_TASK_COMPLETED.value in recorded
    # Low-risk: no confirmation was ever minted.
    assert await h.rows(ConfirmationToken) == []


# ── CASE B — confirmation-required request ────────────────────────────────


async def test_case_b_consequential_action_waits_for_the_human(make_harness, tmp_path):
    h = await make_harness(config=real_tools(tmp_path), use_real_execution_tools=True)
    alice = await h.user("alice")
    notes = {"sandbox_root": "notes"}
    await h.grant(alice, "file.write", resource_scope=notes)
    target = sandbox(tmp_path, alice.user_id, "notes") / "old.txt"

    h.model.push(
        ask("file.write", scope=notes),
        call("files.write", "create_file", args={"relative_path": "old.txt", "content": "x"}),
        call("files.write", "delete_file", args={"relative_path": "old.txt"}),
    )
    paused = pending_of(await h.submit(alice, "tidy up"))

    # Deterministic authorization paused it; nothing was deleted yet.
    assert paused["pending"]["risk_category"] == "consequential"
    assert paused["pending"]["operation"] == "delete_file"
    assert target.exists()

    h.model.push(final("deleted"))
    body = completed(await h.confirm(alice, paused["task_id"], paused["confirmation_token"]))

    assert body["response"] == "deleted"
    assert not target.exists()
    recorded = await actions(h)
    assert AuditAction.CONFIRMATION_ISSUED.value in recorded
    assert AuditAction.CONFIRMATION_ACCEPTED.value in recorded
    # The token was spent exactly once.
    tokens = await h.rows(ConfirmationToken)
    assert len(tokens) == 1 and tokens[0].used_at is not None


async def test_case_b_a_confirmation_cannot_be_replayed(make_harness, tmp_path):
    h = await make_harness(config=real_tools(tmp_path), use_real_execution_tools=True)
    alice = await h.user("alice")
    notes = {"sandbox_root": "notes"}
    await h.grant(alice, "file.write", resource_scope=notes)
    h.model.push(
        ask("file.write", scope=notes),
        call("files.write", "create_file", args={"relative_path": "a.txt", "content": "x"}),
        call("files.write", "delete_file", args={"relative_path": "a.txt"}),
    )
    paused = pending_of(await h.submit(alice))
    h.model.push(final("ok"))
    completed(await h.confirm(alice, paused["task_id"], paused["confirmation_token"]))

    replay = await h.confirm(alice, paused["task_id"], paused["confirmation_token"])
    assert replay.status_code in (404, 409)
    assert len(await tool_executions(h)) == 2  # create + the one confirmed delete


# ── CASE C — denied request ───────────────────────────────────────────────


async def test_case_c_an_ungranted_capability_never_executes(make_harness, tmp_path):
    h = await make_harness(config=real_tools(tmp_path), use_real_execution_tools=True)
    alice = await h.user("alice")  # holds no grants at all

    h.model.push(
        # Straight to the tool: the capability is not active for this task.
        call("files.read", "read_file", args={"relative_path": "anything.txt"}, scope={"sandbox_root": "x"}),
        # Asking to activate it is a consequential grant-create — the human is asked.
        ask("file.read", scope={"sandbox_root": "x"}),
    )
    paused = pending_of(await h.submit(alice))
    assert paused["pending"]["kind"] == "capability_activation"

    h.model.push(final("understood, not reading anything"))
    body = completed(await h.confirm(alice, paused["task_id"], paused["confirmation_token"], approve=False))

    assert body["response"] == "understood, not reading anything"
    assert await tool_executions(h) == []
    recorded = await actions(h)
    assert AuditAction.AGENT_PROPOSAL_REJECTED.value in recorded
    assert AuditAction.CONFIRMATION_REJECTED.value in recorded
    assert AuditAction.AGENT_TOOL_EXECUTED.value not in recorded
    # Declining created no grant.
    assert await h.rows(CapabilityGrant) == []


# ── CASE D — prohibited request ───────────────────────────────────────────


async def test_case_d_a_floor_capability_is_never_confirmable(make_harness, tmp_path):
    h = await make_harness(config=real_tools(tmp_path), use_real_execution_tools=True)
    alice = await h.user("alice")

    h.model.push(
        ask("secret.read_raw"),
        ask("capability.self_grant"),
        # An "approved" flag the model tries to assert for itself is not even a
        # parseable proposal (extra fields are forbidden).
        say({"type": "tool_call", "tool": "files.write", "operation": "bulk_delete",
             "arguments": {"relative_path": "."}, "approved": True}),
        final("I can't do that"),
    )
    body = completed(await h.submit(alice))

    assert body["response"] == "I can't do that"
    assert await h.rows(ConfirmationToken) == []  # never offered to the human
    assert await h.rows(CapabilityGrant) == []
    assert await tool_executions(h) == []

    # And there is no HTTP path to the floor either (PERM-006).
    resp = await h.client.post(
        f"{API}/capabilities",
        json={"capability": "audit.disable", "scope_type": "user", "scope_id": str(alice.user_id)},
        headers=alice.auth,
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "prohibited"
    assert await h.rows(CapabilityGrant) == []


# ── CASE E — cross-user attempt ───────────────────────────────────────────


async def test_case_e_one_user_can_never_reach_anothers_data_or_actions(make_harness, tmp_path):
    h = await make_harness(config=real_tools(tmp_path), use_real_execution_tools=True)
    alice, bob = await h.user("alice"), await h.user("bob")
    notes = {"sandbox_root": "notes"}

    # Bob stores something private through his own agent.
    await h.grant(bob, "file.write", resource_scope=notes)
    h.model.push(
        ask("file.write", scope=notes),
        call("files.write", "create_file", args={"relative_path": "diary.txt", "content": "bob's secret plans"}),
        call("files.write", "delete_file", args={"relative_path": "diary.txt"}),
    )
    bob_paused = pending_of(await h.submit(bob))

    # Alice's agent, with the same label, lands in *her* sandbox — the root is
    # derived from her server-side identity, never from anything she names.
    await h.grant(alice, "file.read", resource_scope=notes)
    h.model.push(
        ask("file.read", scope=notes),
        call("files.read", "read_file", args={"relative_path": "diary.txt"}),
        call("files.read", "read_file", args={"relative_path": f"../../../{bob.user_id}/private/notes/diary.txt"}),
        final("nothing found"),
    )
    alice_calls_start = len(h.model.seen)
    completed(await h.submit(alice))
    alice_context = "\n".join(m.content for msgs in h.model.seen[alice_calls_start:] for m in msgs)
    assert "bob's secret plans" not in alice_context
    assert "forbidden_path" in alice_context  # the traversal was refused, not "not found"

    # Alice cannot see, confirm, or cancel Bob's task (404, indistinguishable
    # from absent — 04 §7).
    assert (await h.get(alice, bob_paused["task_id"])).status_code == 404
    hijack = await h.confirm(alice, bob_paused["task_id"], bob_paused["confirmation_token"])
    assert hijack.status_code == 404
    cancel = await h.client.post(f"{API}/agent/tasks/{bob_paused['task_id']}/cancel", headers=alice.auth)
    assert cancel.status_code == 404
    assert (sandbox(tmp_path, bob.user_id, "notes") / "diary.txt").exists()

    # Nor grant Bob — or herself on Bob's behalf — anything.
    resp = await h.client.post(
        f"{API}/capabilities",
        json={"capability": "file.read", "scope_type": "user", "scope_id": str(bob.user_id)},
        headers=alice.auth,
    )
    assert resp.status_code == 403


# ── CASE F — execution sandbox violation ──────────────────────────────────


async def test_case_f_the_execution_boundary_blocks_hostile_targets(make_harness, tmp_path):
    # This case measures each boundary on its own, so the circuit breaker's
    # violation trigger (18 §5.1, default 3) is raised above the five probes
    # here. With the default, the same sequence is stopped by the breaker —
    # `tests/runtime/test_circuit_breaker.py` asserts exactly that.
    config = real_tools(
        tmp_path,
        process={"allowed_executables": ["cat"]},
        network={"default_internet": True},
    )
    config["agent"] = {"breaker": {"violation_limit": 10}}
    h = await make_harness(config=config, use_real_execution_tools=True)
    alice = await h.user("alice")
    notes = {"sandbox_root": "notes"}
    await h.grant(alice, "file.read", resource_scope=notes)
    await h.grant(alice, "net.request")

    h.model.push(
        ask("file.read", scope=notes),
        ask("net.request"),
        call("files.read", "read_file", args={"relative_path": "../../../../../etc/passwd"}),
        call("files.read", "read_file", args={"relative_path": "/etc/passwd"}),
        call("net.request", "get", args={"url": "http://169.254.169.254/latest/meta-data/"}),
        call("net.request", "get", args={"url": "http://127.0.0.1:8000/api/v1/health"}),
        call("net.request", "get", args={"url": "file:///etc/passwd"}),
        final("all refused"),
    )
    completed(await h.submit(alice))

    failures = [row.resource for row in await h.rows(AuditEvent)
                if row.action == AuditAction.AGENT_TOOL_FAILED.value]
    assert sum("forbidden_path" in r for r in failures) == 2
    assert sum("egress_denied" in r for r in failures) == 3
    assert "root:" not in h.model.all_text()  # /etc/passwd content never came back
    assert not any(
        row.action == AuditAction.AGENT_TOOL_EXECUTED.value for row in await h.rows(AuditEvent)
    )


@pytest.mark.skipif(not confinement.available(), reason="needs Landlock confinement")
async def test_case_f_an_approved_process_is_still_confined(make_harness, tmp_path):
    """Human approval is not a sandbox exemption: an approved `cat` of a host
    file still cannot read it (server/execution/confinement.py)."""

    h = await make_harness(
        config=real_tools(tmp_path, process={"allowed_executables": ["cat"]}),
        use_real_execution_tools=True,
    )
    alice = await h.user("alice")
    await h.grant(alice, "system.restricted")
    hostname = Path("/etc/hostname").read_text().strip() if Path("/etc/hostname").exists() else None

    h.model.push(
        ask("system.restricted"),
        call("system.shell", "run_shell_command", args={"argv": ["cat", "/etc/hostname"]}),
    )
    paused = pending_of(await h.submit(alice))
    assert paused["pending"]["requires_step_up"] is True
    h.model.push(final("done"))
    completed(await h.confirm(alice, paused["task_id"], paused["confirmation_token"]))

    if hostname:
        observation = [m for m in h.model.seen[-1] if m.role == "user"][-1].content
        assert hostname not in observation


# ── CASE G — malformed / hostile provider behaviour ───────────────────────


async def test_case_g_hostile_model_output_is_rejected_without_execution(make_harness, tmp_path):
    h = await make_harness(config=real_tools(tmp_path), use_real_execution_tools=True)
    alice = await h.user("alice")
    await h.grant(alice, "file.write", resource_scope={"sandbox_root": "notes"})

    h.model.push(
        "rm -rf / --no-preserve-root",  # not a proposal at all
        # authority fields the model is not allowed to set
        say({"type": "tool_call", "tool": "files.write", "operation": "bulk_delete",
             "arguments": {"relative_path": "."}, "risk_category": "low_read"}),
        say({"type": "final_answer", "content": "x", "principal": str(uuid.uuid4())}),
        "{\"type\": \"tool_call\"",  # truncated JSON
    )
    resp = await h.submit(alice)

    assert resp.status_code == 503
    assert failure_of(resp) == "unparseable_proposal"
    assert await tool_executions(h) == []
    assert not (tmp_path / "sandboxes" / "users").exists() or not any(
        (tmp_path / "sandboxes" / "users").rglob("*.txt")
    )


async def test_case_g_a_model_cannot_name_a_tool_or_operation_outside_the_registry(make_harness, tmp_path):
    h = await make_harness(config=real_tools(tmp_path), use_real_execution_tools=True)
    alice = await h.user("alice")
    await h.grant(alice, "file.read", resource_scope={"sandbox_root": "notes"})

    h.model.push(
        ask("file.read", scope={"sandbox_root": "notes"}),
        call("python.exec", "run", args={"code": "import os; os.system('id')"}),
        call("files.read", "chmod", args={"relative_path": "x", "mode": "777"}),
        call("system.shell", "run_shell_command", args={"argv": ["id"]}),  # capability not active
        final("refused"),
    )
    completed(await h.submit(alice))
    assert await tool_executions(h) == []
    assert (await actions(h)).count(AuditAction.AGENT_PROPOSAL_REJECTED.value) == 3


# ── CASE H — secret protection ────────────────────────────────────────────


class _Wire:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        content = self.answers.pop(0) if self.answers else final("wire default")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })


async def test_case_h_a_secret_is_resolved_only_at_the_boundary(make_harness, tmp_path, caplog):
    secret = f"TEST-ONLY-provider-key-{uuid.uuid4().hex}"
    wire = _Wire(
        ask("secret.read_raw"),  # the floor — prohibited
        call("files.read", "read_file", args={"relative_path": "../../../../hypermind.db"}),
        final("done"),
    )
    config = real_tools(tmp_path)
    config.update({
        "agent": {"provider": "openai_compatible", "model": "gpt-test", "endpoint": "https://llm.test/v1",
                  "pricing": {"input_per_1k_tokens": 0.1, "output_per_1k_tokens": 0.1},
                  "bounds": {"per_task_budget": 5.0}},
        "security": {"budgets": {"per_user_daily_cost_limit": 50.0, "global_daily_cost_limit": 500.0}},
    })
    h = await make_harness(config=config, transport=httpx.MockTransport(wire.handler),
                           use_real_execution_tools=True)
    async with h.storage.session() as s:
        handle = await h.core.secret_store.set(
            s, owner_scope_type=SecretOwnerScopeType.SERVER, owner_scope_id=None,
            secret_class=SecretClass.MODEL_API_KEY, value=secret,
            requester=SecretRequester.server(), audit=AuditLogger(s, request_id=uuid.uuid4()),
        )
        await s.commit()
    h.config.agent.secret_ref = f"secretstore:{handle}"
    alice = await h.user("alice")
    await h.grant(alice, "file.read", resource_scope={"sandbox_root": "notes"})

    with caplog.at_level(logging.DEBUG):
        resp = await h.submit(alice)

    assert resp.status_code == 200, resp.text
    # On the wire, in the Authorization header of the provider call — and only there.
    assert wire.requests and all(r.headers["authorization"] == f"Bearer {secret}" for r in wire.requests)
    assert all(secret not in r.content.decode() for r in wire.requests)  # never in model context
    assert secret not in resp.text
    assert secret not in caplog.text
    persisted = []
    for model in (AuditEvent, UsageEvent, PermissionDecision, AgentTask, IdempotencyKey):
        for row in await h.rows(model):
            persisted.append(json.dumps({c.name: str(getattr(row, c.name)) for c in model.__table__.columns}))
    assert secret not in "\n".join(persisted)
    assert await tool_executions(h) == [] or all(
        "forbidden_path" in r.resource
        for r in await h.rows(AuditEvent) if r.action == AuditAction.AGENT_TOOL_FAILED.value
    )


# ── CASE I — cancellation ─────────────────────────────────────────────────


def _processes_with(marker: str) -> list[int]:
    found = []
    for entry in Path("/proc").iterdir() if Path("/proc").exists() else []:
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if marker.encode() in cmdline:
            found.append(int(entry.name))
    return found


@pytest.mark.skipif(not Path("/proc").exists(), reason="needs /proc to observe the child process")
@requires_landlock
async def test_case_i_cancelling_a_task_kills_its_running_process(make_harness, tmp_path):
    marker = "47.125"  # a sleep duration no other process uses
    h = await make_harness(
        config=real_tools(tmp_path, process={"allowed_executables": ["sleep"], "max_timeout_seconds": 60}),
        use_real_execution_tools=True,
    )
    alice = await h.user("alice")
    await h.grant(alice, "system.restricted")

    h.model.push(
        ask("system.restricted"),
        call("system.shell", "run_shell_command", args={"argv": ["sleep", marker], "timeout": 55}),
    )
    paused = pending_of(await h.submit(alice))
    task_id = paused["task_id"]
    h.model.push(final("should not be reached"))

    started = time.monotonic()
    confirming = asyncio.create_task(h.confirm(alice, task_id, paused["confirmation_token"]))
    for _ in range(100):
        if _processes_with(marker):
            break
        await asyncio.sleep(0.05)
    assert _processes_with(marker), "the approved process never started"

    cancel = await h.client.post(f"{API}/agent/tasks/{task_id}/cancel", headers=alice.auth)
    assert cancel.status_code == 200, cancel.text

    finished = await asyncio.wait_for(confirming, timeout=15)
    assert finished.status_code == 200, finished.text
    assert finished.json()["status"] == "cancelled"
    assert time.monotonic() - started < 15  # not the 47s the process asked for

    for _ in range(40):
        if not _processes_with(marker):
            break
        await asyncio.sleep(0.05)
    assert _processes_with(marker) == [], "an orphan process outlived its cancelled task"

    # The task's temp root — the only place the process could write — is gone.
    assert not (tmp_path / "sandboxes" / "tasks" / task_id).exists()
    failures = [r.resource for r in await h.rows(AuditEvent) if r.action == AuditAction.AGENT_TOOL_FAILED.value]
    assert any(r.endswith(":cancelled") for r in failures)
    assert AuditAction.AGENT_TASK_CANCELLED.value in await actions(h)


async def test_case_i_cancelling_a_paused_task_drops_the_action(make_harness, tmp_path):
    h = await make_harness(config=real_tools(tmp_path), use_real_execution_tools=True)
    alice = await h.user("alice")
    notes = {"sandbox_root": "notes"}
    await h.grant(alice, "file.write", resource_scope=notes)
    h.model.push(
        ask("file.write", scope=notes),
        call("files.write", "create_file", args={"relative_path": "keep.txt", "content": "x"}),
        call("files.write", "delete_file", args={"relative_path": "keep.txt"}),
    )
    paused = pending_of(await h.submit(alice))

    cancel = await h.client.post(f"{API}/agent/tasks/{paused['task_id']}/cancel", headers=alice.auth)
    assert cancel.json()["status"] == "cancelled"
    late = await h.confirm(alice, paused["task_id"], paused["confirmation_token"])
    assert late.status_code == 409
    assert (sandbox(tmp_path, alice.user_id, "notes") / "keep.txt").exists()


# ── CASE J — provider outage ──────────────────────────────────────────────


async def test_case_j_a_provider_outage_fails_deterministically(make_harness, tmp_path):
    h = await make_harness(config=real_tools(tmp_path), use_real_execution_tools=True)
    alice = await h.user("alice")
    h.model.push(ModelUnavailable("connection refused"))

    resp = await h.submit(alice)

    assert resp.status_code == 503
    assert resp.json()["error"]["details"]["dependency"] == "model"
    assert failure_of(resp) == "model_unavailable"
    assert await tool_executions(h) == []
    task = (await h.rows(AgentTask))[0]
    assert task.status == "failed" and task.response is None  # never a fabricated answer


async def test_case_j_a_failing_fallback_is_not_an_unsafe_fallback(make_harness, tmp_path):
    from tests.runtime.conftest import ScriptedModel

    backup = ScriptedModel("scripted-fallback")
    config = real_tools(tmp_path)
    config["agent"] = {"fallback": {"provider": "ollama", "model": "scripted-fallback"}}
    h = await make_harness(config=config, models={"scripted-fallback": backup},
                           use_real_execution_tools=True)
    alice = await h.user("alice")
    h.model.push(ModelUnavailable("primary down"))
    backup.push(ModelUnavailable("fallback down too"))

    resp = await h.submit(alice)

    assert resp.status_code == 503
    assert failure_of(resp) == "model_unavailable"
    assert await tool_executions(h) == []
