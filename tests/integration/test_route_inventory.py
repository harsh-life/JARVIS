"""Route inventory (pilot plan step M2 exit: "route-inventory test green; all
new routes isolated").

Every route the production composition root serves is enumerated, and each
must be on the short public list below or refuse a request that carries no
token. The operator's routes (`/admin/*`, `/control/*`) additionally refuse an
ordinary user's token. A route added later is covered automatically: it either
authenticates, or this test fails until it is consciously added as public.
"""

from __future__ import annotations

import re
import uuid

from server.gateway.app import API_V1_PREFIX
from server.gateway.routers.auth import public_router

V1 = API_V1_PREFIX
PUBLIC = {
    ("GET", f"{V1}/health"),
    ("GET", f"{V1}/ready"),
    ("GET", f"{V1}/auth/oidc/start"),
    ("GET", f"{V1}/auth/oidc/callback"),
    ("GET", "/app/login"),
    ("GET", "/.well-known/assetlinks.json"),
}
# Authenticated by a credential other than an access token: the bootstrap
# token (device registration) and the device proof (token issuance) — both
# refuse a request that carries neither.
OTHER_CREDENTIAL = {
    ("POST", f"{V1}/devices"),
    ("POST", f"{V1}/sessions/token"),
}
OPERATOR = re.compile(rf"^{re.escape(V1)}/(admin|control)(/|$)")

# The pilot's M2 additions, by name, so dropping one is noticed too.
M2_ROUTES = {
    ("GET", f"{V1}/me"),
    ("GET", f"{V1}/devices"),
    ("GET", f"{V1}/agent/tasks"),
    ("GET", f"{V1}/usage"),
    ("DELETE", f"{V1}/memory"),
    ("GET", f"{V1}/memory/export"),
    ("GET", f"{V1}/config/presentation"),
    ("PUT", f"{V1}/config/presentation"),
    ("DELETE", f"{V1}/config/presentation"),
    ("GET", f"{V1}/config/model/catalog"),
    ("GET", f"{V1}/config/model"),
    ("PUT", f"{V1}/config/model"),
    ("DELETE", f"{V1}/config/model"),
    ("POST", f"{V1}/secrets"),
    ("DELETE", f"{V1}/secrets/{{key_ref}}"),
}


def _routes(app) -> set[tuple[str, str]]:
    routes = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }
    routes |= {(m, r.path) for r in public_router.routes for m in r.methods if m != "HEAD"}
    return routes


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)


async def test_every_route_is_public_by_name_or_refuses_an_anonymous_request(make_harness):
    h = await make_harness(config={"agents": {"enabled": True}, "memory": {"enabled": False}})
    routes = _routes(h.app)
    assert M2_ROUTES <= routes, sorted(M2_ROUTES - routes)
    assert PUBLIC <= routes, sorted(PUBLIC - routes)
    leaking = []
    for method, path in sorted(routes - PUBLIC):
        resp = await h.client.request(method, _concrete(path), json={})
        # A route whose credential travels in the body refuses a body without it.
        refused = (401, 403, 422) if (method, path) in OTHER_CREDENTIAL else (401, 403)
        if resp.status_code not in refused:
            leaking.append((method, path, resp.status_code))
    assert leaking == [], leaking


async def test_operator_routes_refuse_an_ordinary_users_token(make_harness):
    h = await make_harness()
    alice = await h.user("alice")
    operator = sorted(r for r in _routes(h.app) if OPERATOR.match(r[1]))
    assert operator, "expected operator routes to exist"
    for method, path in operator:
        resp = await h.client.request(method, _concrete(path), json={}, headers=alice.auth)
        assert resp.status_code in (401, 403), (method, path, resp.status_code)


def test_the_public_list_is_exactly_what_the_proxies_may_answer_without_a_token():
    # Liveness, readiness, the two OIDC legs and the two App Link files — nothing
    # that reads or changes a user's data.
    assert len(PUBLIC) == 6 and all(method == "GET" for method, _ in PUBLIC)
    assert OTHER_CREDENTIAL.isdisjoint(PUBLIC)
