"""dashboard — the operator console's read-only views (28_DASHBOARD_OPERATOR_CONSOLE.md).

> The dashboard shows. Controls live elsewhere. (28 §0)

  * `console`   — the ten views of 28 §3 and the persistent banner, as reads
  * `ports`     — the read-only live snapshots it needs, satisfied by the
                  composition root (`server/composition/console.py`)
  * `redaction` — server-side secret and PII redaction (DASH-005/006)

`[LOCKED]` DASH-002: read-only over gated results, no mutation path, no secret
resolution — enforced by the import contracts "The dashboard is read-only" and
"Dashboard cannot import secrets", and by a check that no module here writes
through a session (`tests/dashboard/test_console_readonly.py`). The HTTP routes
(`GET /api/v1/admin/*`, superuser only) live in `server/gateway/routers/admin.py`;
operator *actions* are the separate `/api/v1/admin/control/*` routes owned by
the subsystems that enforce them (18, 19, 20). This package holds no superuser
authority itself (pyproject: "Only the gateway reaches superuser authority").
"""
