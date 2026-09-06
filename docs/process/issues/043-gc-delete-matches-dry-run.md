---
id: "043"
title: "gc --delete and gc --dry-run disagree about what is collectable"
status: done
priority: medium
area: cli
---

## Description

After issues 039 and 042, `gc --dry-run` gives an honest inventory — on the Pi it
lists 20 branches, 20 merged. `gc --delete` cannot act on 13 of them, because it
still drops every pipeline branch before evaluating candidates
(`src/herdr_routines/gc.py`, in `run_gc_delete`):

```python
# Issue 039 only restores an honest dry-run inventory; the delete half stays
# exactly as gated as before — every auto/pipeline-* branch is excluded
# unconditionally here, never just the ones pipeline_branch_retained() would flag.
rows = [r for r in rows if not r.branch.startswith(PIPELINE_PREFIX)]
```

That was the right call for 039, which deliberately scoped itself to the inventory.
The result, though, is that **the two halves of one command now disagree about what
"collectable" means**: dry-run says a merged pipeline branch with no open PR is
eligible, delete says it does not exist. A user reading the dry-run table and then
running `--delete` gets a silently different answer, with no line explaining why.

That divergence is its own trap, independent of whether anything ever gets deleted.

`run_gc_dry_run` already has the correct predicate one function away:

```python
visible_rows = [r for r in rows if not pipeline_branch_retained(root, r)]
```

## Design

Replace the prefix filter in `run_gc_delete` with the same
`pipeline_branch_retained(root, r)` call dry-run uses. One line; the two halves then
share a single definition of "still needed".

### Why this is safe with `--force`

`--force` bypasses the *merged* requirement, so it is worth being explicit:
`pipeline_branch_retained()` retains a pipeline branch that is unmerged, has an open
PR, **or** belongs to an in-flight run — and it is applied to `rows` *before*
`candidates` is derived, so a retained branch never reaches the force path at all.
`gc --delete --force --yes` therefore still cannot touch an unmerged or in-flight
pipeline branch.

### What this deliberately does not do

- **Not scheduled.** `gc --delete` stays human-invoked, still requires `--yes`, and is
  not wired into `tick`. `ROADMAP.md`'s gate for the delete half — *"several weeks of
  trusting this dry-run output"* — is about automating deletion, and today is day one
  of that output being correct. This issue removes a contradiction; it does not open
  the gate.
- **No age threshold yet.** Collecting only branches older than N days is the natural
  next step and belongs with the graduation of the delete half, not here.

### Side effect worth noting

`test_gc_delete_is_exactly_dry_run_candidates` asserts an invariant (dry-run eligible
== delete candidates) that issue 042's author correctly observed had stopped holding
for pipeline branches. This change makes it true again universally, which is the
clearest signal that the divergence is gone.

## Acceptance criteria

1. A merged pipeline branch with no open PR and no in-flight run is deleted. Test: `test_gc_delete_removes_unretained_pipeline_branch`
2. An unmerged pipeline branch survives `--delete --force --yes`. Test: `test_gc_delete_force_retains_unmerged_pipeline_branch`
3. A pipeline branch with an open PR survives. Test: `test_gc_delete_retains_pipeline_branch_with_open_pr`
4. Dry-run eligibility and delete candidacy agree for every branch, pipeline included. Test: `test_gc_delete_is_exactly_dry_run_candidates`
