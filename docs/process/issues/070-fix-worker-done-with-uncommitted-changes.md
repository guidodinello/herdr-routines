---
id: "070"
title: "babysit-prs records a fix worker as done when it left uncommitted changes, then wedges on them"
status: open
priority: high
area: pipeline
---

## Description

On PR #211, the first `babysit-prs` fix worker (`babysit-prs-20261021T060000Z`,
attempt 0) edited `cli.py`, `review_scan.py` and `tick.py` in the retained pipeline
worktree. It settled with `final_agent_status: done` and never committed, pushed or replied to a thread.
History recorded `state: done`.

The next two ticks reused the same worktree (`_find_existing_worktree`) and
`_worktree_reuse_check` refused it:

    reason: worktree_reuse_not_clean
    error: existing worktree …/auto-pipeline-20261021t050000z for
           auto/pipeline-20261021T050000Z: worktree has uncommitted changes

Both refusals count as attempts. After 3 the PR logs `max_attempts_exceeded` every
30 minutes and never recovers. The worker's own leftovers caused the dirty tree,
and it holds no orchestrator edit that the check exists to protect.

So there are two bugs:

1. **`done` is taken on trust.** Nothing checks that a fix worker actually changed the
   PR: a new commit on the remote head, or a reply on each thread it was given.
2. **A dirty tree left by our own worker blocks every later attempt.** A
   `worktree_reuse_not_clean` refusal that never launched an agent should not count
   against the attempt budget, and it should not be retried blindly.

## Acceptance criteria

1. [blocking] After a fix worker settles `done`, its worktree is checked. If it has
   uncommitted changes, or the remote head SHA has not moved since dispatch, the
   attempt is recorded `failed` with reason `worker_left_uncommitted_changes` or
   `worker_pushed_nothing`. Test: `test_fix_worker_done_with_dirty_worktree_recorded_failed`
2. [blocking] A `worktree_reuse_not_clean` refusal does not count toward the
   attempt budget (`_COUNTABLE_STATES` / the reason allow-list), because no agent ran.
   Test: `test_worktree_reuse_not_clean_does_not_consume_attempt`
3. [blocking] Uncommitted changes left by our own fix worker are reported in the tick
   report with the worktree path, so a human can rescue or discard them. They are
   never auto-discarded. Test: `test_dirty_worktree_surfaced_in_babysit_report`
4. [non-blocking] Consider whether the fix-worker prompt should end with an explicit
   commit/push/reply checklist. It must not stand in for the code check in
   criterion 1.

## Notes

- Related: issue 036 (reusing the retained pipeline worktree), and the gate blind
  spots noted in issue 035: each gate checks that output was produced, not that it
  achieved the goal.
- Clean up the worktree that triggered this
  (`~/.herdr/worktrees/herdr-routines/auto-pipeline-20261021t050000z` on the Pi) by
  hand. Its edits are superseded by `387baab`, which merged in #211.
