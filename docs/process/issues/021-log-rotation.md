---
id: "021"
title: "Log rotation"
status: done
priority: low
area: infra
---

## Description

`history.jsonl` and the reports directory grow unbounded. A handful of jobs
writing a few JSONL lines a day won't matter for years
(`docs/plan-v1.md` §5), but a simple, mechanical rotation/retention is cheap
to add now and removes a "someday" chore.

Scope: size- or age-based rotation of `history.jsonl` (roll to
`history-YYYYMM.jsonl` or similar) and an age-based prune of the reports
directory, both configurable, both off or generous by default.

## Acceptance

- `history.jsonl` rotates on a configurable size/age threshold; `history` /
  `ps` / `scheduled` still read across the rolled files transparently.
- Reports older than a configurable window can be pruned by an explicit
  command; nothing is deleted automatically without opt-in.

## Log

- **2026-08-27**: curated from `ROADMAP.md` Later §. Original gate
  ("history.jsonl size becoming noticeable") waived 2026-08-27 — small,
  mechanical, and worth having in place before it's needed rather than after.
- **2026-09-28**: implemented, in two halves that are asymmetric on purpose, because the
  risk of each is different:
  1. **Rotation is automatic and non-destructive.** `history.maybe_rotate` renames
     `history.jsonl` aside past a configurable size/age threshold and starts a fresh empty
     live file; `run_tick` calls it at the end of every tick under the same best-effort
     contract as the `/tmp` reap, so a rotation error can never stop a job dispatching.
     Rolls are named `history-<UTC ts>.jsonl` rather than the issue's suggested
     `history-YYYYMM.jsonl`: three rolls in one month must still read back in write order,
     and a month bucket would return that month backwards and feed a wrong `first_seen_at`
     into `schedule.decide`. `read_job`/`status`/`history` read live-plus-rolls in that
     order, so rotation is invisible to every reader.
  2. **Deletion is explicit only.** `herdr-routines prune {reports,history}` is the sole
     delete path in the state dir and requires `--yes`, mirroring `gc --delete`'s refusal
     and rc 2. Reports belonging to an in-flight pipeline run are protected regardless of
     age (reusing `ps`'s own predicate: in flight == final report not yet written), since
     deleting that `state.json` would make `ps` drop the row and let `pick_feature` re-pick
     the same issue. A finished run's report is collectable — otherwise the command could
     never reclaim anything.
- **2026-09-28**: both thresholds default to off and the reports window to 90 days, so an
  operator's existing config loads unchanged with no migration. Configured in one place
  under a top-level `retention:` block, or `jobs.d/retention.yaml` in the per-file layout
  (excluded from job discovery like `defaults.yaml`). `docs/plan-v1.md` §5 "No log rotation
  in v1" and the §Out-of-scope list entry were corrected in place rather than left to
  contradict a shipped feature.
- **2026-09-28**: deliberately *not* wired to a timer. `prune history` at 365 days is
  reachable only by name, because deleting the earliest roll can make `first_seen_at`
  return `None` for a long-lived job, which re-registers it and shifts the
  `job_registered_at` fed into `schedule.decide`. Documented in
  `reports_prune.prune_rolled_history` with the fix (exclude the most recent roll first) if
  anyone wires it up later.