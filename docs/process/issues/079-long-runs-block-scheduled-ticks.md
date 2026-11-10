---
id: "079"
title: "A long manual or request run holds the tick lock and blocks every scheduled job"
status: done
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
4. [blocking] The guarantees the old lock gave stay true: no skip or refusal consumes a cron
   occurrence (074), self-update never swaps code under a running dispatch (053), and two
   jobs sharing a checkout never mutate it at once (004). Tests in
   `tests/test_dispatch_locks.py` and `tests/test_self_update.py`, listed in the spec's
   "Amendments after review" section.

## Notes

- Related: 004 (tick lock), 053 (self-update), 074, 075.

## Log

- **2026-11-06**: chose the per-job lock option (spec `20261106T050000Z`). The global
  `tick.lock` now covers only brief housekeeping and schedule-decision sections, history
  appends rotate under a sibling `history.lock`, and each job's whole synchronous dispatch
  holds `locks/job-<name>.lock`. A loser of the non-blocking job lock records `skipped`
  (`dispatch_in_flight`) instead of racing the same `rt-<job>` name; `run`/`/job` converge on
  the same lock. Collapsed-earlier-occurrence records no longer count as a scheduled terminal
  (they would hide an in-flight run from a concurrent decision), and a never-run job no longer
  invents a `missed` record for the gap since registration. Per-checkout root-mode locking
  (AC 7) is still open.
- **2026-11-09**: review fixes on PR #255; all ACs now met. Skip records carry the
  occurrence's trigger, and contention skips (`dispatch_in_flight`, `checkout_busy`) never
  count as a scheduled terminal, so neither a lost lock nor a refused manual run consumes a
  cron occurrence. The decision phase keeps main's guard order, so a tick during a job's own
  run skips without a record. Stale reaping happens only while holding the job lock. A
  firing event's watermark is written only once its dispatch starts. A manual or `/job` run
  that does not start is always a refusal: rc 1, plus a notification for `/job`.
  Self-update holds the global lock and every job's dispatch lock across the swap. AC 7 is
  done: a per-checkout lock in the state dir (not inside the checkout, which would dirty
  it) covers each sync, and covers the whole dispatch for `workspace: root`. The spec's
  "Amendments after review" section records each change and why.
