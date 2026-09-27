"""Scheduler — task-linked reminders (docs/22, PRD §22 SCHED-001).

> **A firing reminder delivers a message. It never executes.** (docs/22 §0)

There is no principal at fire time — no session is live — so this package
never calls the runtime, never activates a capability, never touches a tool or
a device operation. What it can do is bounded by what it imports: storage, the
audit writer, the usage module's scheduler quota, and three Protocols of its
own that the composition root satisfies (fire-time checks, a typed reminder
channel, a content-free wake). The import-linter contracts in `pyproject.toml`
make the rest unreachable.

* `schedule` — what a `schedule` string means (cron / ISO datetime).
* `backend` — `SchedulerBackend`, the replaceable "when is it due" store.
"""
