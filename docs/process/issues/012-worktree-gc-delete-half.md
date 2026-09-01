---
id: "012"
title: "Worktree GC, delete half"
status: done
priority: medium
area: cli
---

## Description

Opt-in deletion of what `herdr-routines gc --dry-run` (issue 002, PR #28)
lists — merged or orphaned `auto/<name>-<ts>` branches and their worktrees.
Nothing is ever removed automatically: a scheduled tool that deletes branches
unattended is explicitly not wanted. Deletion is a human-invoked
`herdr-routines gc --delete` (or `--prune`) acting on the dry-run's own
output.

## Acceptance

- `herdr-routines gc --delete` removes exactly the branches/worktrees the
  paired `--dry-run` lists, and nothing else.
- Refuses to run from an unattended/scheduled context (interactive-only, or
  requires an explicit `--yes`).
- A branch that is not merged is never deleted without an explicit
  `--force`-style opt-in.

## Log

- **2026-08-27**: curated from `ROADMAP.md` Next §. Original gate ("several
  weeks of trusting the dry-run output") waived by decision 2026-08-27; the
  dry-run has been correct across the pipeline dogfood runs so far. Phase 1
  (dry-run) is issue 002.
- **2026-08-29**: implemented and merged as PR #49 (`gc.py`: `run_gc_delete`,
  `_remove_worktree`, `_delete_branch`; `cli.py`: `gc --delete`/`--prune`
  with mandatory `--yes`; 8 acceptance tests). All 3 acceptance criteria
  met — `--delete` mirrors `--dry-run`'s own listing, always requires
  `--yes` (interactive or not, fixed during PR #49 review), and only
  deletes already-merged branches (verified against `--base` before
  deletion) unless run against a stale unmerged one is still refused.
- **2026-08-31**: status was never flipped on merge — PR #49's stage-3
  worker committed the implementation but not the `docs/process/issues/012-
  *.md` status change the orchestrator prompt calls for (`docs/pipeline/
  orchestrator-prompt.md:21-22`). Left at `open`, `pick-feature` re-picked
  this same issue the next night, producing a redundant PR (#64, closed —
  only a meta-test for the pipeline's own review-tier gate, no GC code).
  Flipping to `done` now by hand to stop it recurring; the underlying gap
  (a merged PR whose status-flip commit didn't land) is worth its own
  issue if it happens to a different issue again.
