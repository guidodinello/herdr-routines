---
id: "071"
title: "Routine review jobs record done without checking the review landed"
status: open
priority: high
area: scheduler
---

## Description

On 2026-10-24 two manual runs of the fitted reviewers ran back to back on the Pi.
`fitted-pr-review` (`fitted-pr-review-20261024T000552Z`) reviewed fitted #838. The
review is on the PR with 3 inline notes, and the label moved to `reviewed`.
`fitted-pr-review-4` (`fitted-pr-review-4-20261024T001947Z`, `opencode/big-pickle`)
was recorded `done (ok)`. Its report says it reviewed #839 "via code-review skill (PR
review with inline comments; pending-review label swapped to reviewed)". Nothing of
that is on #839: no review, no comment, label still `pending-review`. The only review
there is Copilot's quota notice. The report itself is one line with literal `\n`
escapes.

The cause is how success is judged. All five `fitted-pr-review*` jobs are plain
`routine` jobs. For those, `runner.execute_run` treats a settled agent with a
non-empty `$ROUTINE_REPORT` as `done` (`runner.py`, the `no_report` check). Whether
the review was posted is decided only by the prompt and the model's word.

The repo already has the code gate for this, just not on these jobs. `kind: review`
(issue 055, PR #159) re-reads the PR after the agent settles and requires the
job's review marker from a trusted author. Without it the target is recorded
`review_not_posted` / `review_verification_failed` and the run fails
(`tick.py`, "Post-condition (the gate analogue)"). The fitted reviewers predate it
and were never migrated. `kind: review` can't take them as it stands: `select`
picks `@me` PRs across every repo (`author`, `review_requested`), with no way to
limit it to one repo or to "oldest unreviewed first, one per run".

Same class as issue 070, where `babysit-prs` fix workers are recorded `done` on
trust: each check confirms that output was produced, not that the goal was met.

## Design

Recommended: run the fitted reviewers as `kind: review`, so one code path owns both
"review a PR" and "prove it landed".

- Add a repo scope to `kind: review`. For example a `repos: [owner/name, ...]`
  filter applied after the batched search, so `select: [author]` plus
  `repos: [guidodinello/fitted]` gives exactly fitted's own PRs.
- Add an ordering knob that preserves the fitted jobs' "oldest first" behaviour
  (today's selector caps `max_workers_per_tick` newest-last).
- Replace the five `fitted-pr-review*` job files on the Pi with one or two `kind:
  review` jobs: one per model family, staggered as today. Delete the disabled
  duplicates. `max_attempts_per_target` stays 1, so a review that failed to land is
  a reported miss, not an endless retry.

The fallback, if the migration is rejected, is a post-condition for `routine` jobs:
an opt-in `verify: review_posted` job key that runs the same `pr_review_bodies` and
marker check against the PR named in the report. That needs the marker
convention adopted in the routine prompt too.

## Acceptance criteria

1. [blocking] A review job whose agent settles without a marked review on the target
   PR is recorded `failed` with reason `review_not_posted`, never `done`, whatever its
   report says. Test: `test_review_job_without_posted_review_recorded_failed`
2. [blocking] `kind: review` can be limited to named repos, and a PR outside them is
   never selected. Test: `test_review_select_limited_to_configured_repos`
3. [blocking] The selector can order oldest-first, so the fitted jobs' "oldest open
   unreviewed PR" behaviour survives the migration. Test:
   `test_review_select_oldest_first`
4. [blocking] `deploy/jobs.d/` ships the fitted reviewer as a `kind: review` example,
   and `validate` warns about a `routine` job whose prompt runs `/code-review` (it
   bypasses the post-condition). Test: `test_validate_warns_on_routine_code_review_job`
5. [non-blocking] Flag a `$ROUTINE_REPORT` made of literal `\n` escapes as malformed
   in the run record. Cheap signal of a model that wrote the report without
   doing the work.

## Notes

- The missed review on fitted #839 was re-run by hand with `fitted-pr-review` on
  2026-10-23. Check that it landed before closing this.
- Related: issue 055 (`kind: review`), issue 070 (fix workers `done` on trust),
  issues 034/035 (gates moved from prompt prose into code).
