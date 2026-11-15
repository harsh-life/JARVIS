"""Google OIDC as a confidential Web application client (`security.oidc.client_secret_ref`).

Google's token endpoint authenticates a Web application client by its secret,
PKCE notwithstanding. What is pinned here:

* **Config holds a reference, never a value.** A literal is a load-time failure.
* **Resolution happens inside server/auth**, per login: `env:NAME`, or a
  `secretstore:` handle that must be a server-owned `oauth_token`.
* **The secret goes to one place:** the body of the token request to Google's
  fixed endpoint — never the authorization URL, a redirect, a response, the
  database, a log line or an audit row.
* **A missing secret fails closed** before any request leaves the server
  (`503 dependency_unavailable`, generic), and a misconfigured reference stops
  startup.
* **Nothing else weakened:** PKCE S256, the stored redirect_uri, issuer /
  audience / nonce / signature / expiry checks, the allow-list, and the desktop
  handoff's state + challenge + single-use redemption all still hold.

Google is mocked (`FakeGoogle`, an `httpx.MockTransport` that checks what the
real endpoint checks) and driven through the **production**
`GoogleOIDCProvider`. Every secret is generated per test; none is real.
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio
import yaml
from pydantic import ValidationError
from sqlalchemy import select, update

from server.auth.client_secret import ClientSecretConfigError, ClientSecretSource
from server.auth.desktop_handoff import s256_challenge
from server.auth.errors import InvalidIdToken, OIDCClientSecretUnavailable
from server.auth.oidc import GOOGLE_JWKS_URI, GOOGLE_TOKEN_ENDPOINT, GoogleOIDCProvider, code_challenge_for
from server.config.schema import AppConfig, DesktopConfig
from server.gateway.app import API_V1_PREFIX, create_app
from server.gateway.security import OIDC_CALLBACK_PATH, build_security_core, verify_oidc_client_secret
from server.secrets.kek import resolve_kek
from server.secrets.requester import SecretRequester
from server.secrets.store import EncryptedLocalSecretStore
from server.security.audit import AuditLogger
from server.security.events import AuditAction
from server.storage.models import AuditEvent, OIDCLoginState, SecretReference, User
from shared.schemas.enums import SecretClass, SecretOwnerScopeType
from tests.security_core.helpers import database_contains
from tests.support import TEST_CLIENT_ID, TEST_ISSUER, TEST_KEK_ENV_VAR, LocalOIDCProvider, make_test_config

V1 = API_V1_PREFIX
SECRET_ENV = "JARVIS_TEST_OIDC_CLIENT_SECRET"
PORT = 53682


def new_secret() -> str:
    """A fresh, fake client secret. Never a literal in this file."""

    return "TEST-ONLY-" + secrets.token_urlsafe(24)


def oidc_config(client_secret_ref: str | None = None, **extra) -> AppConfig:
    oidc = {"client_id": TEST_CLIENT_ID, "issuer": TEST_ISSUER, **extra}
    if client_secret_ref is not None:
        oidc["client_secret_ref"] = client_secret_ref
    return make_test_config(security={"oidc": oidc})


# ── a mocked Google ─────────────────────────────────────────────────────────


class FakeGoogle:
    """Google's token and JWKS endpoints. The token endpoint checks what the
    real one does: the client id, the client secret (missing → invalid_request,
    wrong → invalid_client), the authorization code, the exact redirect_uri,
    and PKCE S256 — then returns an id_token minted by `signer`."""

    def __init__(self, *, expected_secret: str | None, signer: LocalOIDCProvider | None = None) -> None:
        self.expected_secret = expected_secret
        self.signer = signer or LocalOIDCProvider()
        self.token_requests: list[dict[str, str]] = []
        self.token_request_urls: list[str] = []
        self._codes: dict[str, dict[str, str]] = {}

    def authorize(self, redirect_url: str) -> tuple[str, str]:
        """The user consents: returns (code, state) for the redirect URL the
        server sent the browser to."""

        query = {k: v[0] for k, v in parse_qs(urlparse(redirect_url).query).items()}
        assert query["code_challenge_method"] == "S256"
        assert "client_secret" not in query
        code = "google-code-" + secrets.token_urlsafe(12)
        self._codes[code] = query
        return code, query["state"]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == GOOGLE_JWKS_URI:
            return httpx.Response(200, json=await self.signer.jwks())
        assert str(request.url) == GOOGLE_TOKEN_ENDPOINT, request.url
        self.token_request_urls.append(str(request.url))
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_requests.append(form)
        if form.get("client_id") != TEST_CLIENT_ID:
            return httpx.Response(401, json={"error": "invalid_client"})
        if self.expected_secret is not None:
            if "client_secret" not in form:
                return httpx.Response(400, json={"error": "invalid_request", "error_description": "client_secret is missing."})
            if not secrets.compare_digest(form["client_secret"], self.expected_secret):
                # A real error body can echo request values: the provider must
                # never log or surface it.
                return httpx.Response(401, json={"error": "invalid_client",
                                                 "error_description": f"bad secret {form['client_secret']}"})
        authorized = self._codes.pop(form.get("code", ""), None)
        if authorized is None or form.get("grant_type") != "authorization_code":
            return httpx.Response(400, json={"error": "invalid_grant"})
        if form.get("redirect_uri") != authorized["redirect_uri"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        if code_challenge_for(form.get("code_verifier", "")) != authorized["code_challenge"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json={"id_token": self.signer.mint_id_token(nonce=authorized["nonce"]),
                                         "access_token": "google-access-token-never-used"})

    def provider(self) -> GoogleOIDCProvider:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return GoogleOIDCProvider(client_id=TEST_CLIENT_ID, issuer=TEST_ISSUER, http_client=client)


async def store_secret(storage, value: str, *, cls=SecretClass.OAUTH_TOKEN,
                       scope=SecretOwnerScopeType.SERVER, scope_id: str | None = None) -> str:
    """Bootstrap the store (idempotent) and put `value` in it; return the handle."""

    store = EncryptedLocalSecretStore()
    async with storage.session() as session:
        await store.bootstrap(session, resolve_kek(f"env:{TEST_KEK_ENV_VAR}"))
        handle = await store.set(
            session, owner_scope_type=scope, owner_scope_id=scope_id, secret_class=cls, value=value,
            requester=SecretRequester.server(), audit=AuditLogger(session, request_id=uuid.uuid4()),
        )
        await session.commit()
    return handle


class World:
    def __init__(self, client: httpx.AsyncClient, google: FakeGoogle, core, storage) -> None:
        self.client, self.google, self.core, self.storage = client, google, core, storage
        self.responses: list[httpx.Response] = []

    async def start(self, **params) -> httpx.Response:
        resp = await self.client.get(f"{V1}/auth/oidc/start", params=params)
        self.responses.append(resp)
        return resp

    async def login(self, **start_params) -> httpx.Response:
        start = await self.start(**start_params)
        assert start.status_code == 200, start.text
        code, state = self.google.authorize(start.json()["redirect_url"])
        resp = await self.client.get(f"{V1}/auth/oidc/callback", params={"code": code, "state": state},
                                     follow_redirects=False)
        self.responses.append(resp)
        return resp

    async def users(self) -> int:
        async with self.storage.session() as session:
            return len((await session.execute(select(User))).scalars().all())

    async def audit_rows(self) -> list[AuditEvent]:
        async with self.storage.session() as session:
            return list((await session.execute(select(AuditEvent))).scalars().all())


@pytest_asyncio.fixture
async def world_factory(storage, kek_value):
    clients: list[httpx.AsyncClient] = []

    async def make(*, config: AppConfig, google: FakeGoogle, desktop: bool = False) -> World:
        core = build_security_core(config, oidc_provider=google.provider())
        async with storage.session() as session:
            await core.secret_store.bootstrap(session, resolve_kek(f"env:{TEST_KEK_ENV_VAR}"))
            await session.commit()
        app = create_app(storage=storage, security=core,
                         desktop_config=DesktopConfig(enabled=True) if desktop else None)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        clients.append(client)
        return World(client, google, core, storage)

    yield make
    for client in clients:
        await client.aclose()


# ── configuration ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["GOCSPX-a-literal-looking-secret", "plain", "env:", "secretstore:", "file:/etc/x"])
def test_a_literal_or_malformed_reference_is_a_load_time_failure(value):
    with pytest.raises(ValidationError, match="secret reference must be"):
        oidc_config(value)


def test_a_client_secret_value_field_does_not_exist():
    with pytest.raises(ValidationError, match="client_secret"):
        make_test_config(security={"oidc": {"client_id": TEST_CLIENT_ID, "issuer": TEST_ISSUER,
                                            "client_secret": "anything"}})


@pytest.mark.parametrize("ref", [f"env:{SECRET_ENV}", "secretstore:0123abcd"])
def test_a_reference_is_accepted_and_absent_by_default(ref):
    assert oidc_config(ref).security.oidc.client_secret_ref == ref
    assert oidc_config().security.oidc.client_secret_ref is None


def test_the_default_redirect_uri_is_base_url_plus_the_callback_path():
    """The URI to register with Google: `server.base_url` + the existing path.
    With the defaults that is exactly http://127.0.0.1:8000/api/v1/auth/oidc/callback."""

    config = make_test_config(server={})
    assert config.server.base_url == "http://127.0.0.1:8000"
    assert OIDC_CALLBACK_PATH == "/api/v1/auth/oidc/callback"
    core = build_security_core(config)
    assert core.login_flow._redirect_uri == "http://127.0.0.1:8000/api/v1/auth/oidc/callback"


# ── the production provider's token request ─────────────────────────────────


async def test_the_secret_is_sent_in_the_token_request_body_only():
    secret = new_secret()
    google = FakeGoogle(expected_secret=secret)
    provider = google.provider()
    url = provider.authorization_url(state="s", nonce="n", code_challenge=code_challenge_for("v" * 64),
                                     redirect_uri="https://host.invalid/cb")
    assert secret not in url and "client_secret" not in url
    code, _ = google.authorize(url)
    raw = await provider.exchange_code(code=code, code_verifier="v" * 64, redirect_uri="https://host.invalid/cb",
                                       client_secret=secret)
    assert raw.count(".") == 2
    [form] = google.token_requests
    assert form == {"client_id": TEST_CLIENT_ID, "client_secret": secret, "code": code, "code_verifier": "v" * 64,
                    "grant_type": "authorization_code", "redirect_uri": "https://host.invalid/cb"}
    assert google.token_request_urls == [GOOGLE_TOKEN_ENDPOINT]   # no query string
    assert not hasattr(provider, "_client_secret")                 # never held by the provider


async def test_without_a_secret_the_request_is_unchanged():
    google = FakeGoogle(expected_secret=None)
    provider = google.provider()
    code, _ = google.authorize(provider.authorization_url(
        state="s", nonce="n", code_challenge=code_challenge_for("v" * 64), redirect_uri="https://host.invalid/cb"))
    await provider.exchange_code(code=code, code_verifier="v" * 64, redirect_uri="https://host.invalid/cb")
    assert "client_secret" not in google.token_requests[0]


async def test_a_refused_exchange_logs_only_the_error_code(caplog):
    secret, wrong = new_secret(), new_secret()
    google = FakeGoogle(expected_secret=secret)
    provider = google.provider()
    code, _ = google.authorize(provider.authorization_url(
        state="s", nonce="n", code_challenge=code_challenge_for("v" * 64), redirect_uri="https://host.invalid/cb"))
    caplog.set_level(logging.DEBUG)
    with pytest.raises(InvalidIdToken) as excinfo:
        await provider.exchange_code(code=code, code_verifier="v" * 64, redirect_uri="https://host.invalid/cb",
                                     client_secret=wrong)
    assert "error=invalid_client client_authenticated=True" in caplog.text
    assert wrong not in caplog.text and secret not in caplog.text and "bad secret" not in caplog.text
    assert wrong not in excinfo.value.reason and str(excinfo.value) == "authentication failed"


async def test_an_unauthenticated_refusal_points_at_the_setting(caplog):
    google = FakeGoogle(expected_secret=new_secret())
    provider = google.provider()
    code, _ = google.authorize(provider.authorization_url(
        state="s", nonce="n", code_challenge=code_challenge_for("v" * 64), redirect_uri="https://host.invalid/cb"))
    caplog.set_level(logging.WARNING)
    with pytest.raises(InvalidIdToken):
        await provider.exchange_code(code=code, code_verifier="v" * 64, redirect_uri="https://host.invalid/cb")
    assert "error=invalid_request client_authenticated=False" in caplog.text
    assert "security.oidc.client_secret_ref" in caplog.text
    assert "client_secret is missing" not in caplog.text   # never the provider's own text


# ── resolution inside the auth boundary ─────────────────────────────────────


async def test_an_env_reference_resolves_at_use(db, audit, store, monkeypatch):
    source = ClientSecretSource(secret_ref=f"env:{SECRET_ENV}", secret_store=store)
    first, second = new_secret(), new_secret()
    monkeypatch.setenv(SECRET_ENV, first)
    assert await source.resolve(db, audit) == first
    monkeypatch.setenv(SECRET_ENV, second)      # rotation: read per use, never cached
    assert await source.resolve(db, audit) == second


@pytest.mark.parametrize("value", [None, "", "   "])
async def test_an_unset_or_empty_env_reference_fails_closed(db, audit, store, monkeypatch, value, caplog):
    if value is None:
        monkeypatch.delenv(SECRET_ENV, raising=False)
    else:
        monkeypatch.setenv(SECRET_ENV, value)
    source = ClientSecretSource(secret_ref=f"env:{SECRET_ENV}", secret_store=store)
    with pytest.raises(OIDCClientSecretUnavailable) as excinfo:
        await source.resolve(db, audit)
    assert excinfo.value.reason == "client_secret_env_unset"
    assert SECRET_ENV in caplog.text          # the operator learns which variable


async def test_a_store_reference_resolves_for_the_server_and_is_audited_by_handle(db, audit, store):
    secret = new_secret()
    handle = await store.set(db, owner_scope_type=SecretOwnerScopeType.SERVER, owner_scope_id=None,
                             secret_class=SecretClass.OAUTH_TOKEN, value=secret,
                             requester=SecretRequester.server(), audit=audit)
    source = ClientSecretSource(secret_ref=handle, secret_store=store)
    assert await source.resolve(db, audit) == secret
    rows = (await db.execute(select(AuditEvent).where(AuditEvent.action == "secret.get"))).scalars().all()
    assert rows and all(handle in (row.resource or "") for row in rows)
    assert not await database_contains(db, secret)


@pytest.mark.parametrize("cls, scope, scope_id", [
    (SecretClass.MODEL_API_KEY, SecretOwnerScopeType.SERVER, None),
    (SecretClass.DEVICE_CREDENTIAL, SecretOwnerScopeType.SERVER, None),
    (SecretClass.OAUTH_TOKEN, SecretOwnerScopeType.USER, str(uuid.uuid4())),
])
async def test_any_other_secret_is_refused_even_though_it_exists(db, audit, store, cls, scope, scope_id):
    secret = new_secret()
    handle = await store.set(db, owner_scope_type=scope, owner_scope_id=scope_id, secret_class=cls, value=secret,
                             requester=SecretRequester.server(), audit=audit)
    source = ClientSecretSource(secret_ref=handle, secret_store=store)
    with pytest.raises(OIDCClientSecretUnavailable) as excinfo:
        await source.resolve(db, audit)
    assert excinfo.value.reason == "client_secret_wrong_class_or_scope"
    with pytest.raises(ClientSecretConfigError):
        await source.verify(db)
    blocked = (await db.execute(select(AuditEvent).where(AuditEvent.action == "secret.get"))).scalars().all()
    assert blocked and secret not in str([vars(r) for r in blocked])


async def test_a_revoked_missing_or_locked_store_reference_fails_closed(db, audit, store):
    handle = await store.set(db, owner_scope_type=SecretOwnerScopeType.SERVER, owner_scope_id=None,
                             secret_class=SecretClass.OAUTH_TOKEN, value=new_secret(),
                             requester=SecretRequester.server(), audit=audit)
    source = ClientSecretSource(secret_ref=handle, secret_store=store)
    await source.verify(db)                                    # usable
    missing = ClientSecretSource(secret_ref="secretstore:" + uuid.uuid4().hex, secret_store=store)
    with pytest.raises(OIDCClientSecretUnavailable):
        await missing.resolve(db, audit)
    with pytest.raises(ClientSecretConfigError, match="no such SecretStore reference"):
        await missing.verify(db)
    store.lock()
    with pytest.raises(OIDCClientSecretUnavailable):
        await source.resolve(db, audit)
    await store.unlock(db, resolve_kek(f"env:{TEST_KEK_ENV_VAR}"))
    await store.delete(db, handle, SecretRequester.server(), audit)
    with pytest.raises(OIDCClientSecretUnavailable):
        await source.resolve(db, audit)
    with pytest.raises(ClientSecretConfigError, match="revoked"):
        await source.verify(db)


def test_a_source_cannot_hold_a_literal(store):
    with pytest.raises(ValueError):
        ClientSecretSource(secret_ref="a-literal-secret", secret_store=store)


# ── the login flow end to end ───────────────────────────────────────────────


async def test_an_env_secret_signs_in_with_pkce_and_the_stored_redirect_uri(world_factory, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    secret = new_secret()
    monkeypatch.setenv(SECRET_ENV, secret)
    world = await world_factory(config=oidc_config(f"env:{SECRET_ENV}"), google=FakeGoogle(expected_secret=secret))
    resp = await world.login()
    assert resp.status_code == 200, resp.text
    assert resp.json()["bootstrap_token"]
    [form] = world.google.token_requests
    assert form["client_secret"] == secret
    # FakeGoogle already verified S256(code_verifier) == the challenge it was sent.
    assert form["redirect_uri"] == "http://test.invalid" + OIDC_CALLBACK_PATH
    async with world.storage.session() as session:
        [state] = (await session.execute(select(OIDCLoginState))).scalars().all()
        assert state.redirect_uri == form["redirect_uri"] and state.code_verifier == form["code_verifier"]
        assert not await database_contains(session, secret)
    for r in world.responses:
        assert secret not in r.text and secret not in str(r.headers)
    assert secret not in caplog.text


async def test_a_store_secret_signs_in(world_factory, storage, caplog):
    caplog.set_level(logging.DEBUG)
    secret = new_secret()
    handle = await store_secret(storage, secret)
    world = await world_factory(config=oidc_config(handle), google=FakeGoogle(expected_secret=secret))
    resp = await world.login()
    assert resp.status_code == 200, resp.text
    assert world.google.token_requests[0]["client_secret"] == secret
    async with storage.session() as session:
        assert not await database_contains(session, secret)
    assert secret not in caplog.text


async def test_a_wrong_secret_is_an_ordinary_authentication_failure(world_factory, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    right, wrong = new_secret(), new_secret()
    monkeypatch.setenv(SECRET_ENV, wrong)
    world = await world_factory(config=oidc_config(f"env:{SECRET_ENV}"), google=FakeGoogle(expected_secret=right))
    resp = await world.login()
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "unauthenticated"
    assert wrong not in resp.text and right not in resp.text
    assert await world.users() == 0
    assert [r.resource for r in await world.audit_rows() if r.action == AuditAction.OIDC_LOGIN_REJECTED] == ["auth:oidc"]
    assert "error=invalid_client" in caplog.text and wrong not in caplog.text


async def test_a_missing_secret_stops_the_login_before_google_is_called(world_factory, monkeypatch):
    monkeypatch.delenv(SECRET_ENV, raising=False)
    world = await world_factory(config=oidc_config(f"env:{SECRET_ENV}"), google=FakeGoogle(expected_secret=new_secret()))
    resp = await world.login()
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "dependency_unavailable"
    assert resp.json()["error"]["message"] == "sign-in is not available on this server right now"
    assert world.google.token_requests == []           # nothing left the server
    assert await world.users() == 0
    rejected = [r.resource for r in await world.audit_rows() if r.action == AuditAction.OIDC_LOGIN_REJECTED]
    assert rejected == ["auth:oidc:client_secret_unavailable"]


async def test_pkce_is_still_verified(world_factory, monkeypatch):
    """With the secret right, a tampered verifier still fails at Google."""

    secret = new_secret()
    monkeypatch.setenv(SECRET_ENV, secret)
    world = await world_factory(config=oidc_config(f"env:{SECRET_ENV}"), google=FakeGoogle(expected_secret=secret))
    start = await world.start()
    code, state = world.google.authorize(start.json()["redirect_url"])
    async with world.storage.session() as session:
        await session.execute(update(OIDCLoginState).values(code_verifier="x" * 64))
        await session.commit()
    resp = await world.client.get(f"{V1}/auth/oidc/callback", params={"code": code, "state": state})
    assert resp.status_code == 401
    assert world.google.token_requests[0]["client_secret"] == secret
    assert await world.users() == 0


@pytest.mark.parametrize("label, overrides, foreign", [
    ("issuer", {"iss": "https://evil.example"}, False),
    ("audience", {"aud": "someone-elses-client-id"}, False),
    ("nonce", {"nonce": "not-the-login-nonce"}, False),
    ("expiry", {"exp": 1}, False),
    ("signature", {}, True),
])
async def test_every_id_token_check_still_applies(world_factory, monkeypatch, label, overrides, foreign):
    secret = new_secret()
    monkeypatch.setenv(SECRET_ENV, secret)
    signer = LocalOIDCProvider(claim_overrides=overrides, sign_with_foreign_key=foreign)
    world = await world_factory(config=oidc_config(f"env:{SECRET_ENV}"),
                                google=FakeGoogle(expected_secret=secret, signer=signer))
    resp = await world.login()
    assert resp.status_code == 401, label
    assert world.google.token_requests[0]["client_secret"] == secret   # the exchange itself succeeded
    assert await world.users() == 0


async def test_the_allow_list_still_applies(world_factory, monkeypatch):
    secret = new_secret()
    monkeypatch.setenv(SECRET_ENV, secret)
    refused = await world_factory(config=oidc_config(f"env:{SECRET_ENV}", allowed_emails=["other@example.test"]),
                                  google=FakeGoogle(expected_secret=secret,
                                                    signer=LocalOIDCProvider(claim_overrides={"email_verified": True})))
    resp = await refused.login()
    assert resp.status_code == 403
    assert await refused.users() == 0


async def test_the_allow_list_admits_a_listed_verified_email(world_factory, monkeypatch):
    secret = new_secret()
    monkeypatch.setenv(SECRET_ENV, secret)
    world = await world_factory(config=oidc_config(f"env:{SECRET_ENV}", allowed_emails=["person@example.test"]),
                                google=FakeGoogle(expected_secret=secret,
                                                  signer=LocalOIDCProvider(claim_overrides={"email_verified": True})))
    resp = await world.login()
    assert resp.status_code == 200, resp.text


async def test_the_desktop_handoff_is_unchanged_and_never_carries_the_secret(world_factory, monkeypatch):
    secret = new_secret()
    monkeypatch.setenv(SECRET_ENV, secret)
    world = await world_factory(config=oidc_config(f"env:{SECRET_ENV}"),
                                google=FakeGoogle(expected_secret=secret), desktop=True)
    verifier, desktop_state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    callback = await world.login(desktop_state=desktop_state, desktop_challenge=s256_challenge(verifier),
                                 desktop_port=PORT)
    assert callback.status_code == 302, callback.text
    location = callback.headers["location"]
    parsed = urlparse(location)
    assert (parsed.scheme, parsed.hostname, parsed.port, parsed.path) == ("http", "127.0.0.1", PORT, "/jarvis/callback")
    query = parse_qs(parsed.query)
    assert query["state"] == [desktop_state] and set(query) == {"code", "state"}
    assert secret not in location
    assert world.google.token_requests[0]["client_secret"] == secret

    redeem = lambda v: world.client.post(f"{V1}/auth/desktop/handoff", json={"code": query["code"][0], "code_verifier": v})  # noqa: E731
    assert (await redeem(secrets.token_urlsafe(48))).status_code == 401      # wrong verifier
    ok = await redeem(verifier)
    assert ok.status_code == 200 and ok.json()["bootstrap_token"]
    assert secret not in ok.text
    assert (await redeem(verifier)).status_code == 401                       # single use


# ── startup ─────────────────────────────────────────────────────────────────


async def test_startup_refuses_an_unusable_reference_and_accepts_a_usable_one(storage, kek_value, monkeypatch):
    monkeypatch.delenv(SECRET_ENV, raising=False)
    core = build_security_core(oidc_config(f"env:{SECRET_ENV}"), oidc_provider=LocalOIDCProvider())
    async with storage.session() as session:
        await core.secret_store.bootstrap(session, resolve_kek(f"env:{TEST_KEK_ENV_VAR}"))
        await session.commit()
    app = create_app(storage=storage, security=core, unlock_secrets_on_startup=True)
    with pytest.raises(ClientSecretConfigError, match=SECRET_ENV):
        async with app.router.lifespan_context(app):
            pass
    monkeypatch.setenv(SECRET_ENV, new_secret())
    async with app.router.lifespan_context(app):
        pass
    # No reference configured: nothing to check.
    plain = build_security_core(oidc_config(), oidc_provider=LocalOIDCProvider())
    await verify_oidc_client_secret(plain, storage)


# ── the operator command ────────────────────────────────────────────────────


async def test_the_operator_command_stores_checks_and_rotates_without_printing_the_value(
    storage, kek_value, tmp_path, monkeypatch, capsys
):
    from server.composition import oidc_client_secret as cli

    async with storage.session() as session:
        await EncryptedLocalSecretStore().bootstrap(session, resolve_kek(f"env:{TEST_KEK_ENV_VAR}"))
        await session.commit()
    url = storage._engine.url.render_as_string(hide_password=False)

    def write_config(ref: str | None) -> str:
        payload = make_test_config().model_dump(mode="json")
        payload["database_url"] = url
        if ref:
            payload["security"]["oidc"]["client_secret_ref"] = ref
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(payload), encoding="utf-8", newline="\n")
        return str(path)

    first, second = new_secret(), new_secret()
    monkeypatch.setattr("sys.stdin", _Stdin(first))
    assert await cli._run("store", write_config(None), None) == 0
    out = capsys.readouterr()
    handle = out.out.strip()
    assert handle.startswith("secretstore:") and first not in out.out + out.err

    config_path = write_config(handle)
    assert await cli._run("check", config_path, None) == 0

    monkeypatch.setattr("sys.stdin", _Stdin(second))
    assert await cli._run("rotate", config_path, handle) == 0
    out = capsys.readouterr()
    assert second not in out.out + out.err

    store = EncryptedLocalSecretStore()
    async with storage.session() as session:
        await store.unlock(session, resolve_kek(f"env:{TEST_KEK_ENV_VAR}"))
        source = ClientSecretSource(secret_ref=handle, secret_store=store)
        assert await source.resolve(session, AuditLogger(session, request_id=uuid.uuid4())) == second
        ref = await session.get(SecretReference, handle)
        assert ref.class_ is SecretClass.OAUTH_TOKEN and ref.owner_scope_type is SecretOwnerScopeType.SERVER
        model_key = await store.set(session, owner_scope_type=SecretOwnerScopeType.SERVER, owner_scope_id=None,
                                    secret_class=SecretClass.MODEL_API_KEY, value=new_secret(),
                                    requester=SecretRequester.server(),
                                    audit=AuditLogger(session, request_id=uuid.uuid4()))
        await session.commit()
    monkeypatch.setattr("sys.stdin", _Stdin(new_secret()))
    assert await cli._run("rotate", config_path, model_key) == 1      # never another kind of secret


class _Stdin:
    def __init__(self, value: str) -> None:
        self._value = value

    def isatty(self) -> bool:
        return False

    def readline(self) -> str:
        return self._value + "\n"


# ── oauth_token is shared with the push credential ──────────────────────────


# Assembled at runtime so no key-shaped literal sits in the repository (the
# repo secret scan, tests/foundation/test_repo_secret_scan.py). Not a key.
_PEM_BEGIN, _PEM_END = "-----BEGIN " + "PRIVATE KEY-----", "-----END " + "PRIVATE KEY-----"
SERVICE_ACCOUNT_SHAPED = json.dumps({"type": "service_account", "client_email": "x@y.test",
                                     "private_key": f"{_PEM_BEGIN}\nTEST\n{_PEM_END}\n"})


@pytest.mark.parametrize("value", [
    SERVICE_ACCOUNT_SHAPED,
    f"{_PEM_BEGIN}TEST{_PEM_END}",
    "two words",
    '["a"]',
])
async def test_a_value_shaped_like_another_credential_is_never_sent(world_factory, storage, value, caplog):
    """The push wake's service-account key is also a server-owned oauth_token.
    If its handle were pasted into client_secret_ref, nothing may go to Google."""

    caplog.set_level(logging.DEBUG)
    handle = await store_secret(storage, value)
    world = await world_factory(config=oidc_config(handle), google=FakeGoogle(expected_secret=new_secret()))
    resp = await world.login()
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "dependency_unavailable"
    assert world.google.token_requests == []
    assert await world.users() == 0
    assert "shaped like another credential" in caplog.text
    assert "PRIVATE KEY" not in caplog.text and "service_account" not in caplog.text


async def test_the_same_guard_applies_to_an_env_reference(db, audit, store, monkeypatch):
    monkeypatch.setenv(SECRET_ENV, SERVICE_ACCOUNT_SHAPED)
    source = ClientSecretSource(secret_ref=f"env:{SECRET_ENV}", secret_store=store)
    with pytest.raises(OIDCClientSecretUnavailable) as excinfo:
        await source.resolve(db, audit)
    assert excinfo.value.reason == "client_secret_wrong_shape"


async def test_the_push_sender_cannot_use_an_oidc_client_secret_either():
    """The reverse direction: the FCM sender parses its credential as
    service-account JSON, so an OAuth client secret fails locally."""

    from server.execution.device_wake import PushCredentialError, _service_account

    with pytest.raises(PushCredentialError):
        _service_account(new_secret())


def test_a_genuine_client_secret_shape_passes():
    from server.auth.client_secret import looks_like_another_credential

    for _ in range(20):
        assert not looks_like_another_credential(new_secret())
    assert not looks_like_another_credential("GOCSPX-" + secrets.token_urlsafe(21))
