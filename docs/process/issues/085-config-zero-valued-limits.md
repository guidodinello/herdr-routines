---
id: "085"
title: "Decide whether 0 is a valid value for timeout and worker-limit config fields"
status: open
priority: low
area: config
---

## Description

`config._build_job` parses these with `_int_field(..., positive=False)`, which accepts 0:

- `timeout_ms` (~:1404)
- `start_timeout_ms` (~:1405)
- `max_workers_per_tick` (~:1551)
- the gated job's `max_attempts_per_target` (~:1552)

Sibling fields require a positive value, for example the check and audit `timeout_ms`
(~:1193, ~:1528) and the audit `max_attempts_per_target`. A 0 timeout or a 0 worker
cap is either a meaningful setting (such as "pause dispatch") or a typo that
currently makes a job silently do nothing.

Rejecting 0 tightens validation, and an existing `jobs.d` file that relies on it would
stop loading. Check the deployed configs before changing anything.

## Acceptance

- For each field, either reject 0 (`positive=True`) or document what 0 means next to
  the field.
- Config tests cover the decision for each field.
- `scripts/ci-check.sh` is green.

## Log

- **2026-10-26**: filed from the type-health audit's INVESTIGATE list.
