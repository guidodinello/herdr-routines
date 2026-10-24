---
id: "077"
title: "Move `gated` jobs' `github_event` checks onto `trigger.event`"
status: open
priority: low
area: scheduler
---

## Description

Issue 075 (#233) added `trigger.event`: a polled `github_event` query that fires any job,
deduped through issue 014's `last_seen_event_ids` watermark. `gated` jobs still carry
their own `github_event` *checks* (issue 014), evaluated only when the job's cron fires,
with their own watermark and per-event attempt budget. 075 rejects `trigger.event` on a
gated job that has such a check, because the two would write the same watermark.

Two mechanisms now answer "did a matching event happen?", and the gated kind keeps one
of them to itself, which is the special-casing 071's split exists to remove.

## Design

- A gated job's `github_event` check becomes `trigger.event` plus the job's existing fix
  payload. The per-event attempt budget (`attempt_count_for_event`) and 070's
  dirty-worktree rule must survive the move.
- Existing gated job files load unchanged (config sugar, the same way 076 handles
  `kind: review`), or the plan justifies a one-time migration of the Pi's `babysit-prs`
  file.
- Decide whether event delivery stays at-most-once (075) or keeps 014's retry-on-failure
  for gated jobs, and say why.

## Acceptance criteria

1. [blocking] A gated job with a `github_event` check behaves the same before and after:
   same events fire, same skips, same attempt budget. Pinned by a golden test committed
   on the unmodified code first (076's approach). Test:
   `test_gated_event_check_behaviour_unchanged_after_migration`
2. [blocking] Only one watermark writer per job remains. Test:
   `test_single_event_watermark_per_job`

## Notes

- Related: 014, 075, 076 (golden-test pattern), 070.
