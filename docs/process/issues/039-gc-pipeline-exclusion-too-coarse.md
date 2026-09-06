---
id: "039"
title: "gc excludes every auto/pipeline-* branch, so 23 merged worktrees are uncollectable"
status: done
priority: low
area: cli
---

## Description

`herdr-routines gc --dry-run` on the Pi reports **0 eligible branches** while 23
`auto/pipeline-*` worktrees (2.4 GB) sit under `~/.herdr/worktrees/herdr-routines/`,
most of them for PRs that merged days or weeks ago.

The cause is a blanket exclusion in `list_auto_branches` (`src/herdr_routines/gc.py`):

```python
PIPELINE_PREFIX = "auto/pipeline-"
...
return sorted(n for n in names if n and not n.startswith(PIPELINE_PREFIX))
```

G-14 ("future `gc` must exclude `auto/pipeline-*`") is quoted in
`docs/pipeline/orchestrator-prompt.md` and was right when written: the orchestrator
deliberately retains its worktree at end of run, and collecting the branch would
destroy the very thing the run produced.

But that rationale only holds **while the PR is open**. Once the implementing PR
merges, an `auto/pipeline-*` branch is exactly as collectable as any other merged
`auto/*` branch — and `gc` already has a merged-into-base check it applies to
every other branch. The exclusion is keyed on the wrong property: *"is this a
pipeline branch"* rather than *"is this branch still needed"*.

Net effect: `gc` — the tool whose entire job is inventorying stale `auto/*`
branches — is structurally blind to the largest and fastest-growing category of
them, and has been reporting a reassuring `0 branch(es) listed` while the pile
grew to 23.

### Not urgent

The Pi is at 5% disk (215 GB free), so this is housekeeping, not pressure. It is
filed because `gc` currently gives a *misleading* answer, which is worse than
giving none — and because the pile is what made issue 036's collision a
guaranteed rather than occasional failure.

## Design (proposal)

Narrow the exclusion from "all pipeline branches" to "pipeline branches still in
use":

- Drop the unconditional `PIPELINE_PREFIX` filter from `list_auto_branches`.
- In the eligibility check, treat an `auto/pipeline-*` branch as **retained** when
  it is unmerged into base, **or** has an open PR (`gh pr list --head <branch>`),
  **or** is the branch of a currently in-flight run (`state.json` / a live
  `pl-*` agent). Otherwise it is eligible exactly like any other merged `auto/*`.
- Keep the delete half gated as it is today — this issue only restores an honest
  dry-run inventory.

Update the G-14 wording in `docs/pipeline/orchestrator-prompt.md` in the same
change, so the prompt and the code stop disagreeing about what gc is allowed to do.

## Acceptance criteria

1. A merged `auto/pipeline-*` branch with no open PR is listed by `gc --dry-run`. Test: `test_gc_lists_merged_pipeline_branch`
2. An unmerged `auto/pipeline-*` branch is not listed. Test: `test_gc_retains_unmerged_pipeline_branch`
3. A merged `auto/pipeline-*` branch with an open PR is not listed. Test: `test_gc_retains_pipeline_branch_with_open_pr`
4. Non-pipeline `auto/*` behaviour is unchanged. Test: `test_gc_non_pipeline_branches_unaffected`
