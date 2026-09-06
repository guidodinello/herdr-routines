---
id: "044"
title: "gc --delete has no age threshold: a branch merged minutes ago is immediately collectable"
status: open
priority: medium
area: cli
---

## Description

With issue 043 the delete half collects any `auto/*` branch that is merged and not
otherwise retained. There is no lower bound on *how recently* it merged. A pipeline
PR squash-merged five minutes ago is eligible on the next `gc --delete --yes`.

That is fine while a human types the command and reads the table first — which is
the only way it can run today. It stops being fine the moment the delete half is
scheduled, which is the direction `ROADMAP.md` points ("Worktree GC, delete half",
gated on trusting the dry-run output). An unattended gc that collects a
just-merged branch destroys the working copy someone may still have open, and the
branch pointer with it.

Retention already covers the cases that are *knowably* still in use — unmerged, open
PR, in-flight run. Age covers the case retention cannot see: a human who merged a PR
and is still poking at the worktree, or a follow-up fix about to be branched from it.

### Honest scoping

This is a **safety property, not a disk-pressure fix.** The Pi is at 5% (215 GB
free), and — correcting a figure quoted in issues 039 and 042 — herdr-routines'
worktrees were roughly 200 MB, not 2.4 GB; that measurement had included the
`fitted` project's 2.2 GB. The 2026-09-06 sweep reclaimed on the order of 100 MB.
So this issue is worth doing because unattended deletion needs a guard, not because
anything is running out of space.

Worth noting where the space actually goes, if it ever matters: the one retained
worktree is 112 MB, of which **108 MB is `.venv`**. Per-worktree cost is dominated
by virtualenvs, not repo content.

## Design

Add `--older-than DAYS` to `gc`, defaulting to something conservative (14):

- A branch is collectable only if it is merged, not retained, **and** its merge (or
  last commit, if the merge date is unavailable) is older than the threshold.
- Prefer the PR's `mergedAt` — `gc` already batches `gh pr list --state merged`
  (issue 042), so add `mergedAt` to the `--json` field list and carry it on the row.
  Fall back to the branch tip's committer date when there is no PR record.
- Show the age in the dry-run table so the reason a branch is *not* listed is
  visible, rather than it silently vanishing from the inventory.
- `--older-than 0` disables the threshold, preserving today's behaviour for a human
  who has read the table and wants it gone now.

Keep `--delete` human-invoked and `--yes`-gated regardless; this issue adds the
guard that would make scheduling it defensible, it does not schedule it.

## Acceptance criteria

1. A merged branch newer than the threshold is not collected. Test: `test_gc_delete_retains_recently_merged_branch`
2. A merged branch older than the threshold is collected. Test: `test_gc_delete_collects_branch_past_age_threshold`
3. `--older-than 0` collects regardless of age. Test: `test_gc_delete_age_threshold_zero_disables`
4. Age comes from the PR's `mergedAt` when available, tip committer date otherwise. Test: `test_gc_age_prefers_merged_at_over_commit_date`
