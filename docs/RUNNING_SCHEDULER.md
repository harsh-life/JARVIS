# Running the scheduler (task-linked reminders)

Contract: `docs/22_SCHEDULER.md`. Decisions implemented: `docs/DECISION_REGISTER.md` §2D.

> **A firing reminder delivers a message. It never executes.** Anything that
> happens after a reminder happens because the user started a task.

## 1. What it does

- A user creates a reminder with their own words (`task_reason`) and a
  schedule, either directly (`POST /api/v1/jobs`) or by asking the agent inside
  an ordinary `execute` task (`scheduler.create.create_reminder`).
- When it comes due, the server re-checks the owner, the job and (for a
  graph-scoped job) the owner's membership. If all still hold, it sends the
  owner's own words to the owner's own devices.
- On the phone the reminder is a notification. "Start task" opens JARVIS with
  the words in the task box. The user presses Send, and that is a normal task,
  authorized like any other.

What it never does: call the runtime, activate a capability, run a tool, send a
device operation, create a confirmation, or act for a user who is not present.
`pyproject.toml`'s contracts make those imports impossible from `server/scheduler`.

## 2. Configuration

```yaml
scheduler:
  enabled: true                    # false → /jobs answers 503 {dependency: scheduler}
  backend: apscheduler_db          # the only backend shipped
  misfire_grace_minutes: 60
  max_active_jobs_per_user: 50
  agent_tool_enabled: true         # the agent path (OD-SCH-1); the API and firing are unaffected
  poll_seconds: 30
  min_recurrence_minutes: 5
  max_horizon_days: 730
  max_task_reason_chars: 1000
  pending_delivery_ttl_hours: 72
security:
  rate_limits:
    scheduler_creations_per_hour: 20
```

Delivery to phones needs `android.enabled: true` (the device channel). Without it,
reminders are recorded, queued, and expire after `pending_delivery_ttl_hours`.
Nothing is lost silently: every step is audited.

Run the migration once: `alembic upgrade head` (revision `b6d4f8a2c1e7`).

## 3. Schedules

| Form | Example | Meaning |
|---|---|---|
| ISO-8601 with offset | `2026-10-01T09:00:00+05:30` | once. A naive datetime is refused. |
| cron (UTC) | `0 9 * * 1-5` | weekdays 09:00 UTC |
| cron in a zone | `CRON_TZ=Europe/London 0 8 * * 1` | Mondays 08:00 London time, DST-aware |

## 4. API

| Call | Result |
|---|---|
| `POST /api/v1/jobs {task_reason, schedule, graph_id?}` | `201` job (`private`, owned by the caller); `422` empty or long reason, or a bad schedule; `429 {limit}` over quota; `404` for a graph you are not in |
| `GET /api/v1/jobs?limit&cursor&include_inactive` | your jobs, plus graph-visible jobs in graphs you belong to; `last_firing` reports `delivered` / `queued` / `undeliverable` / `missed` / `cancelled_recheck` |
| `GET /api/v1/jobs/{id}` | one job, or `404` |
| `DELETE /api/v1/jobs/{id}` | `403 confirmation_required` + token, then `204` with `X-Confirmation-Token`; owner only |

`Idempotency-Key` is honoured on `POST`.

## 5. Firing, misfires, restarts

- The runner starts with the app (a lifespan service). Its first pass is at
  startup, so jobs that came due while the server was down are handled at once.
- An occurrence missed by less than `misfire_grace_minutes` is delivered and
  marked late ("missed at 09:00" on the phone).
- An older one is recorded as `missed` and audited, and it shows in `GET /jobs`.
  For a recurring job, every missed occurrence is folded into one row. The job
  then resumes at its next occurrence.
- Each occurrence fires at most once: a unique `(job_id, scheduled_for)`.

## 6. Delivery and privacy

- Only the owner's own non-revoked devices get a reminder, even when the job is
  graph-visible.
- Offline devices receive it on reconnect: re-checked at send time, and re-sent
  until the phone acknowledges it.
- A third-party push (not wired yet, OD-AND-5) could carry only
  `{"type":"wake"}`. The words travel over JARVIS's own authenticated socket.
- Audit rows name job, firing and delivery ids, never the reminder's words. On
  the lock screen the phone shows "Reminder" only.

## 7. Tests

```
python -m pytest tests/scheduler -q          # SCH-T1..T7, API, firing, restart, contracts
lint-imports --config pyproject.toml         # includes the scheduler's two contracts
cd android && ./gradlew :contract:test :app:testDebugUnitTest   # reminder frames, inbox, channel
```
