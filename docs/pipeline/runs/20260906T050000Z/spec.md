# Spec — pick-feature: skip issues with an open pipeline PR (028)

## Problem
`herdr-routines pick-feature` picks the lowest-id `status: open` issue and cannot see GitHub. A later launch can re-pick an issue that an in-flight run already has an open `auto/pipeline-*` PR for. Both runs then mutate overlapping state and duplicate work. Issue 041 moved the claim out of the tracked file, but `claims.py` knows only about claims it made — a released or lost claim makes the issue re-pickable while its PR is still open (issue 028 re-scope).

## Approach
Teach the existing claim check to consult open PRs, rather than adding a parallel flag. `pipeline_open_pr_issue_ids()` performs a single batched `gh pr list --state open --json headRefName,title,body` call, filters to `auto/pipeline-*` branches, derives the issue id structurally from `state.json:feature_source` when possible (branch `auto/pipeline-<run_id>` → `state.json` under `worktrees_root/auto-pipeline-<run_id>/state.json` → `feature_source`), and falls back to PR title/body prose parsing only when no structural record exists. The exclusion is unconditional and fail-open (warn to stderr, pick anyway on `gh` failure). `select_next()` now accepts `pipeline_pr_ids` and excludes `int(issue.id)` in that set alongside `claimed_ids`. `run_pick_feature()` calls the new helper once per pick (batched, not per issue) and merges the result with `claimed_ids` for selection. No new CLI flag.

## Files touched
- `src/herdr_routines/pick_feature.py` — add `pipeline_open_pr_issue_ids()`, extend `select_next()` to accept `pipeline_pr_ids`, update `run_pick_feature()` to consult open pipeline PRs unconditionally
- `tests/test_pick_feature.py` — add tests for pipeline PR skipping, non-pipeline exclusion, fail-open, batched lookup (if needed for verification, existing 21 tests remain green)

## Risks
- `gh` not installed / not authenticated / rate-limited → fail-open must warn but still pick (never brick nightly run)
- Worktree `state.json` missing or malformed → fallback to body parsing, never guess
- Structural derivation case sensitivity (herdr lowercases worktree dir) → check both variants
- Two `gh` calls per pick when `mark=True` and stale claims exist (reclaim + pipeline) — acceptable (both batched, not per-issue); when no stale claims, only one call

