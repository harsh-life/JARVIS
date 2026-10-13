"""OD-PILOT-5 — the pilot is invite-only (register §2N, owner confirmation
2026-10-13).

A login whose *verified* email is not on `security.oidc.allowed_emails` and
not in `security.oidc.allowed_domains` is refused with `403` — after every
token check, before any user row exists (AUTH-T1: no mutation on a failed
login) — and the refusal is audited. Driven through the real HTTP surface with
genuine RS256 id_tokens, like the AUTH-T tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from server.auth.login import Admission
from server.gateway.app import API_V1_PREFIX, create_app
from server.gateway.security import build_security_core
from server.secrets.kek import resolve_kek
from server.security.events import AuditAction
from server.storage.models import AuditEvent, User
from tests.support import OMIT, TEST_CLIENT_ID, TEST_ISSUER, TEST_KEK_ENV_VAR, LocalOIDCProvider, make_test_config


class _Client:
    def __init__(self, client: httpx.AsyncClient, provider: LocalOIDCProvider, storage) -> None:
        self.client, self.provider, self.storage = client, provider, storage

    async def login(self, *, email: str | None, verified: object = True) -> httpx.Response:
        self.provider.email = email
        self.provider.claim_overrides = {"email_verified": verified}
        start = await self.client.get(f"{API_V1_PREFIX}/auth/oidc/start")
        state = parse_qs(urlparse(start.json()["redirect_url"]).query)["state"][0]
        return await self.client.get(f"{API_V1_PREFIX}/auth/oidc/callback", params={"code": "c", "state": state})

    async def users(self) -> list[User]:
        async with self.storage.session() as session:
            return list((await session.execute(select(User))).scalars().all())

    async def rejections(self) -> list[str]:
        async with self.storage.session() as session:
            rows = (await session.execute(select(AuditEvent))).scalars().all()
        return [e.resource for e in rows if e.action == AuditAction.OIDC_LOGIN_REJECTED.value]


@pytest_asyncio.fixture
async def invite_only(storage, kek_value: str) -> AsyncIterator[_Client]:
    provider = LocalOIDCProvider()
    config = make_test_config(
        security={
            "oidc": {
                "client_id": TEST_CLIENT_ID,
                "issuer": TEST_ISSUER,
                "allowed_emails": ["Alice@Example.test"],
                "allowed_domains": ["team.example.test"],
            }
        }
    )
    core = build_security_core(config, oidc_provider=provider)
    async with storage.session() as session:
        await core.secret_store.bootstrap(session, resolve_kek(f"env:{TEST_KEK_ENV_VAR}"))
        await session.commit()
    app = create_app(storage=storage, security=core)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield _Client(client, provider, storage)


@pytest.mark.asyncio
async def test_an_allow_listed_email_signs_in(invite_only):
    resp = await invite_only.login(email="alice@example.test")
    assert resp.status_code == 200, resp.text
    assert len(await invite_only.users()) == 1


@pytest.mark.asyncio
async def test_an_allow_listed_domain_signs_in_but_not_its_subdomains(invite_only):
    assert (await invite_only.login(email="bob@team.example.test")).status_code == 200
    resp = await invite_only.login(email="carol@eu.team.example.test")
    assert resp.status_code == 403
    assert len(await invite_only.users()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("email", "verified"),
    [
        ("mallory@example.test", True),          # not listed
        ("alice@example.test.evil", True),       # suffix, not the address
        ("alice@example.test", False),           # listed, but not verified by the provider
        ("alice@example.test", OMIT),            # no verification claim at all
        ("alice@example.test", "yes"),           # a claim that is not `true`
        (None, True),                            # no email at all
    ],
)
async def test_anyone_else_is_refused_with_403_and_no_user_row(invite_only, email, verified):
    resp = await invite_only.login(email=email, verified=verified)
    assert resp.status_code == 403, resp.text
    body = resp.json()["error"]
    assert body["code"] == "unauthorized"
    assert "allow" not in body["message"].lower()        # the list is never named
    assert await invite_only.users() == []                # AUTH-T1: no mutation
    assert await invite_only.rejections() == ["auth:oidc:not_admitted"]


@pytest.mark.asyncio
async def test_without_a_list_anyone_who_completes_oidc_signs_in(api):
    # The development default (both lists empty) is unchanged.
    start = await api.client.get(f"{API_V1_PREFIX}/auth/oidc/start")
    state = parse_qs(urlparse(start.json()["redirect_url"]).query)["state"][0]
    resp = await api.client.get(f"{API_V1_PREFIX}/auth/oidc/callback", params={"code": "c", "state": state})
    assert resp.status_code == 200


def test_matching_is_exact_and_case_insensitive():
    admission = Admission(emails=frozenset({"alice@example.test"}), domains=frozenset({"team.example.test"}))
    assert admission.admits("ALICE@Example.TEST", verified=True)
    assert admission.admits("x@TEAM.example.test", verified=True)
    assert not admission.admits("x@sub.team.example.test", verified=True)
    assert not admission.admits("alice@example.test", verified=False)
    assert not admission.admits("", verified=True)
    assert Admission().admits(None, verified=False)   # unrestricted


@pytest.mark.parametrize("bad", [["not-an-email"], ["a@b"], ["@example.test"]])
def test_a_malformed_allow_list_is_a_config_error(bad):
    with pytest.raises(Exception):
        make_test_config(security={"oidc": {"client_id": "c", "issuer": "i", "allowed_emails": bad}})
    with pytest.raises(Exception):
        make_test_config(security={"oidc": {"client_id": "c", "issuer": "i", "allowed_domains": ["*.example.test"]}})
