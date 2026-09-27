"""`python -m tests.tools.export_grant_samples [export|check]`.

Writes `shared/android/grant_samples.json`: the capability-grant wire shapes
the Android per-app grid uses (PRD §13, docs/23 §5.2) — a `POST /capabilities`
request body exactly as the server's `GrantRequest` accepts it, and a
`GET /capabilities` listing exactly as `GrantListResponse` renders it. The
Android client encodes and parses these with its strict models
(android/contract GridGrantsTest); the request is also posted to the real
endpoint (tests/security_core/test_grid_grants.py).
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from server.gateway.routers.capabilities import GrantListResponse, GrantRequest
from shared.schemas.capability import CapabilityGrant
from shared.schemas.enums import CapabilityScopeType

PATH = Path(__file__).resolve().parents[2] / "shared" / "android" / "grant_samples.json"

USER = uuid.UUID("6f1c2d3e-4a5b-4c6d-8e7f-00000000b001")
DEVICE = uuid.UUID("6f1c2d3e-4a5b-4c6d-8e7f-00000000d001")
WHEN = datetime(2026, 9, 27, 12, 0, 30, tzinfo=timezone.utc)


def _grant(n: int, capability: str, scope_type: CapabilityScopeType, principal: uuid.UUID,
           resource_scope: dict | None, **fields) -> CapabilityGrant:
    return CapabilityGrant(grant_id=uuid.UUID(f"6f1c2d3e-4a5b-4c6d-8e7f-00000000c{n:03d}"), principal_id=principal,
                           scope_type=scope_type, capability=capability, resource_scope=resource_scope,
                           granted_by=USER, created_at=WHEN, **fields)


def request() -> dict:
    body = GrantRequest(capability="app.interact", scope_type=CapabilityScopeType.DEVICE, scope_id=DEVICE,
                        resource_scope={"package_name": "com.example.notes"})
    return body.model_dump(mode="json", exclude_none=True)


def listing() -> dict:
    notes = {"package_name": "com.example.notes"}
    items = [
        # Grid-managed: this device, exactly one app.
        _grant(1, "device.read", CapabilityScopeType.DEVICE, DEVICE, notes),
        _grant(2, "app.interact", CapabilityScopeType.DEVICE, DEVICE, notes, expires_at=WHEN),
        # Not the grid's: a user-wide grant, an unscoped device grant, a
        # non-device capability. The grid never revokes these.
        _grant(3, "device.read", CapabilityScopeType.USER, USER, notes),
        _grant(4, "device.read", CapabilityScopeType.DEVICE, DEVICE, None),
        _grant(5, "file.read", CapabilityScopeType.DEVICE, DEVICE, None),
    ]
    return GrantListResponse(items=items).model_dump(mode="json")


def render_file() -> str:
    doc = {"device_id": str(DEVICE), "grant_request": request(), "grant_list": listing()}
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "check"
    if command == "export":
        PATH.write_text(render_file(), encoding="ascii")
        print(f"wrote {PATH}")
        return 0
    ok = PATH.exists() and PATH.read_text(encoding="ascii") == render_file()
    print("grant samples up to date" if ok else "grant samples stale — run export")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
