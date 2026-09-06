---
id: "028"
title: "pick-feature: skip issues with an open pipeline PR"
status: done
priority: medium
area: pipeline
---

## Description

`herdr-routines pick-feature` (issue 013) is a pure-filesystem selector: it picks
the lowest-id `status: open` issue and cannot see GitHub. So when a run is still
in flight and a **later launch** fires its own pick moment, the new run re-picks
the same issue the in-flight run already has an open PR for. Both runs then
mutate the same issue file (`--mark-in-progress` flips it in the parent clone),
two orchestrators write overlapping `state.json`/reports, and the work is
duplicated.

Hit on 2026-08-31: run `20260831T012350Z` was resumed (stage 6) when the same
night's 02:00 auto-run would have re-picked its feature (**006**, still `open`
on main because the PR was unmerged). Avoided manually by keeping
`pipeline-nightly.timer` stopped — a structural guarantee it is not.

## Re-scope (2026-09-06) — read this before the original design below

Issues 041 and 040 shipped after this issue was written and **removed the mechanism
it describes.** The paragraph above says "Both runs then mutate the same issue file
(`--mark-in-progress` flips it in the parent clone)". That no longer happens:

- **041** moved the claim out of the tracked issue file into
  `src/herdr_routines/claims.py`. `pick-feature` no longer edits `$REPO_PARENT` at
  all, and `status: in-progress` is never written in-tree.
- **040** added lease expiry on top: `claimed_at` is the lease, `is_claimed` /
  `release_claim` are the primitives, and a claim older than `LEASE_HOURS` (12) with
  no open PR and no in-flight run is reclaimed automatically.

So the *file-mutation* half of this issue is already fixed, and the
`--skip-open-prs` design below would now introduce a **second source of truth**
alongside `claims.py` — one that re-derives "which issue is claimed" by parsing PR
bodies for a prose convention ("Closes issue NN — the `status: done` flip rides this
PR"). Two mechanisms answering the same question, one of them by regexing English,
is worse than the problem.

### What is actually still open

The real remaining gap is narrow: **`claims.py` knows about claims it made, and
nothing else.** An issue whose claim was legitimately released — lease expired, or
the claims file was lost — can still be re-picked while its PR sits open awaiting
human review. 040's note calls this out directly: "if the claims file is lost, an
in-flight issue becomes re-pickable immediately."

### Re-scoped design

Teach the **existing** claim check to consult open PRs, rather than adding a
parallel flag and convention:

- `is_claimed(issue_id)` (or the selection filter that calls it) additionally treats
  an issue as claimed when an open `auto/pipeline-*` PR references it.
- Derive the reference from the branch/run rather than PR prose where possible —
  `state.json` already records `feature_source`, and the run id is in the branch
  name. Fall back to the PR body convention only if nothing structural is available,
  and say so in a comment.
- **Fail-open is still right**, and the original issue argued it well: if
  `gh`/remote resolution errors, warn to stderr and pick anyway. A transient GitHub
  outage must never brick the nightly run.
- Batch the `gh` call — `gc.py` already does exactly this
  (`fetch_merged_pr_heads`); follow that shape rather than one call per issue.

No new CLI flag. The behaviour should be unconditional: there is no scenario where
re-picking an issue with a live PR is wanted.

## Acceptance criteria

**These describe the re-scoped design above.** Everything below the "Original design
(superseded)" heading is kept for its reasoning only — do not implement it, and in
particular do not add a `--skip-open-prs` flag or parse issue ids out of PR bodies.

1. An issue with an open `auto/pipeline-*` PR is not picked, even when `claims.py` holds no claim for it. Test: `test_pick_feature_skips_issue_with_open_pipeline_pr`
2. The exclusion is unconditional — no flag is required to enable it. Test: `test_open_pr_exclusion_needs_no_flag`
3. A non-pipeline open PR never excludes an issue. Test: `test_non_pipeline_pr_does_not_exclude`
4. A `gh` or git-remote failure warns and picks anyway (fail-open, exit 0). Test: `test_pick_feature_fails_open_on_gh_error`
5. The open-PR lookup is a single batched call, not one per issue. Test: `test_open_pr_lookup_is_batched`

### Original design (superseded — kept for the reasoning, not the plan)

## Design (proposal, superseded)

`pick-feature --skip-open-prs`:

- Lists open PRs via `gh pr list --repo <owner>/<repo> --state open --json
  number,headRefName,body`, deriving `owner/repo` from the cwd's
  `git remote get-url origin` (pick-feature always runs in `$REPO_PARENT`).
- Filters to PRs whose `headRefName` starts with `auto/pipeline-`.
- Extracts claimed issue ids from the pipeline PR body convention
  ("Closes issue NN — the `status: done` flip rides this PR", stage-4 template).
- Excludes those ids from selection without touching the issue files.
- **Fail-open**: if git-remote resolution or `gh` errors, warn to stderr and
  pick anyway — a transient gh outage must never brick the nightly run.
  (Issue 027 proved the failure-investigation budget is already tight.)

Files: `src/herdr_routines/pick_feature.py`, `src/herdr_routines/cli.py` (flag
wiring), `tests/test_pick_feature.py`, `docs/pipeline/orchestrator-prompt.md`
(the self-select invocation gains `--skip-open-prs`).

### Acceptance (superseded — belongs to the design above, not the re-scope)

- With an open `auto/pipeline-*` PR whose body closes issue N, `pick-feature`
  returns the next eligible issue, not N.
- Non-pipeline open PRs (head not under `auto/pipeline-*`) never exclude an issue.
- Missing `git remote` or `gh` failure → warning + normal pick (exit 0).
- The orchestrator-prompt self-select uses the flag: a resumed run's feature is
  never re-picked by a later launch.

## Log

- **2026-08-31**: filed from run `20260831T012350Z` collision analysis (resume
  vs. the same night's 02:00 auto-run). Design proposed in-session: pregnant
  `--skip-open-prs`, cheap, fail-open.
