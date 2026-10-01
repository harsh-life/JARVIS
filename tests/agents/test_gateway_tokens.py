"""docs/29 §11.3 — run tokens, nonces and the Agent Gateway's request check
(Phase 3, slice 3A; AGENT-T6, T7, T8, T23 and the replay rules).

The Agent Gateway is the one place an agent run's requests are authenticated:
in-process for the native runtime now, behind a network surface for an
external runtime later (Phase 6), with the same code. What must hold:

* a run gets two tokens — one per gateway — stored only as SHA-256 hashes;
* a token is bound to exactly one run, one agent, one spec hash and one
  purpose, and is useless for anything else (AGENT-T6);
* an expired token is refused (AGENT-T7); a revoked one is refused on the very
  next request — nothing is cached (AGENT-T8);
* a run that finished, an agent that was paused, deleted or changed, refuses;
* every request carries a fresh `sent_at` and a nonce: a stale request is
  refused, a nonce reused for a different request is a replay, and a retry of
  the same request is answered from the stored response, never executed twice.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from server.agents.gateway import tokens as run_tokens
from server.agents.gateway.core import AgentGateway, GatewayAdmitted, GatewayContext, GatewayDenied, GatewayReplayed
from server.agents.service import AgentDefinitionService
from server.storage.models import AgentDefinitionRow, AgentGatewayNonceRow, AgentRunTokenRow, Graph
from shared.schemas.agent import AgentTaskStatus
from shared.schemas.agent_factory import AgentGatewayErrorCode, AgentGatewayRequest, AgentRunContext, RunTokenPurpose
from shared.schemas.enums import GraphType
from tests.agents.conftest import make_user
from tests.agents.support import registries
from tests.agents.test_service import Clock, compile_, create, draft


MODEL, TOOL = RunTokenPurpose.MODEL, RunTokenPurpose.TOOL
DIGEST_A, DIGEST_B = "a" * 64, "b" * 64
_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(clock):
    return AgentDefinitionService(registries(), preview_ttl_minutes=15, clock=clock)


@pytest.fixture
def gateway(service, clock):
    return AgentGateway(service, clock=clock)


async def member(graph_id, user_id) -> bool:
    return True


class Run:
    """One agent, one open run of it, and its two plaintext tokens."""

    def __init__(self, definition, spec, run, issued):
        self.definition, self.spec, self.run, self.issued = definition, spec, run, issued

    @property
    def agent_id(self):
        return self.definition.agent_id

    @property
    def run_id(self):
        return self.run.run_id


async def open_run(storage, service, gateway, clock, *, user_id=None, definition=None, spec=None) -> Run:
    async with storage.session() as session:
        if definition is None:
            user_id = user_id or await make_user(storage)
            definition, spec = await create(service, session, user_id)
        run = await service.create_run(session, spec=spec, run_id=uuid.uuid4())
        issued = await gateway.issue(session, run, deadline=clock.now + timedelta(minutes=5))
        await session.commit()
    return Run(definition, spec, run, issued)


def request(r: Run, purpose: RunTokenPurpose = TOOL, *, token: str | None = None, nonce: str | None = None,
            digest: str = DIGEST_A, sent_at=None, run_id=None, agent_id=None, clock=None) -> AgentGatewayRequest:
    return AgentGatewayRequest(
        run_id=run_id or r.run_id, agent_id=agent_id or r.agent_id, purpose=purpose,
        run_token=token if token is not None else (r.issued.tool if purpose is TOOL else r.issued.model),
        request_nonce=nonce or run_tokens.new_nonce(), sent_at=sent_at or clock.now, request_digest=digest,
    )


async def admit(storage, gateway, req):
    async with storage.session() as session:
        outcome = await gateway.admit(session, req, is_member=member)
        await session.commit()
    return outcome


async def authenticate(storage, gateway, r: Run, purpose=TOOL, *, token=None, run_id=None, agent_id=None):
    async with storage.session() as session:
        return await gateway.authenticate(
            session, run_id=run_id or r.run_id, agent_id=agent_id or r.agent_id, purpose=purpose,
            token=token if token is not None else (r.issued.tool if purpose is TOOL else r.issued.model),
            is_member=member)


def denied(outcome, code: AgentGatewayErrorCode, detail: str | None = None) -> None:
    assert isinstance(outcome, GatewayDenied), outcome
    assert outcome.code is code, outcome
    if detail is not None:
        assert outcome.detail == detail, outcome


async def token_rows(storage, run_id=None) -> list[AgentRunTokenRow]:
    async with storage.session() as session:
        query = select(AgentRunTokenRow)
        if run_id is not None:
            query = query.where(AgentRunTokenRow.run_id == run_id)
        return list((await session.execute(query)).scalars())


# ── issuing ─────────────────────────────────────────────────────────────


async def test_a_run_gets_two_tokens_stored_only_as_hashes(storage, service, gateway, clock):
    r = await open_run(storage, service, gateway, clock)
    model, tool = r.issued.model, r.issued.tool
    assert model != tool
    for token in (model, tool):
        assert len(token) == 43 and _B64URL.fullmatch(token)
    rows = await token_rows(storage, r.run_id)
    assert {row.purpose for row in rows} == {"model", "tool"}
    by_purpose = {row.purpose: row for row in rows}
    assert by_purpose["model"].token_hash == hashlib.sha256(model.encode()).hexdigest()
    assert by_purpose["tool"].token_hash == hashlib.sha256(tool.encode()).hexdigest()
    for row in rows:
        assert (row.run_id, row.agent_id, row.spec_hash) == (r.run_id, r.agent_id, r.spec.spec_hash)
        assert row.revoked_at is None
        expected = clock.now + timedelta(minutes=5, seconds=30)
        assert row.expires_at.replace(tzinfo=None) == expected.replace(tzinfo=None)
        # The plaintext is nowhere at rest.
        assert model not in repr(vars(row)) and tool not in repr(vars(row))
    assert model not in repr(r.issued) and tool not in repr(r.issued)


async def test_a_valid_token_authenticates_exactly_its_own_run(storage, service, gateway, clock):
    r = await open_run(storage, service, gateway, clock)
    for purpose in (MODEL, TOOL):
        ctx = await authenticate(storage, gateway, r, purpose)
        assert isinstance(ctx, GatewayContext), ctx
        assert (ctx.run_id, ctx.agent_id, ctx.spec_hash, ctx.version) == (r.run_id, r.agent_id, r.spec.spec_hash, 1)
        assert ctx.owner_user_id == r.definition.owner_user_id and ctx.purpose is purpose


# ── AGENT-T6: bound to one run, agent, spec hash and purpose ─────────────


async def test_agent_t6_a_token_for_the_other_gateway_is_invalid(storage, service, gateway, clock):
    r = await open_run(storage, service, gateway, clock)
    denied(await authenticate(storage, gateway, r, MODEL, token=r.issued.tool),
           AgentGatewayErrorCode.INVALID_RUN_TOKEN, "purpose")
    denied(await authenticate(storage, gateway, r, TOOL, token=r.issued.model),
           AgentGatewayErrorCode.INVALID_RUN_TOKEN, "purpose")


async def test_agent_t6_a_token_is_useless_for_another_run_of_the_same_agent(storage, service, gateway, clock):
    first = await open_run(storage, service, gateway, clock)
    second = await open_run(storage, service, gateway, clock, definition=first.definition, spec=first.spec)
    denied(await authenticate(storage, gateway, first, token=first.issued.tool, run_id=second.run_id),
           AgentGatewayErrorCode.INVALID_RUN_TOKEN, "binding")
    denied(await admit(storage, gateway, request(first, run_id=second.run_id, clock=clock)),
           AgentGatewayErrorCode.INVALID_RUN_TOKEN, "binding")


async def test_agent_t6_a_token_is_useless_for_another_agent(storage, service, gateway, clock):
    mine = await open_run(storage, service, gateway, clock)
    other = await open_run(storage, service, gateway, clock, user_id=mine.definition.owner_user_id)
    denied(await authenticate(storage, gateway, mine, agent_id=other.agent_id),
           AgentGatewayErrorCode.INVALID_RUN_TOKEN, "binding")
    # Nor does the other run's token reach this run.
    denied(await authenticate(storage, gateway, mine, token=other.issued.tool),
           AgentGatewayErrorCode.INVALID_RUN_TOKEN, "binding")


async def test_agent_t6_a_token_is_bound_to_the_spec_hash_of_its_run(storage, service, gateway, clock):
    r = await open_run(storage, service, gateway, clock)
    async with storage.session() as session:
        await session.execute(update(AgentRunTokenRow).where(AgentRunTokenRow.run_id == r.run_id)
                              .values(spec_hash="0" * 64))
        await session.commit()
    denied(await authenticate(storage, gateway, r), AgentGatewayErrorCode.INVALID_RUN_TOKEN, "binding")
    denied(await admit(storage, gateway, request(r, clock=clock)), AgentGatewayErrorCode.INVALID_RUN_TOKEN, "binding")


@pytest.mark.parametrize("token", ["", "short", "x" * 43, "!" * 43, None])
async def test_unknown_or_malformed_tokens_are_refused(storage, service, gateway, clock, token):
    r = await open_run(storage, service, gateway, clock)
    presented = run_tokens.new_token() if token is None else token
    denied(await authenticate(storage, gateway, r, token=presented), AgentGatewayErrorCode.INVALID_RUN_TOKEN)
    denied(await admit(storage, gateway, request(r, token=presented, clock=clock)),
           AgentGatewayErrorCode.INVALID_RUN_TOKEN)


# ── AGENT-T7 / AGENT-T8: expiry and revocation, no cache ─────────────────


async def test_agent_t7_an_expired_token_is_refused(storage, service, gateway, clock):
    r = await open_run(storage, service, gateway, clock)
    clock.now = clock.now + timedelta(minutes=5, seconds=29)
    assert isinstance(await authenticate(storage, gateway, r), GatewayContext)
    clock.now = clock.now + timedelta(seconds=1)
    denied(await authenticate(storage, gateway, r), AgentGatewayErrorCode.INVALID_RUN_TOKEN, "expired")
    denied(await admit(storage, gateway, request(r, clock=clock)), AgentGatewayErrorCode.INVALID_RUN_TOKEN, "expired")


async def test_agent_t8_revocation_applies_to_the_very_next_request(storage, service, gateway, clock):
    r = await open_run(storage, service, gateway, clock)
    async with storage.session() as session:
        ctx = await gateway.authenticate(session, run_id=r.run_id, agent_id=r.agent_id, purpose=TOOL,
                                         token=r.issued.tool, is_member=member)
        assert isinstance(ctx, GatewayContext)
        # Revoked from another request; the same session asks again.
        async with storage.session() as other:
            assert await gateway.revoke_run(other, r.run_id, "owner_stop") == 2
            await other.commit()
        again = await gateway.authenticate(session, run_id=r.run_id, agent_id=r.agent_id, purpose=TOOL,
                                           token=r.issued.tool, is_member=member)
        denied(again, AgentGatewayErrorCode.INVALID_RUN_TOKEN, "revoked")
    denied(await admit(storage, gateway, request(r, MODEL, clock=clock)),
           AgentGatewayErrorCode.INVALID_RUN_TOKEN, "revoked")
    rows = await token_rows(storage, r.run_id)
    assert all(row.revoked_at is not None and row.revoked_reason == "owner_stop" for row in rows)


async def test_a_token_revoked_at_rest_is_refused(storage, service, gateway, clock):
    """Revocation is a column, read fresh on every request — whoever set it."""

    r = await open_run(storage, service, gateway, clock)
    async with storage.session() as session:
        await session.execute(update(AgentRunTokenRow).where(AgentRunTokenRow.purpose == "tool")
                              .values(revoked_at=clock.now, revoked_reason="breaker"))
        await session.commit()
    denied(await authenticate(storage, gateway, r), AgentGatewayErrorCode.INVALID_RUN_TOKEN, "revoked")
    assert isinstance(await authenticate(storage, gateway, r, MODEL), GatewayContext)


async def test_revoking_an_agent_revokes_every_live_run(storage, service, gateway, clock):
    first = await open_run(storage, service, gateway, clock)
    second = await open_run(storage, service, gateway, clock, definition=first.definition, spec=first.spec)
    async with storage.session() as session:
        assert await gateway.revoke_agent(session, first.agent_id, "paused") == 4
        await session.commit()
    for r in (first, second):
        denied(await authenticate(storage, gateway, r), AgentGatewayErrorCode.INVALID_RUN_TOKEN, "revoked")


# ── the run and the definition, read live ────────────────────────────────


async def test_a_finished_run_refuses(storage, service, gateway, clock):
    r = await open_run(storage, service, gateway, clock)
    async with storage.session() as session:
        await service.run_finished(session, r.run_id, status=AgentTaskStatus.COMPLETED, failure=None, cost=0.0)
        # Whatever else happened, a finished run's tokens are revoked with it.
        await session.execute(update(AgentRunTokenRow).values(revoked_at=None, revoked_reason=None))
        await session.commit()
    denied(await authenticate(storage, gateway, r), AgentGatewayErrorCode.RUN_NOT_RUNNING)


@pytest.mark.parametrize("status", ["paused", "deleted", "revoked", "needs_reapproval"])
async def test_an_agent_that_is_no_longer_active_refuses(storage, service, gateway, clock, status):
    r = await open_run(storage, service, gateway, clock)
    async with storage.session() as session:
        await session.execute(update(AgentDefinitionRow).values(status=status))
        await session.commit()
    denied(await authenticate(storage, gateway, r), AgentGatewayErrorCode.AGENT_UNAVAILABLE)
    denied(await admit(storage, gateway, request(r, clock=clock)), AgentGatewayErrorCode.AGENT_UNAVAILABLE)


async def test_a_newer_spec_version_refuses_the_older_runs_tokens(storage, service, gateway, clock):
    r = await open_run(storage, service, gateway, clock)
    async with storage.session() as session:
        definition = await session.get(AgentDefinitionRow, r.agent_id)
        outcome = await compile_(service, session, definition.owner_user_id, agent=definition,
                                 d=draft(name="Advisory digest v2"))
        preview = await service.load_preview(session, compile_id=outcome.compile_id,
                                             owner_user_id=definition.owner_user_id, task_id=None,
                                             for_agent=definition.agent_id)
        await service.update_from_preview(session, definition, preview)
        await session.commit()
    denied(await authenticate(storage, gateway, r), AgentGatewayErrorCode.SPEC_CHANGED)
    # The update revoked them as well.
    assert all(row.revoked_reason == "spec_changed" for row in await token_rows(storage, r.run_id))


async def test_the_owner_leaving_the_graph_refuses(storage, service, gateway, clock):
    r = await open_run(storage, service, gateway, clock)
    graph_id = uuid.uuid4()
    async with storage.session() as session:
        session.add(Graph(graph_id=graph_id, name="g", type=GraphType.SHARED,
                          owner_user_id=r.definition.owner_user_id, created_at=clock.now))
        await session.flush()
        await session.execute(update(AgentDefinitionRow).values(graph_id=graph_id))
        await session.commit()

    async def left(graph, user):
        return False

    async with storage.session() as session:
        outcome = await gateway.authenticate(session, run_id=r.run_id, agent_id=r.agent_id, purpose=TOOL,
                                             token=r.issued.tool, is_member=left)
    denied(outcome, AgentGatewayErrorCode.AGENT_UNAVAILABLE)


# ── freshness, nonces, replay and idempotency ────────────────────────────


@pytest.mark.parametrize("offset", [-61, 61, -3600])
async def test_a_stale_request_is_refused(storage, service, gateway, clock, offset):
    r = await open_run(storage, service, gateway, clock)
    denied(await admit(storage, gateway, request(r, sent_at=clock.now + timedelta(seconds=offset), clock=clock)),
           AgentGatewayErrorCode.STALE_REQUEST)


@pytest.mark.parametrize("offset", [-60, 0, 59])
async def test_a_fresh_request_is_admitted(storage, service, gateway, clock, offset):
    r = await open_run(storage, service, gateway, clock)
    outcome = await admit(storage, gateway, request(r, sent_at=clock.now + timedelta(seconds=offset), clock=clock))
    assert isinstance(outcome, GatewayAdmitted), outcome


@pytest.mark.parametrize("nonce", ["short", "x" * 21, "has spaces in it......", "é" * 30, "y" * 65])
async def test_a_malformed_nonce_is_refused(storage, service, gateway, clock, nonce):
    r = await open_run(storage, service, gateway, clock)
    denied(await admit(storage, gateway, request(r, nonce=nonce, clock=clock)), AgentGatewayErrorCode.SCHEMA_INVALID)


async def test_a_nonce_reused_for_a_different_request_is_a_replay(storage, service, gateway, clock):
    r = await open_run(storage, service, gateway, clock)
    nonce = run_tokens.new_nonce()
    first = await admit(storage, gateway, request(r, nonce=nonce, digest=DIGEST_A, clock=clock))
    assert isinstance(first, GatewayAdmitted)
    async with storage.session() as session:
        await gateway.settle(session, first, {"observation": "ok"})
        await session.commit()
    denied(await admit(storage, gateway, request(r, nonce=nonce, digest=DIGEST_B, clock=clock)),
           AgentGatewayErrorCode.REPLAY)


async def test_a_retried_request_is_answered_from_the_ledger_and_runs_nothing(storage, service, gateway, clock):
    r = await open_run(storage, service, gateway, clock)
    nonce = run_tokens.new_nonce()
    first = await admit(storage, gateway, request(r, nonce=nonce, clock=clock))
    assert isinstance(first, GatewayAdmitted)
    async with storage.session() as session:
        await gateway.settle(session, first, {"observation": "three reports"})
        await session.commit()
    retry = await admit(storage, gateway, request(r, nonce=nonce, clock=clock))
    assert isinstance(retry, GatewayReplayed), retry
    assert retry.response == {"observation": "three reports"}
    async with storage.session() as session:
        assert len(list((await session.execute(select(AgentGatewayNonceRow))).scalars())) == 1


async def test_a_duplicate_of_a_request_still_in_flight_is_refused(storage, service, gateway, clock):
    r = await open_run(storage, service, gateway, clock)
    nonce = run_tokens.new_nonce()
    assert isinstance(await admit(storage, gateway, request(r, nonce=nonce, clock=clock)), GatewayAdmitted)
    denied(await admit(storage, gateway, request(r, nonce=nonce, clock=clock)), AgentGatewayErrorCode.REPLAY)


async def test_nonces_are_per_token(storage, service, gateway, clock):
    r = await open_run(storage, service, gateway, clock)
    nonce = run_tokens.new_nonce()
    assert isinstance(await admit(storage, gateway, request(r, TOOL, nonce=nonce, clock=clock)), GatewayAdmitted)
    assert isinstance(await admit(storage, gateway, request(r, MODEL, nonce=nonce, clock=clock)), GatewayAdmitted)


async def test_a_revoked_token_cannot_fetch_a_stored_response(storage, service, gateway, clock):
    r = await open_run(storage, service, gateway, clock)
    nonce = run_tokens.new_nonce()
    first = await admit(storage, gateway, request(r, nonce=nonce, clock=clock))
    async with storage.session() as session:
        await gateway.settle(session, first, {"observation": "x"})
        await gateway.revoke_run(session, r.run_id, "finished")
        await session.commit()
    denied(await admit(storage, gateway, request(r, nonce=nonce, clock=clock)),
           AgentGatewayErrorCode.INVALID_RUN_TOKEN, "revoked")


async def test_tokens_and_nonces_are_pruned_a_day_after_expiry(storage, service, gateway, clock):
    r = await open_run(storage, service, gateway, clock)
    await admit(storage, gateway, request(r, clock=clock))
    clock.now = clock.now + timedelta(hours=24, minutes=5)
    async with storage.session() as session:
        assert await gateway.prune(session) == 0          # not yet a day past expiry
        clock.now = clock.now + timedelta(minutes=1)
        assert await gateway.prune(session) == 2
        await session.commit()
    assert await token_rows(storage) == []
    async with storage.session() as session:
        assert list((await session.execute(select(AgentGatewayNonceRow))).scalars()) == []


# ── the service revokes on every lifecycle change ────────────────────────


async def test_finishing_cancelling_pausing_or_deleting_revokes(storage, service, gateway, clock):
    finished = await open_run(storage, service, gateway, clock)
    cancelled = await open_run(storage, service, gateway, clock, definition=finished.definition, spec=finished.spec)
    async with storage.session() as session:
        await service.run_finished(session, finished.run_id, status=AgentTaskStatus.COMPLETED, failure=None, cost=0.0)
        await service.run_cancelled(session, cancelled.run_id, reason="owner_stop")
        await session.commit()
    assert all(row.revoked_at is not None for row in await token_rows(storage))

    paused = await open_run(storage, service, gateway, clock, definition=finished.definition, spec=finished.spec)
    async with storage.session() as session:
        await service.pause(session, await session.get(AgentDefinitionRow, paused.agent_id))
        await session.commit()
    assert all(row.revoked_reason == "paused" for row in await token_rows(storage, paused.run_id))

    deleted = await open_run(storage, service, gateway, clock)
    async with storage.session() as session:
        await service.delete(session, await session.get(AgentDefinitionRow, deleted.agent_id))
        await session.commit()
    assert all(row.revoked_at is not None for row in await token_rows(storage, deleted.run_id))


# ── tokens never travel in the clear ─────────────────────────────────────


def test_tokens_never_appear_in_a_repr():
    token, model = run_tokens.new_token(), run_tokens.new_token()
    from datetime import datetime, timezone

    ctx = AgentRunContext(run_id=uuid.uuid4(), agent_id=uuid.uuid4(), version=1, spec_hash="0" * 64,
                          input_text="x", deadline=datetime.now(timezone.utc), run_token=token,
                          model_run_token=model)
    req = AgentGatewayRequest(run_id=uuid.uuid4(), agent_id=uuid.uuid4(), purpose=TOOL, run_token=token,
                              request_nonce=run_tokens.new_nonce(), sent_at=datetime.now(timezone.utc),
                              request_digest=DIGEST_A)
    for text in (repr(ctx), str(ctx), repr(req), str(req)):
        assert token not in text and model not in text


def test_a_nonce_carries_at_least_128_bits():
    nonce = run_tokens.new_nonce()
    assert len(nonce) >= 22 and _B64URL.fullmatch(nonce)
    assert len({run_tokens.new_nonce() for _ in range(1000)}) == 1000
