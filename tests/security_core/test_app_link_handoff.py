"""docs/23 §3 — the browser → app return after Google login (App Link).

The bootstrap token is the same single-use, register-only token as before; what
changes is the delivery: a 302 to the app's verified App Link with the token
in the URL fragment (never sent to any server), plus an app-generated nonce the
app checks, so an app accepts only the login it started.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio

from server.config.schema import AndroidAppLinksConfig, AndroidConfig
from server.gateway.app import API_V1_PREFIX, create_app
from server.gateway.security import build_security_core
from server.secrets.kek import resolve_kek
from tests.security_core.conftest import TEST_KEK_ENV_VAR, Api, LocalOIDCProvider, make_test_config

FINGERPRINT = ":".join(["AB"] * 32)
BASE = "https://jarvis.example.test"
APP_STATE = "a" * 20 + "B" * 20 + "_-" * 4


@pytest_asyncio.fixture
async def linked_api(storage, kek_value):
    provider = LocalOIDCProvider()
    core = build_security_core(make_test_config(), oidc_provider=provider)
    async with storage.session() as session:
        await core.secret_store.bootstrap(session, resolve_kek(f"env:{TEST_KEK_ENV_VAR}"))
        await session.commit()
    android = AndroidConfig(
        app_links=AndroidAppLinksConfig(base_url=BASE, sha256_cert_fingerprints=[FINGERPRINT])
    )
    app = create_app(storage=storage, security=core, android_config=android)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield Api(client=client, provider=provider, core=core, storage=storage)


async def _start(api, app_state: str | None):
    params = {"app_state": app_state} if app_state is not None else {}
    return await api.client.get(f"{API_V1_PREFIX}/auth/oidc/start", params=params)


async def _callback(api, start):
    state = parse_qs(urlparse(start.json()["redirect_url"]).query)["state"][0]
    return await api.client.get(
        f"{API_V1_PREFIX}/auth/oidc/callback",
        params={"code": "test-authorization-code", "state": state},
        follow_redirects=False,
    )


async def test_an_app_started_login_returns_through_the_app_link(linked_api):
    start = await _start(linked_api, APP_STATE)
    assert start.status_code == 200, start.text
    callback = await _callback(linked_api, start)
    assert callback.status_code == 302
    assert callback.headers["cache-control"] == "no-store"
    location = urlparse(callback.headers["location"])
    assert f"{location.scheme}://{location.netloc}" == BASE
    assert location.path == "/app/login"
    # The token is only in the fragment — never in the path or query a server
    # would log.
    assert location.query == ""
    fragment = parse_qs(location.fragment)
    assert fragment["app_state"] == [APP_STATE]
    bootstrap = fragment["bootstrap_token"][0]

    # It is the ordinary single-use bootstrap token.
    device_id, _ = await linked_api.register_device(bootstrap)
    again = await linked_api.client.post(
        f"{API_V1_PREFIX}/devices", json={"platform": "android"},
        headers={"Authorization": f"Bearer {bootstrap}"},
    )
    assert again.status_code == 401


async def test_a_login_without_app_state_keeps_the_json_contract(linked_api):
    start = await _start(linked_api, None)
    callback = await _callback(linked_api, start)
    assert callback.status_code == 200
    assert set(callback.json()) == {"needs_device_registration", "bootstrap_token"}
    assert callback.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("bad", ["short", "x" * 129, "a" * 40 + "/", "a" * 40 + "#frag"])
async def test_a_malformed_app_state_is_refused(linked_api, bad):
    assert (await _start(linked_api, bad)).status_code == 422


async def test_app_state_is_refused_when_app_links_are_not_configured(api):
    assert (await _start(api, APP_STATE)).status_code == 422


async def test_asset_links_name_exactly_the_configured_app(linked_api):
    resp = await linked_api.client.get("/.well-known/assetlinks.json")
    assert resp.status_code == 200
    assert resp.json() == [
        {
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": "com.hypermind.jarvis",
                "sha256_cert_fingerprints": [FINGERPRINT],
            },
        }
    ]


async def test_asset_links_are_absent_until_configured(api):
    assert (await api.client.get("/.well-known/assetlinks.json")).status_code == 404


async def test_the_fallback_page_cannot_read_the_fragment(linked_api):
    resp = await linked_api.client.get("/app/login")
    assert resp.status_code == 200
    assert "<script" not in resp.text.lower()
    assert resp.headers["content-security-policy"] == "default-src 'none'"
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "links",
    [
        {"base_url": "http://jarvis.example.test"},
        {"base_url": "https://jarvis.example.test/path"},
        {"sha256_cert_fingerprints": ["ab:cd"]},
        {"package_name": "not a package"},
    ],
)
def test_app_link_configuration_is_validated(links):
    with pytest.raises(ValueError):
        AndroidAppLinksConfig(**links)


# ── the cached app policy (docs/23 §5.2) ────────────────────────────────


async def test_the_app_policy_is_served_to_an_authenticated_device_only(storage, kek_value):
    from server.config.schema import AndroidAppClassificationConfig

    provider = LocalOIDCProvider()
    core = build_security_core(make_test_config(), oidc_provider=provider)
    async with storage.session() as session:
        await core.secret_store.bootstrap(session, resolve_kek(f"env:{TEST_KEK_ENV_VAR}"))
        await session.commit()
    android = AndroidConfig(
        app_classification=AndroidAppClassificationConfig(
            non_sensitive=["com.example.notes"], sensitive=["com.bank.app"], payment=["com.wallet.pay"]
        )
    )
    app = create_app(storage=storage, security=core, android_config=android)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        api = Api(client=client, provider=provider, core=core, storage=storage)
        assert (await client.get(f"{API_V1_PREFIX}/devices/app-policy")).status_code == 401
        phone = await api.onboard("alice")
        resp = await client.get(f"{API_V1_PREFIX}/devices/app-policy", headers=phone.auth)
        assert resp.status_code == 200
        assert resp.json() == {
            "non_sensitive": ["com.example.notes"],
            "sensitive": ["com.bank.app"],
            "payment": ["com.wallet.pay"],
        }
