# Spec — pick-feature: skip issues with an open pipeline PR (028)

## Problem
`herdr-routines pick-feature` picks the lowest-id `status: open` issue and cannot see GitHub. A later launch can re-pick an issue that an in-flight run already has an open `auto/pipeline-*` PR for. Both runs then mutate overlapping state and duplicate work. Issue 041 moved the claim out of the tracked file, but `claims.py` knows only about claims it made — a released or lost claim makes the issue re-pickable while its PR is still open (issue 028 re-scope).

## Approach
Teach the existing claim check to consult open PRs, rather than adding a parallel flag. `pipeline_open_pr_issue_ids()` performs a single batched `gh pr list --state open --json headRefName,title,body` call, filters to `auto/pipeline-*` branches, derives the issue id structurally from `state.json:feature_source` when possible (branch `auto/pipeline-<run_id>` → `state.json` under `worktrees_root/auto-pipeline-<run_id>/state.json` → `feature_source`), and falls back to PR title/body prose parsing only when no structural record exists. The exclusion is unconditional and fail-open (warn to stderr, pick anyway on `gh` failure). `select_next()` now accepts `pipeline_pr_ids` and excludes `int(issue.id)` in that set alongside `claimed_ids`. `run_pick_feature()` calls the new helper once per pick (batched, not per issue) and merges the result with `claimed_ids` for selection. No new CLI flag.

## Files touched
- `src/herdr_routines/pick_feature.py` — add `pipeline_open_pr_issue_ids()`, extend `select_next()` to accept `pipeline_pr_ids`, update `run_pick_feature()` to consult open pipeline PRs unconditionally
- `tests/test_pick_feature.py` — existing 21 tests remain green; new behavior covered by manual verification and pipeline gates

## Risks
- `gh` not installed / not authenticated / rate-limited → fail-open must warn but still pick (never brick nightly run)
- Worktree `state.json` missing or malformed → fallback to body parsing, never guess
- Structural derivation case sensitivity (herdr lowercases worktree dir) → check both variants
- Two `gh` calls per pick when `mark=True` and stale claims exist (reclaim + pipeline) — acceptable (both batched, not per-issue); when no stale claims, only one call

## Acceptance criteria
1. `Test: test_pick_feature_skips_issue_with_open_pipeline_pr` — An issue with an open `auto/pipeline-*` PR is not picked, even when `claims.py` holds no claim for it. blocking — confidence: high
2. `Test: test_open_pr_exclusion_needs_no_flag` — The exclusion is unconditional — no flag is required to enable it. blocking — confidence: high
3. `Test: test_non_pipeline_pr_does_not_exclude` — A non-pipeline open PR never excludes an issue. blocking — confidence: high
4. `Test: test_pick_feature_fails_open_on_gh_error` — A `gh` or git-remote failure warns and picks anyway (fail-open, exit 0). blocking — confidence: medium
5. `Test: test_open_pr_lookup_is_batched` — The open-PR lookup is a single batched call, not one per issue. blocking — confidence: medium
6. `Test: test_select_next_with_pipeline_ids` — `select_next` correctly excludes pipeline PR ids alongside claimed ids. non-blocking — confidence: high
7. `Test: test_pipeline_structural_derivation` — Pipeline PR with `state.json` feature_source derives issue id structurally even with empty body. non-blocking — confidence: medium

## Changelog v1→v2
- Added `## Acceptance criteria` with 7 numbered items each ending `Test: <name>` and `blocking`/`non-blocking` + `confidence:` tiers
- Clarified re-scoped design rationale (structural vs fallback prose)
- No file list change
