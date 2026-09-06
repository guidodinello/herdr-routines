---
id: "040"
title: "A run that dies before opening a PR permanently orphans the issue it picked"
status: done
priority: high
area: pipeline
---

## Description

`pick-feature --mark-in-progress` flips the chosen issue `open → in-progress` in
`$REPO_PARENT` so a later run does not re-pick work already underway. The flip is
released exactly one way: the implementing PR carries the `in-progress → done`
commit, which lands on merge.

**There is no release path for a run that never opens a PR.** If the orchestrator
dies at stage 1, aborts on a gate, hits its deadline, or settles `blocked` (as on
2026-09-05), the issue is left at `in-progress` forever. `pick-feature` skips it on
every subsequent night. The issue is silently retired without anyone deciding to
retire it.

Observed 2026-09-06: `docs/process/issues/028-pick-feature-skip-open-prs.md` sat at
`in-progress` in the Pi's parent clone with **no open PR and no in-flight run**. It
had been picked by a run that never produced a PR, and would never have been picked
again. Nothing surfaced this — no report, no notification, no log line. It was found
only while investigating an unrelated sync failure.

Note the compounding effect with issue 041: the flip is an *uncommitted* working-tree
change, so an orphaned issue also permanently blocks `sync-repo` once main touches
the same file.

## Design (proposal)

Prefer a release path that does not depend on the dying process still being alive
to run it — that is the failure mode, so an "orchestrator reverts on abort" handler
is exactly the code least likely to run.

Options, roughly in order of robustness:

1. **Treat `in-progress` as a lease with an expiry.** `pick-feature` considers an
   `in-progress` issue re-pickable when it has no open PR and no in-flight run and
   its flip is older than N hours (a `picked_at:` timestamp written alongside the
   status, or the file's git mtime). Self-healing; needs no cooperation from the
   crashed run.
2. **Reconcile at tick.** The same reconciler that already parses a run's
   `## Outcome:` marker releases the flip when the run ends without a PR. Bounded
   and observable, but only fires when `tick` learns the run ended.
3. **Revert on abort in the orchestrator.** Cheapest to write, least reliable —
   does not cover the silent-death case that motivated this issue.

Whichever is chosen, an orphaned pick should be *visible*: a line in the run report
and a `herdr notification show` when a flip is released, so a human learns the issue
went back in the pool.

## Acceptance criteria

1. An `in-progress` issue with no open PR and no in-flight run becomes re-pickable. Test: `test_pick_feature_reclaims_stale_in_progress`
2. An `in-progress` issue with an open PR stays skipped. Test: `test_pick_feature_skips_in_progress_with_open_pr`
3. An `in-progress` issue belonging to an in-flight run stays skipped. Test: `test_pick_feature_skips_in_progress_inflight_run`
4. Releasing a stale flip is reported, not silent. Test: `test_reclaimed_pick_is_surfaced`
