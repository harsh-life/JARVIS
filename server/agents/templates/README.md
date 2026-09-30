# Agent templates (docs/29 §5)

`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]` — these files are the shipped v1
catalog of docs/29 §5.5. An operator enables a subset with
`agents.enabled_templates`; with `agents.enabled: false` (the default) none is
used.

A template is the operator-approved **maximum** shape of a class of agent. It
limits authority; it never grants any. The owner's live capability grants,
task activation, the mode ceiling, the template's `risk_ceiling` and the
authorization engine (`04`) still decide every operation an agent attempts.

## Format

One YAML file per template, named `<template_id>.yaml`, validated against
`shared/schemas/agent_factory.py::AgentTemplate` (unknown keys are an error).
Loading is fail-closed: one invalid file stops the server from starting.

## Review checklist (every new or changed template is a pull request)

- [ ] `abilities` ⊆ the ability table (`server/agents/abilities.py`); none maps
      to `agent.*`, `system.restricted`, `device.*`, `app.interact`, a floor
      name, a delete, or `net.request.post` (the loader refuses these).
- [ ] `risk_ceiling` justified; every capability-backed ability's registry tier
      is at or below it (the loader refuses otherwise).
- [ ] `run_mode` justified: `observe` for read-only classes; `execute` only
      where writes are the point of the class. A non-`execute` template's
      `risk_ceiling` is `low_read`.
- [ ] `memory_policy` allows exactly the runtime-owned abilities listed
      (memory, vault, notebook). `user_memory_write` is always `never`.
- [ ] `unattended_supported` is template-level permission only; unattended runs
      stay unavailable until docs/29 §15 (OD-AF-2) is ratified and built.
- [ ] `version` bumped on any change: a bump sends every spec built on an older
      version through revalidation (docs/29 §5.3, AGENT-T25).
- [ ] Tests added or updated in `tests/agents/`.
