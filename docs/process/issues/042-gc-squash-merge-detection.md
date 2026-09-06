---
id: "042"
title: "gc's merge check uses ancestry, so squash-merged branches never look merged"
status: done
priority: medium
area: cli
---

## Description

Issue 039 removed `gc`'s blanket `auto/pipeline-*` exclusion so merged pipeline
branches could be collected. **It did not change the outcome on the Pi.** Verified
2026-09-06 immediately after deploying `017e5cd`:

```
$ uv run herdr-routines gc --dry-run
BRANCH  WORKTREE-EXISTS  MERGED-INTO-BASE
0 branch(es) listed (dry-run, nothing deleted; eligible: 0, merged: 0, missing worktree: 0)
```

Still zero, with 23 stale worktrees (2.4 GB) on disk.

> **Correction (2026-09-06, after this issue shipped as PR #92):** the command
> above was run from `~/projects/herdr-routines` — the *runner* checkout, which
> holds **0** `auto/*` branches. `gc --repo` defaults to the current directory, and
> the 21 stale branches live in the *work-target* clone at
> `~/.local/state/herdr-routines/repos/herdr-routines`. `0 branch(es) listed` was
> the correct answer to a question about the wrong repository, so **this was not
> valid evidence that issue 039 had failed.**
>
> The reasoning in this issue stands on its own without it: `merge-base
> --is-ancestor` genuinely cannot detect a squash merge, which is how every PR in
> this repo lands, and 039's implementing agent flagged the gap independently.
> But the empirical claim was wrong and is corrected here rather than quietly left
> in the record.
>
> Pointed at the right clone after #92 shipped, `gc --dry-run` lists **20 branches,
> 20 merged**. Whether #90 alone would have produced that table was never
> established — it would need a revert to find out.

The cause is the merge check itself. `is_merged` uses
`git merge-base --is-ancestor <branch> <base>`, which answers *"are this branch's
commits reachable from base?"* **This repository squash-merges** — every PR lands
as a single new commit on `main` with no second parent, so the branch's own commits
are never ancestors of base. Every squash-merged branch reads as unmerged, forever.

So `gc`'s inventory is not just incomplete for pipeline branches — the
`MERGED-INTO-BASE` column is structurally wrong for **every** `auto/*` branch in a
squash-merge repo. Issue 039 fixed the prefix filter sitting in front of a check
that was already returning the wrong answer.

### Why this was not caught

Every acceptance criterion in issue 039 passes, `tests/test_gc.py` is green, and CI
is green — because the tests construct branches with real ancestry, which is the one
topology this repo never produces. The gap is between "the unit under test behaves
as specified" and "the command achieves its purpose on the actual repository". The
only signal was running `gc --dry-run` against the real Pi.

(The implementing agent flagged this in its own PR description as a follow-up. It is
filed here rather than left in a merged PR body.)

## Design (proposal)

Ancestry is the wrong primitive for a squash-merge repo. Options:

1. **Ask GitHub.** `gh pr list --head <branch> --state merged --json number` — the
   authoritative answer to "did this branch's PR land", independent of topology.
   `gc` already shells out to `gh` for the open-PR check added by 039, so this adds
   no new dependency. Costs a network call per branch; batch with a single
   `gh pr list --state merged --limit N` and match on `headRefName`.
2. **Patch-equivalence.** `git cherry base branch` reports commits whose patch is
   already present upstream — catches squash merges without a network call, but is
   fuzzy for branches whose content was reworked during review.
3. **Both**: `cherry` as a cheap local pre-filter, `gh` as the authority before
   anything is listed as collectable.

Prefer (1) for correctness and (3) if the per-branch cost matters. Whichever lands,
rename or re-document the `MERGED-INTO-BASE` column so it states what it actually
checked.

Keep `gc --delete` gated exactly as it is; this issue is about the inventory
telling the truth.

## Acceptance criteria

1. A squash-merged `auto/*` branch is reported as merged. Test: `test_gc_detects_squash_merged_branch`
2. A squash-merged `auto/pipeline-*` branch with no open PR is listed as eligible. Test: `test_gc_lists_squash_merged_pipeline_branch`
3. A branch whose PR is still open is not reported merged. Test: `test_gc_open_pr_branch_not_merged`
4. A genuinely unmerged branch with no PR is not reported merged. Test: `test_gc_unmerged_branch_not_reported_merged`
