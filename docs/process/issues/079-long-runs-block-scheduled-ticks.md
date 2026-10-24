---
id: "079"
title: "A long manual or request run holds the tick lock and blocks every scheduled job"
status: open
priority: low
area: scheduler
---

## Description

`herdr-routines run` and Telegram `/job` (issues 074, 075) take the tick lock for the whole
run, the same as a tick that runs a routine job synchronously. A 30–60 minute agent run
therefore pauses every other scheduled job. On 2026-10-23 a manual reviewer run held the
lock while `babysit-prs` skipped silently. A request run is refused outright while a tick
holds the lock. Self-update waits only 5 minutes for the lock (#219) before deferring.

## Design (to be decided in the plan)

The lock protects the shared history file, the `rt-<job>` agent names and the job
checkouts. Options:
- a per-job lock for dispatch plus a short global lock for history appends and schedule
  decisions, so unrelated jobs keep running;
- or run long dispatches detached, as `kind: pipeline` already does, with the tick
  reconciling their result.

Whichever is chosen must keep the guarantees issue 004 introduced the lock for.

## Acceptance criteria

1. [blocking] A long run of job A does not stop job B from being dispatched on schedule.
   Test: `test_long_run_does_not_block_other_jobs`
2. [blocking] Two dispatches of the same job never overlap. Test:
   `test_same_job_never_dispatched_concurrently`
3. [blocking] History appends stay atomic under concurrent writers. Test:
   `test_concurrent_history_appends_are_atomic`

## Notes

- Related: 004 (tick lock), 053 (self-update), 074, 075.
