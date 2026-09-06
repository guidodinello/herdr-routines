---
id: "041"
title: "pick-feature's in-progress flip is an uncommitted edit to a file the merge also touches, so it wedges sync-repo"
status: done
priority: high
area: pipeline
---

## Description

`pick-feature --mark-in-progress` writes `status: in-progress` into the issue file
in `$REPO_PARENT` as an **uncommitted working-tree modification**. The implementing
PR later changes the *same line* of the *same file* to `status: done`, and that
lands on `main`.

Those two facts guarantee a collision. Once the PR merges, the parent clone has a
local modification to a file that also changed upstream, and git refuses to
fast-forward:

```
$ git merge --ff-only origin/main
error: Your local changes to the following files would be overwritten by merge:
	docs/process/issues/027-tmp-hygiene.md
Please commit your changes or stash them before you merge.
Aborting
```

`sync-repo` correctly exits 1. But `sync-repo` is:

- **Prerequisite 1 of the orchestrator prompt** — "non-zero exit ⇒ stop and write a
  report saying so; never branch off a stale/diverged `$REPO_PARENT`". So the
  nightly pipeline hard-stops.
- **`ensure_repo`, called by every routine job before every run.** So every job on
  that repo path fails `repo_sync_failed` until a human intervenes.

This is not hypothetical. It is the `repo_sync_failed` seen across 2026-09-03 and
09-04, and it recurred and was cleared by hand on 2026-09-06 with two files stuck
(`027`, `028`). Left alone it takes down the whole schedule for that repo.

The mechanism is *worse on success* than on failure: a successful run guarantees
the upstream change that collides with its own uncommitted flip. It only stays
quiet while the flip and the merge happen to be cleaned up in the right order.

## Design (proposal)

**Stop writing pipeline bookkeeping into a tracked working-tree file.** The run
already maintains `state.json` (`feature_source` records exactly which issue was
picked); that is the natural and already-atomic home for "this issue is claimed".

- `pick-feature` records the claim out-of-tree — `state.json`, or a small
  claims file under `~/.local/state/herdr-routines/` — instead of editing the
  issue file in `$REPO_PARENT`.
- The `open → done` flip stays exactly where it is: committed by the implementer,
  carried by the PR, landing atomically on merge. That half works and should not
  change.
- `$REPO_PARENT` returns to being a clean mirror of `origin/main`, which is what
  `sync-repo` and `ensure_repo` assume.

If the flip must stay in-tree for visibility, the fallback is to **commit** it in
the parent rather than leave it dirty — but that trades a sync wedge for a diverged
parent, which issue 030 already went to some trouble to prevent. Out-of-tree is the
better trade.

Closely related to issue 040: out-of-tree claims also make the stale-lease
reclamation there far easier, since releasing a claim stops meaning "edit a tracked
file in a checkout you do not own".

## Acceptance criteria

1. `pick-feature --mark-in-progress` leaves `$REPO_PARENT` clean. Test: `test_pick_feature_leaves_parent_clone_clean`
2. `sync-repo` fast-forwards after a pick, including after the PR merges the done flip. Test: `test_sync_repo_ff_after_pick_and_merge`
3. A claimed issue is still skipped by the next pick. Test: `test_claimed_issue_not_repicked`
4. The PR-carried `done` flip still closes the issue on merge. Test: `test_done_flip_still_rides_the_pr`
