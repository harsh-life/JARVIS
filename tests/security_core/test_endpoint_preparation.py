"""Endpoint-facing boundaries a desktop client would meet (docs/24 preparation).

docs/24 is `[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]`. These tests pin the
*current* server behaviour at the points docs/24 proposes to change, so the
change is made deliberately, against a failing test, once the owner rules. No
test here ratifies a proposal. Where a test pins a value an open decision
would change, it names the decision; ratifying it means editing that test in
the same commit as the change.

* Device registration accepts only the registry's platforms (`android`) and
  no endpoint-profile field — OD-EP-2 (`credential_alg`) and OD-EP-9
  (`endpoint_class`, `desktop_*`) are open. A refused registration does not
  spend the user's login.
* The login result cannot be steered anywhere by the caller: no `return_to`
  or `redirect_uri` parameter exists (docs/24 §9.2 proposes a loopback
  `return_to`; not built). There is no open redirect to close later.
* The wire types a client sends carry no identity, authority or profile
  claim (PHONE-003): such a field is unrepresentable, not ignored.
* The authorization engine never reads a device's platform, class, client
  version or advertised features — docs/24 §16's "the profile is an
  advertisement, not a grant" already holds structurally.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import ValidationError

from server.gateway.app import API_V1_PREFIX
from server.gateway.routers.agent import ConfirmRequest, SubmitTaskRequest
from server.gateway.routers.auth import DeviceRegistrationRequest, DeviceRotationRequest
from shared.schemas.device_channel import DeviceHello, DeviceReauth
from shared.schemas.enums import DevicePlatform

REPO = Path(__file__).resolve().parents[2]


# ── device registration ─────────────────────────────────────────────────


def test_the_registry_has_exactly_one_device_platform():
    """Current state for OD-EP-9 / docs/24 §10.3: no desktop value exists.
    `linux` in `ExecutionPlatform` is the *server host* (docs/24 §2.2 item 3)
    and is deliberately not a device platform."""

    assert [p.value for p in DevicePlatform] == ["android"]


@pytest.mark.parametrize(
    "platform", ["desktop", "desktop_windows", "desktop_macos", "desktop_linux", "linux", "server", "browser"]
)
async def test_an_unregistered_platform_is_refused_without_spending_the_login(api, platform):
    bootstrap = await api.oidc_login()
    refused = await api.client.post(
        f"{API_V1_PREFIX}/devices", json={"platform": platform},
        headers={"Authorization": f"Bearer {bootstrap}"},
    )
    assert refused.status_code == 422

    # Validation happens before the bootstrap token is consumed.
    device_id, _ = await api.register_device(bootstrap)
    assert isinstance(device_id, uuid.UUID)


@pytest.mark.parametrize(
    "claim",
    [
        {"endpoint_class": "desktop"},
        {"credential_alg": "ecdsa_p256"},
        {"profile": {"execution": ["file.read"]}},
        {"capabilities": ["file.read"]},
        {"user_id": str(uuid.uuid4())},
    ],
)
async def test_registration_carries_no_profile_or_identity_claim(api, claim):
    """OD-EP-2 / OD-EP-9 open: no such field is accepted today, so nothing a
    client advertises can reach the device row."""

    bootstrap = await api.oidc_login()
    refused = await api.client.post(
        f"{API_V1_PREFIX}/devices", json={"platform": "android", **claim},
        headers={"Authorization": f"Bearer {bootstrap}"},
    )
    assert refused.status_code == 422
    await api.register_device(bootstrap)


# ── the login handoff ───────────────────────────────────────────────────


async def test_the_caller_cannot_choose_where_the_login_result_goes(api):
    """docs/24 §9.2 proposes a loopback `return_to` for native endpoints. It
    does not exist: a caller-supplied return target is ignored, the provider
    is sent the server's own callback, and the callback answers with JSON —
    never a redirect to a caller-chosen place."""

    hostile = {
        "return_to": "http://127.0.0.1:53682/cb",
        "redirect_uri": "https://attacker.example/cb",
        "redirect": "https://attacker.example/cb",
    }
    start = await api.client.get(f"{API_V1_PREFIX}/auth/oidc/start", params=hostile)
    assert start.status_code == 200, start.text
    redirect_url = start.json()["redirect_url"]
    for value in hostile.values():
        assert value not in redirect_url
    provider_query = parse_qs(urlparse(redirect_url).query)
    assert provider_query["redirect_uri"] == [f"http://test.invalid{API_V1_PREFIX}/auth/oidc/callback"]

    state = provider_query["state"][0]
    callback = await api.client.get(
        f"{API_V1_PREFIX}/auth/oidc/callback",
        params={"code": "test-authorization-code", "state": state, **hostile},
        follow_redirects=False,
    )
    assert callback.status_code == 200
    assert "location" not in callback.headers
    assert set(callback.json()) == {"needs_device_registration", "bootstrap_token"}


async def test_a_bootstrap_token_is_not_an_access_token(api):
    """03 §3.2's register-only scope, which a native endpoint's handoff must
    keep: the token that comes back from login authorizes nothing else."""

    bootstrap = await api.oidc_login()
    headers = {"Authorization": f"Bearer {bootstrap}"}
    task = await api.client.post(
        f"{API_V1_PREFIX}/agent/tasks", json={"input": "hello"},
        headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
    )
    assert task.status_code == 401
    assert (await api.client.get(f"{API_V1_PREFIX}/graphs", headers=headers)).status_code == 401
    # Still unspent: the failed uses above did not consume it.
    await api.register_device(bootstrap)


# ── wire types carry no claims ──────────────────────────────────────────

_CLAIMS = {
    "user_id": str(uuid.uuid4()),
    "device_id": str(uuid.uuid4()),
    "session_id": str(uuid.uuid4()),
    "endpoint_class": "desktop",
    "platform": "desktop_windows",
    "capabilities": ["file.read"],
    "risk_category": "low_read",
    "authorized": True,
    "step_up_fresh": True,
}

_VALID = [
    (DeviceHello, {"access_token": "t", "device_proof": "p", "mapping_version": "1", "client_version": "1"}),
    (DeviceReauth, {"access_token": "t"}),
    (SubmitTaskRequest, {"input": "hello"}),
    (ConfirmRequest, {"confirmation_token": "t", "approve": True}),
    (DeviceRegistrationRequest, {}),
    (DeviceRotationRequest, {}),
]


# A claim that is one of a model's declared fields (e.g. the registration's
# `platform`, validated against the registry above) is not an extra claim.
_FRAME_CLAIMS = [
    pytest.param(model, valid, claim, id=f"{model.__name__}-{claim}")
    for model, valid in _VALID
    for claim in sorted(_CLAIMS)
    if claim not in model.model_fields
]


@pytest.mark.parametrize("model,valid,claim", _FRAME_CLAIMS)
def test_client_frames_cannot_carry_identity_authority_or_profile(model, valid, claim):
    model.model_validate(valid)
    with pytest.raises(ValidationError):
        model.model_validate({**valid, claim: _CLAIMS[claim]})


# ── the engine never reads the endpoint's self-description ──────────────

_ENGINE = ("server/graph", "server/capabilities")
_ENDPOINT_SELF_DESCRIPTION = {
    "DevicePlatform", "platform", "endpoint_class", "client_version", "ChannelFeature", "features",
    "DeviceHello", "credential_alg",
}


def _names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add(node.name.rsplit(".", 1)[-1])
    return found


@pytest.mark.parametrize("package", _ENGINE)
def test_the_authorization_engine_never_reads_what_an_endpoint_says_about_itself(package):
    """docs/24 §16 (`[LOCKED-in-spirit]`, PHONE-003): an endpoint's platform,
    class, version and advertised features may one day be used for routing;
    never for authorization. Today the engine references none of them."""

    files = sorted((REPO / package).rglob("*.py"))
    assert files
    offenders = {
        str(path.relative_to(REPO)): sorted(_names(path) & _ENDPOINT_SELF_DESCRIPTION)
        for path in files
        if _names(path) & _ENDPOINT_SELF_DESCRIPTION
    }
    assert offenders == {}
