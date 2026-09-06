---
id: "047"
title: "gc's age threshold is defeated by branches that never commit — a 3-day-old branch reports 15.2 days"
status: done
priority: high
area: cli
---

## Description

Issue 044's age threshold is the safety guard that makes an unattended
`gc --delete` defensible. It does not work for the most common kind of `auto/*`
branch this system produces.

`branch_age_days` falls back to the branch tip's committer date when no PR
`mergedAt` is available. For a branch that **never commits**, the tip *is* the base
commit it was cut from — so the reported age is the age of the base commit, not of
the branch.

Review jobs are exactly this shape: they create a worktree/branch, post a review to
GitHub, and commit nothing. Measured on the Pi against `guidodinello/fitted`,
2026-09-06:

```
auto/fitted-pr-review-4-20260903T120000Z   tip 230de86  tipdate 2026-08-21  age 15.2
auto/fitted-pr-review-20260824T090000Z     tip 230de86  tipdate 2026-08-21  age 15.2
auto/fitted-pr-review-5-20260825T130000Z   tip 230de86  tipdate 2026-08-21  age 15.2
```

Three branches created on three different dates, all pointing at **the same commit**,
all reporting the same age. 43 of 47 branches in that repo report an identical
`15.2`.

The first of those was created **three days ago** and reports 15.2 days, comfortably
past the 14-day default. **The threshold passes it.** A branch created *today* by a
review job would also report 15.2 and be immediately collectable — precisely the
scenario issue 044 exists to prevent.

The logic is not wrong by its own docstring ("or since its tip commit when no PR
record exists"); the *definition* is wrong for this branch shape. The docstring even
anticipates the direction of the error — "a branch's tip commit can predate its
merge by any amount" — without noticing that for a no-commit branch it predates the
branch's own creation.

## Design (proposal)

The tip commit is the wrong fallback. Better signals, in order:

1. **The PR's `mergedAt`** — unchanged, still the most honest answer when present.
2. **The timestamp embedded in the branch name.** Every branch this tool collects is
   `auto/<job>-<RUN_ID>` with `RUN_ID` a UTC stamp (`20260903T120000Z`). That is the
   branch's creation time by construction, it needs no git or network call, and it is
   exactly "when did this run happen".
3. **The branch's reflog creation entry** (`git reflog show --date=iso <branch>` tail)
   where the name carries no stamp.
4. Tip committer date only as a last resort, and only when it is *later* than the
   base's — i.e. when the branch actually committed something.

Given every branch `gc` is allowed to touch matches `auto/*`, (2) covers essentially
the whole population and should be the primary fallback.

Also: `_age_str` renders unknown as `n/a` and `branch_age_days` returns `inf` so an
unreadable age "can never itself block a threshold-gated delete". Reconsider that
default. Unknown age failing *open* is the wrong direction for a destructive
operation — an unknown-age branch should be retained, not collected.

## Acceptance criteria

1. A branch whose tip is the unchanged base commit takes its age from the run stamp in its name, not the tip date. Test: `test_branch_age_prefers_run_stamp_over_base_tip`
2. Two branches cut from the same base on different dates report different ages. Test: `test_branch_ages_differ_for_same_base_commit`
3. A recently created no-commit branch is retained by the default threshold. Test: `test_recent_noncommit_branch_survives_age_threshold`
4. An unreadable age retains the branch rather than collecting it. Test: `test_unknown_age_retains_branch`
