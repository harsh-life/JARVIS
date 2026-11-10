"""OD-DESK-1 — the desktop companion's device identity.

The companion is a distinct registered device (`DevicePlatform.desktop`) that
signs in through the system browser and a loopback handoff, holds its own
Ed25519 device key, and gets ordinary short-lived access tokens through the
unchanged `POST /sessions/token`. What is pinned here:

* **No bearer credential in a URL.** The callback returns a one-time handoff
  code to `127.0.0.1:<port>`; the code is worthless without the companion's
  PKCE verifier, is single-use and expires.
* **Login injection fails.** A code minted for another login does not redeem
  with this companion's verifier, and the companion's own state is echoed.
* **The platform binding holds both ways.** A handoff's bootstrap token
  registers only a desktop, an ordinary one never registers a desktop, and a
  desktop must bring its own key (the server never mints one for it).
* **A desktop session only presents.** Outside its activity report, its own
  logout and its own revocation, every route refuses it (`403`), and the
  device channel refuses it — so it cannot submit, confirm, approve, step up
  or become an execution target.
* **Independent revocation.** A desktop is revoked without touching the
  phone, its tokens and proofs die at once, and it can register again.
* **Nothing secret is kept or audited.** No code, verifier, bootstrap token,
  private key or access token appears anywhere in the database.

`tests/integration/test_desktop_scope.py` repeats the scope check against
every route of the full production composition.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import update

from server.auth.desktop_handoff import s256_challenge
from server.auth.device import build_device_proof, registration_message
from server.config.schema import AndroidConfig, DesktopConfig
from server.execution.android import MAPPING_VERSION
from server.execution.device_hub import DeviceHub
from server.gateway.app import API_V1_PREFIX, create_app
from server.gateway.device_scope import DesktopScope, desktop_scope_of
from server.gateway.security import build_security_core
from server.presentation.routes import router as presentation_router
from server.secrets.kek import resolve_kek
from server.storage.models import DesktopHandoff
from tests.device_channel_support import AsgiWebSocket
from tests.security_core.conftest import TEST_KEK_ENV_VAR, Api, LocalOIDCProvider, make_test_config
from tests.security_core.helpers import audit_actions, database_contains

V1 = API_V1_PREFIX
PORT = 53682
REPO = Path(__file__).resolve().parents[2]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@dataclass
class Desktop:
    """What a signed-in companion holds: its device id, its private key
    (in the OS credential store on a real desktop) and a live access token."""

    device_id: uuid.UUID
    seed: str
    access_token: str

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


@dataclass
class DesktopLogin:
    verifier: str
    state: str
    code: str
    location: str


class DesktopClient:
    """The companion's side of the protocol, written from the spec — the
    reference the Rust client (`desktop/src-tauri/src/auth.rs`) mirrors."""

    def __init__(self, api: Api) -> None:
        self.api = api

    async def start(self, *, verifier: str | None = None, state: str | None = None, port: int = PORT):
        verifier = verifier or secrets.token_urlsafe(48)
        state = state or secrets.token_urlsafe(32)
        resp = await self.api.client.get(
            f"{V1}/auth/oidc/start",
            params={"desktop_state": state, "desktop_challenge": s256_challenge(verifier), "desktop_port": port},
        )
        return resp, verifier, state

    async def login(self, subject: str = "alice", *, port: int = PORT) -> DesktopLogin:
        self.api.provider.subject = subject
        start, verifier, state = await self.start(port=port)
        assert start.status_code == 200, start.text
        provider_state = parse_qs(urlparse(start.json()["redirect_url"]).query)["state"][0]
        callback = await self.api.client.get(
            f"{V1}/auth/oidc/callback",
            params={"code": "test-authorization-code", "state": provider_state},
            follow_redirects=False,
        )
        assert callback.status_code == 302, callback.text
        location = callback.headers["location"]
        query = parse_qs(urlparse(location).query)
        assert query["state"] == [state]
        return DesktopLogin(verifier=verifier, state=state, code=query["code"][0], location=location)

    async def redeem(self, code: str, verifier: str) -> httpx.Response:
        return await self.api.client.post(
            f"{V1}/auth/desktop/handoff", json={"code": code, "code_verifier": verifier}
        )

    async def register(self, bootstrap: str, *, platform: str = "desktop", key: Ed25519PrivateKey | None = None,
                       with_key: bool = True) -> tuple[httpx.Response, Ed25519PrivateKey]:
        key = key or Ed25519PrivateKey.generate()
        body: dict = {"platform": platform}
        if with_key:
            public = _b64(key.public_key().public_bytes_raw())
            body |= {"public_key": public,
                     "key_proof": _b64(key.sign(registration_message(bootstrap_token=bootstrap, public_key=public)))}
        resp = await self.api.client.post(f"{V1}/devices", json=body,
                                          headers={"Authorization": f"Bearer {bootstrap}"})
        return resp, key

    async def token(self, device_id: uuid.UUID, seed: str) -> httpx.Response:
        return await self.api.client.post(
            f"{V1}/sessions/token",
            json={"device_credential": build_device_proof(device_id=device_id, device_credential=seed)},
        )

    async def onboard(self, subject: str = "alice") -> Desktop:
        login = await self.login(subject)
        redeemed = await self.redeem(login.code, login.verifier)
        assert redeemed.status_code == 200, redeemed.text
        reg, key = await self.register(redeemed.json()["bootstrap_token"])
        assert reg.status_code == 201, reg.text
        assert "device_credential" not in reg.json()
        device_id = uuid.UUID(reg.json()["device_id"])
        seed = _b64(key.private_bytes_raw())
        tok = await self.token(device_id, seed)
        assert tok.status_code == 200, tok.text
        return Desktop(device_id=device_id, seed=seed, access_token=tok.json()["access_token"])


async def _make_api(storage, *, desktop: DesktopConfig, hub: DeviceHub | None = None):
    provider = LocalOIDCProvider()
    core = build_security_core(make_test_config(), oidc_provider=provider)
    async with storage.session() as session:
        await core.secret_store.bootstrap(session, resolve_kek(f"env:{TEST_KEK_ENV_VAR}"))
        await session.commit()
    app = create_app(storage=storage, security=core, desktop_config=desktop,
                     android_config=AndroidConfig(enabled=hub is not None), device_hub=hub)
    # Mounted by the composition root in production (server/composition), as here.
    app.include_router(presentation_router, prefix=API_V1_PREFIX)
    return app, provider, core


@pytest_asyncio.fixture
async def desk(storage, kek_value):
    app, provider, core = await _make_api(storage, desktop=DesktopConfig(enabled=True))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield DesktopClient(Api(client=client, provider=provider, core=core, storage=storage))


# ── the loopback handoff ────────────────────────────────────────────────


async def test_a_desktop_signs_in_registers_and_reads_its_activity(desk):
    desktop = await desk.onboard()
    resp = await desk.api.client.get(f"{V1}/presentation/activity", headers=desktop.auth)
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"] == "no-store"
    assert set(resp.json()) == {"schema_version", "computed_at", "window_s", "scores", "components"}


async def test_the_callback_returns_to_loopback_with_no_bearer_credential(desk):
    login = await desk.login()
    url = urlparse(login.location)
    assert (url.scheme, url.hostname, url.port, url.path) == ("http", "127.0.0.1", PORT, "/jarvis/callback")
    assert url.fragment == ""
    assert set(parse_qs(url.query)) == {"code", "state"}
    # The code is not a bearer credential anywhere: it registers nothing and
    # authenticates nothing.
    for method, path, body in (("POST", f"{V1}/devices", {"platform": "desktop"}),
                               ("GET", f"{V1}/presentation/activity", None),
                               ("GET", f"{V1}/me", None)):
        resp = await desk.api.client.request(method, path, json=body,
                                             headers={"Authorization": f"Bearer {login.code}"})
        assert resp.status_code == 401, (path, resp.status_code)
    # …and it is still redeemable by its rightful holder afterwards.
    assert (await desk.redeem(login.code, login.verifier)).status_code == 200


async def test_the_callback_host_cannot_be_chosen(desk):
    start, _, _ = await desk.start()
    hostile = await desk.api.client.get(
        f"{V1}/auth/oidc/start",
        params={"desktop_state": "s" * 40, "desktop_challenge": "c" * 43, "desktop_port": PORT,
                "redirect_uri": "https://attacker.example/cb", "return_to": "http://evil.example/"},
    )
    assert hostile.status_code == 200
    desk.api.provider.subject = "alice"
    provider_state = parse_qs(urlparse(hostile.json()["redirect_url"]).query)["state"][0]
    callback = await desk.api.client.get(f"{V1}/auth/oidc/callback", follow_redirects=False,
                                         params={"code": "c", "state": provider_state,
                                                 "redirect_uri": "https://attacker.example/cb"})
    assert urlparse(callback.headers["location"]).hostname == "127.0.0.1"
    assert "attacker" not in callback.headers["location"]
    assert callback.headers["cache-control"] == "no-store"


async def test_the_handoff_needs_the_verifier_and_a_wrong_one_does_not_burn_it(desk):
    login = await desk.login()
    assert (await desk.redeem(login.code, secrets.token_urlsafe(48))).status_code == 401
    redeemed = await desk.redeem(login.code, login.verifier)
    assert redeemed.status_code == 200
    assert redeemed.headers["cache-control"] == "no-store"
    assert set(redeemed.json()) == {"bootstrap_token"}


async def test_the_handoff_is_single_use(desk):
    login = await desk.login()
    assert (await desk.redeem(login.code, login.verifier)).status_code == 200
    assert (await desk.redeem(login.code, login.verifier)).status_code == 401


async def test_an_expired_handoff_is_refused(desk):
    login = await desk.login()
    async with desk.api.storage.session() as db:
        await db.execute(update(DesktopHandoff).values(
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await db.commit()
    assert (await desk.redeem(login.code, login.verifier)).status_code == 401


async def test_a_login_started_elsewhere_cannot_be_injected(desk):
    """An attacker's own completed login, delivered to this companion's
    listener, does not redeem with this companion's verifier."""

    attacker = await desk.login("mallory")
    victim_verifier = secrets.token_urlsafe(48)
    assert (await desk.redeem(attacker.code, victim_verifier)).status_code == 401


@pytest.mark.parametrize(
    "params",
    [
        {"desktop_state": "s" * 40, "desktop_challenge": "c" * 43},                       # no port
        {"desktop_state": "s" * 40, "desktop_port": PORT},                                # no challenge
        {"desktop_challenge": "c" * 43, "desktop_port": PORT},                            # no state
        {"desktop_state": "s" * 40, "desktop_challenge": "c" * 42, "desktop_port": PORT},  # short challenge
        {"desktop_state": "s" * 40, "desktop_challenge": "c" * 43, "desktop_port": 80},    # privileged port
        {"desktop_state": "s" * 40, "desktop_challenge": "c" * 43, "desktop_port": 70000},
        {"desktop_state": "short", "desktop_challenge": "c" * 43, "desktop_port": PORT},
        {"desktop_state": "s" * 40, "desktop_challenge": "c" * 43, "desktop_port": PORT,
         "app_state": "a" * 40},                                                          # mixed handoffs
    ],
)
async def test_a_malformed_desktop_start_is_refused(desk, params):
    assert (await desk.api.client.get(f"{V1}/auth/oidc/start", params=params)).status_code == 422


async def test_the_desktop_login_is_off_by_default(api):
    client = DesktopClient(api)
    start, _, _ = await client.start()
    assert start.status_code == 422
    resp = await api.client.post(f"{V1}/auth/desktop/handoff",
                                 json={"code": "x" * 43, "code_verifier": "v" * 43})
    assert resp.status_code == 404


def test_the_verifier_challenge_is_rfc_7636_s256():
    # RFC 7636 Appendix B.
    assert s256_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == \
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert s256_challenge("x" * 43) == _b64(hashlib.sha256(b"x" * 43).digest())


# ── the platform binding ────────────────────────────────────────────────


async def test_a_handoff_token_registers_only_a_desktop(desk):
    login = await desk.login()
    bootstrap = (await desk.redeem(login.code, login.verifier)).json()["bootstrap_token"]
    as_phone, _ = await desk.register(bootstrap, platform="android")
    assert as_phone.status_code == 401
    # Refused after it was spent: the companion must sign in again.
    again, _ = await desk.register(bootstrap)
    assert again.status_code == 401


async def test_an_ordinary_login_cannot_register_a_desktop(desk):
    bootstrap = await desk.api.oidc_login()
    # Without a key: refused before the token is spent.
    no_key, _ = await desk.register(bootstrap, with_key=False)
    assert no_key.status_code == 401
    # With one: the token is spent and the platform does not match.
    with_key, _ = await desk.register(bootstrap)
    assert with_key.status_code == 401
    # A fresh ordinary login still registers a phone as before.
    await desk.api.register_device(await desk.api.oidc_login())


async def test_a_desktop_must_bring_its_own_key(desk):
    login = await desk.login()
    bootstrap = (await desk.redeem(login.code, login.verifier)).json()["bootstrap_token"]
    no_key, _ = await desk.register(bootstrap, with_key=False)
    assert no_key.status_code == 401
    # Not spent: the same token then registers with a key.
    ok, _ = await desk.register(bootstrap)
    assert ok.status_code == 201
    assert "device_credential" not in ok.json()


async def test_a_key_proof_for_another_key_is_refused(desk):
    login = await desk.login()
    bootstrap = (await desk.redeem(login.code, login.verifier)).json()["bootstrap_token"]
    key, other = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    public = _b64(key.public_key().public_bytes_raw())
    resp = await desk.api.client.post(
        f"{V1}/devices", headers={"Authorization": f"Bearer {bootstrap}"},
        json={"platform": "desktop", "public_key": public,
              "key_proof": _b64(other.sign(registration_message(bootstrap_token=bootstrap, public_key=public)))},
    )
    assert resp.status_code == 401


# ── a desktop session only presents ─────────────────────────────────────


def test_the_desktop_scope_is_exactly_the_presentation_surface():
    """The `@desktop_scope` marker is the only way into a desktop session's
    scope; this pins every use of it in the server source. (The full
    production composition is then checked route by route, behaviourally, in
    tests/integration/test_desktop_scope.py.)"""

    marked = set()
    for path in sorted((REPO / "server").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                if isinstance(deco, ast.Call) and getattr(deco.func, "id", None) == "desktop_scope":
                    marked.add((str(path.relative_to(REPO)), node.name, ast.unparse(deco.args[0])))
    assert marked == {
        ("server/presentation/routes.py", "get_activity", "DesktopScope.PRESENTATION"),
        ("server/gateway/routers/sessions.py", "logout", "DesktopScope.OWN_SESSION"),
        ("server/gateway/routers/auth.py", "revoke_device", "DesktopScope.OWN_DEVICE"),
    }
    assert {s.value for s in DesktopScope} == {"presentation", "own_session", "own_device"}
    assert desktop_scope_of(object()) is None


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", f"{V1}/me", None),
        ("GET", f"{V1}/devices", None),
        ("GET", f"{V1}/graphs", None),
        ("GET", f"{V1}/config/presentation", None),
        ("PUT", f"{V1}/config/presentation", {}),
        ("POST", f"{V1}/agent/tasks", {"input": "hello"}),
        ("GET", f"{V1}/agent/tasks", None),
        ("POST", f"{V1}/sessions/step-up/challenge", None),
        ("POST", f"{V1}/devices/me/step-up-key", {"public_key": "x"}),
        ("PUT", f"{V1}/devices/me/push-token", {"provider": "fcm", "token": "x"}),
        ("POST", f"{V1}/sessions/active-graph", {"graph_id": str(uuid.uuid4())}),
    ],
)
async def test_a_desktop_session_is_refused_everything_else(desk, method, path, body):
    desktop = await desk.onboard()
    resp = await desk.api.client.request(method, path, json=body,
                                         headers={**desktop.auth, "Idempotency-Key": uuid.uuid4().hex})
    assert resp.status_code == 403, (path, resp.status_code, resp.text)
    assert resp.json()["error"]["code"] == "unauthorized"


async def test_a_scope_refusal_is_audited(desk):
    desktop = await desk.onboard()
    await desk.api.client.get(f"{V1}/me", headers=desktop.auth)
    async with desk.api.storage.session() as db:
        assert "session.device_scope.refused" in await audit_actions(db)


async def test_a_phone_session_is_unaffected(desk):
    phone = await desk.api.onboard("alice")
    assert (await desk.api.client.get(f"{V1}/me", headers=phone.auth)).status_code == 200
    assert (await desk.api.client.get(f"{V1}/presentation/activity", headers=phone.auth)).status_code == 200


async def test_the_device_channel_refuses_a_desktop(storage, kek_value):
    hub = DeviceHub()
    app, provider, core = await _make_api(storage, desktop=DesktopConfig(enabled=True), hub=hub)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        desk = DesktopClient(Api(client=client, provider=provider, core=core, storage=storage))
        desktop = await desk.onboard()
        ws = AsgiWebSocket(app)
        await ws.connect()
        await ws.send_json({
            "type": "hello", "access_token": desktop.access_token,
            "device_proof": build_device_proof(device_id=desktop.device_id, device_credential=desktop.seed),
            "mapping_version": MAPPING_VERSION, "client_version": "desktop-test",
        })
        assert await ws.expect_close() == 4001
        assert not await hub.is_connected(device_id=desktop.device_id)


# ── revocation, sign-out and re-registration ────────────────────────────


async def test_a_desktop_is_revoked_without_touching_the_phone(desk):
    phone = await desk.api.onboard("alice")
    desktop = await desk.onboard("alice")
    listed = (await desk.api.client.get(f"{V1}/devices", headers=phone.auth)).json()["items"]
    platforms = {d["device_id"]: d["platform"] for d in listed}
    assert platforms == {str(phone.device_id): "android", str(desktop.device_id): "desktop"}

    resp = await desk.api.client.delete(f"{V1}/devices/{desktop.device_id}", headers=phone.auth)
    assert resp.status_code == 204
    assert (await desk.api.client.get(f"{V1}/presentation/activity", headers=desktop.auth)).status_code == 401
    assert (await desk.token(desktop.device_id, desktop.seed)).status_code == 401
    assert (await desk.api.client.get(f"{V1}/me", headers=phone.auth)).status_code == 200


async def test_a_desktop_revokes_itself_but_never_the_phone(desk):
    phone = await desk.api.onboard("alice")
    desktop = await desk.onboard("alice")
    other = await desk.api.client.delete(f"{V1}/devices/{phone.device_id}", headers=desktop.auth)
    assert other.status_code == 403
    assert (await desk.api.client.get(f"{V1}/me", headers=phone.auth)).status_code == 200

    own = await desk.api.client.delete(f"{V1}/devices/{desktop.device_id}", headers=desktop.auth)
    assert own.status_code == 204
    assert (await desk.token(desktop.device_id, desktop.seed)).status_code == 401
    assert (await desk.api.client.get(f"{V1}/me", headers=phone.auth)).status_code == 200


async def test_logout_ends_the_session_and_the_key_mints_a_new_one(desk):
    desktop = await desk.onboard()
    assert (await desk.api.client.post(f"{V1}/sessions/logout", headers=desktop.auth)).status_code == 204
    assert (await desk.api.client.get(f"{V1}/presentation/activity", headers=desktop.auth)).status_code == 401
    fresh = await desk.token(desktop.device_id, desktop.seed)
    assert fresh.status_code == 200


async def test_a_revoked_desktop_can_register_again(desk):
    first = await desk.onboard("alice")
    await desk.api.client.delete(f"{V1}/devices/{first.device_id}", headers=first.auth)
    second = await desk.onboard("alice")
    assert second.device_id != first.device_id
    assert (await desk.api.client.get(f"{V1}/presentation/activity", headers=second.auth)).status_code == 200


# ── nothing secret is kept ──────────────────────────────────────────────


async def test_no_handoff_secret_or_key_is_stored_anywhere(desk):
    login = await desk.login()
    redeemed = await desk.redeem(login.code, login.verifier)
    bootstrap = redeemed.json()["bootstrap_token"]
    reg, key = await desk.register(bootstrap)
    seed = _b64(key.private_bytes_raw())
    device_id = uuid.UUID(reg.json()["device_id"])
    token = (await desk.token(device_id, seed)).json()["access_token"]
    async with desk.api.storage.session() as db:
        for needle in (login.code, login.verifier, bootstrap, seed, token):
            assert not await database_contains(db, needle)
        actions = await audit_actions(db)
    for expected in ("auth.desktop_handoff.issued", "auth.desktop_handoff.redeemed", "device.registered"):
        assert expected in actions
