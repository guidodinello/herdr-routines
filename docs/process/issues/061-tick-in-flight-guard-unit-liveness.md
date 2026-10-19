---
id: "061"
title: "Tick's pipeline in-flight guard should check systemd unit liveness"
status: done
priority: medium
area: pipeline
gate: the type-gaps refactor PRs that edit tick.py are merged (same as 060)
---

## Description

PR #187 taught the pipeline watchdog not to reap a run whose systemd unit
(`herdr-pipeline-<run_id>*`) is still active: a resumed run gets a fresh unit
and deadline, but its recorded deadline stays the old one, so it can look
stalled while alive.

`tick._pipeline_run_is_live` still decides from
`pipeline_watchdog.is_stalled` alone. A live-but-quiet resumed run therefore
reads as dead there, and the tick could launch a second pipeline night on top
of it.

The unit-name prefix is also duplicated as a literal in three places
(`tick.py`, `cli.py` and `pipeline_watchdog.PIPELINE_UNIT_GLOB`). If one
drifts, the liveness check matches no unit and reads "inactive".

## Acceptance

- `_pipeline_run_is_live` treats a run as live when
  `pipeline_watchdog.pipeline_unit_active` returns `True` or `None` (can't
  tell). Only `False` plus `is_stalled` means dead.
- One shared constant for the `herdr-pipeline-` unit prefix, used by tick, cli
  and the watchdog.
- Tests: an active unit with a stale heartbeat blocks a second launch; an
  inactive unit with a stale heartbeat does not.

## Log

- **2026-10-19**: found while fixing the watchdog (PR #187); deferred because
  `tick.py` was owned by the concurrent type-gaps run.
- **2026-10-19**: fixed in this PR: tick's guard now keeps a stalled run in flight
  while its unit is active or unknown, and the `herdr-pipeline-` prefix is one shared
  constant (`pipeline_watchdog.PIPELINE_UNIT_PREFIX`).
