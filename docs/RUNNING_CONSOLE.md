# Running the operator console (docs/28)

> The dashboard shows. Controls live elsewhere. (28 §0)

The console is a JSON API on the gateway, `GET /api/v1/admin/*`, for the
**superuser principal only** (`Authorization: Superuser <token>`, the
out-of-band `HYPERMIND_SUPERUSER_TOKEN`). An ordinary Bearer token — or any
other credential — is refused `401` before any handler runs (DASH-003/004).
No browser UI ships in this build (OD-DASH-2).

## 1. Views

| `GET /api/v1/admin/…` | Shows |
|---|---|
| `banner` | the persistent banners only |
| `health[?probe_models=true]` | database, SecretStore locked/unlocked, configured model providers (probed only on request — a probe is a call to the provider), Mem0, vault, scheduler, device channel, Judge |
| `tasks[?status=&limit=]` | running / paused / terminal counts; live tasks; per-task status, mode, counters, failure code, worker switches |
| `recovery` | worker switches, stalls, trips by source, recent trips, the global latch |
| `break-glass` | enablement, live records (task, user, executables, reason, expiry), activation/invocation/end events |
| `evaluations` | Judge state and queue, scores and findings, the improvement-candidate queue, config-version history |
| `usage` | per-user and global calls and spend (24 h), rate usage (1 min), Judge spend separately, the limits |
| `memory` | facts per user (counts only), vault index state |
| `devices` | registered devices, connected, last seen, revoked, step-up/push registered (never the key or token) |
| `audit[?action=&user_id=&result=&resource_prefix=&since=&limit=]` | audit events — ids and results; `action=evaluation.*` filters by prefix |
| `configuration` | the effective configuration; every secret as `{"handle", "resolves"}` |

Every view answers `{"view", "banners", "data"}`. **Banners** persist on every
view while break-glass is enabled (`break_glass_enabled`) or a global stop is
latched (`global_stop_latched`).

## 2. What is never shown

* **Secret values** (DASH-005): a configured credential appears only as its
  handle and whether it resolves, decided from metadata (a non-revoked
  SecretStore reference row, an environment variable's presence). Nothing is
  resolved to build a view. Any other string that looks like a credential
  (e.g. a password inside `database_url`) is masked.
* **User content** (DASH-006) by default: task responses, evaluator notes,
  candidate text and applied values appear as `{"redacted": true, "chars": n}`.

To read one task's content, use the separate, privileged view — the access is
written to the audit trail (`console.unredacted_view`, with your reason)
*before* anything is read, and secret-shaped text is still masked:

```bash
curl "$BASE/api/v1/admin/privileged/tasks/$TASK_ID?reason=support_case" \
  -H "Authorization: Superuser $HYPERMIND_SUPERUSER_TOKEN"
```

## 3. Acting

The console's own routes cannot change anything (every one is `GET`, asserted
when the gateway starts, and `server/dashboard` can reach no mutation path).
Actions are the control endpoints, owned by the subsystems that enforce them:

| Action | Endpoint | Owner |
|---|---|---|
| stop a task / user / device | `POST /api/v1/admin/control/stop` | supervisor (18 §5.4) |
| global stop / clear | `POST /api/v1/admin/control/global-stop`, `…/global-clear` | supervisor |
| break-glass activate / revoke / list | `POST|GET /api/v1/admin/control/break-glass…` | break-glass (20 §2.2) |
| Judge on/off, stop requests on/off | `POST /api/v1/admin/control/evaluation/switches` | evaluation control (19) |
| approve / reject a candidate, roll back a version | `POST /api/v1/admin/control/evaluation/…` | review queue (19 §9) |

## 4. Verifying

```bash
python -m pytest tests/dashboard -q
lint-imports --config pyproject.toml   # "The dashboard is read-only", "Dashboard cannot import secrets", …
```
