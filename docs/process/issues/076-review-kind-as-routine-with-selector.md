---
id: "076"
title: "Rebuild `kind: review` as a routine with a multi-target, cross-repo selector"
status: done
priority: medium
area: scheduler
---

## Description

After issues 071 (verify gates) and 073 (selectors), `kind: review` (issue 055) is a
routine job plus three things only it has:

1. **Its own selection:** a batched `@me` GraphQL search across every repo
   (`select: [author, review_requested]`, `include_auto_branches`), with the
   marker/attempt-budget re-review guards (`review_scan.py`).
2. **Several targets per run:** up to `max_workers_per_tick` reviews in one tick.
3. **A checkout per target repo**, created lazily under the managed repos dir.

Its post-condition already goes through `verify.review_posted` (#226). Its selection,
fan-out and per-repo checkout are the last pieces of review-specific dispatch. As
long as they live there, a review job can't be built from the general parts, and
`kind` keeps a value that isn't a different engine.

## Design

- **Selector `my_open_prs`** (name to taste): the review scan's search and skip rules as
  a selector in `target_selectors.py`. It returns an ordered list of targets.
  `oldest_unreviewed_pr` keeps returning at most one.
- **Fan-out on routine jobs:** `max_targets_per_run` (default 1). With more than one
  target, the routine runs one agent per target, sequentially in the occurrence. Each
  target gets its own `running`/terminal per-target record and verify gates, using the
  per-target record shape `kind: review` writes today, so `status` and the digest keep
  working.
- **Per-target checkout:** when a selected target's repo differs from the job's `repo`,
  use the managed per-repo checkout `kind: review` uses today
  (`repos.default_repos_dir()`).
- `kind: review` then becomes config sugar that loads as `kind: routine` with
  `pick: my_open_prs`, `verify: [review_posted]`, the engine-injected review prompt and
  `max_targets_per_run`. Existing review job files load unchanged. Or retire the kind
  with a migration note, whichever the plan justifies better.
- Re-review guards stay: the content marker (already the selector's "reviewed"
  predicate) and the per-target attempt budget.

## Acceptance criteria

1. [blocking] `deploy/jobs.d/review-my-prs.yaml` loads unchanged and selects the same
   PRs, in the same order, with the same skip reasons as before. Test:
   `test_review_job_selection_unchanged_after_migration`
2. [blocking] A routine with `max_targets_per_run: 3` and a multi-target selector runs
   up to three agents in one occurrence, each verified on its own target. Test:
   `test_routine_fans_out_over_targets`
3. [blocking] A target in a different repo runs in that repo's managed checkout. Test:
   `test_cross_repo_target_uses_managed_checkout`
4. [blocking] `_process_review_job` and its review-only dispatch are gone, or reduced
   to the config sugar. The review path is the routine path. Test:
   `test_review_kind_dispatches_through_routine_path`
5. [blocking] Per-target history records keep their current shape (`status`/digest
   tests pass unmodified).

## Notes

- Depends on 074 (shared run path). Can run in parallel with 075 if the two plans keep
  apart: 075 owns triggers/config entry, 076 owns selection/fan-out.
- Related: 055, 071, 073.
