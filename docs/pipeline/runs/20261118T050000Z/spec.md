# Spec v1 — issue 086: Enforce GitHub's 100-result ceiling on search_prs limit

## Problem

`GhClient.search_prs(modes, limit: int = 100)` (`auto_fix.py` protocol ~:115, real impl ~:344)
and `review_scan.scan_review_targets` (~:122) / `classify_review_candidates` (~:258) pass
`limit` into the GraphQL search as `first: {limit}` (auto_fix.py:352). The 100 bound exists
only in prose: a comment at auto_fix.py:887 ("GitHub's own ceiling") and the CLI's inline
`1 <= args.limit <= 100` check (cli.py:1742). Nothing at the library layer enforces it — a
future caller passing `limit=200` would build a query GitHub rejects, failing at GitHub
instead of locally. Latent today (every caller uses the default), per the issue.

## Approach

Option A from the issue: one module constant plus a `ValueError` guard. (Not option B —
removing the parameter would ripple through the `GhClient` protocol and every test fake
implementing `search_prs(..., limit: int = 100)` for zero functional gain.)

1. **Define once**: add `REVIEW_SEARCH_MAX_RESULTS = 100` in `auto_fix.py` next to
   `REVIEW_SEARCH_QUALIFIERS` (~:889), replacing the prose-only comment.
2. **Guard at the lowest level**: `RealGhClient.search_prs` (auto_fix.py:344) raises
   `ValueError` when `not 1 <= limit <= REVIEW_SEARCH_MAX_RESULTS`, before any query is
   built. This is the funnel every real search passes through, so the bound holds for all
   future callers without re-validating at each layer.
3. **Wire the defaults**: the `limit: int = 100` defaults in
   `auto_fix.search_prs` (protocol + impl) and `review_scan.scan_review_targets` /
   `classify_review_candidates` become `limit: int = REVIEW_SEARCH_MAX_RESULTS` so the
   number is defined in exactly one place.
4. **CLI references the constant**: `review-scan --limit`'s default (cli.py:619) and its
   validation message/range (cli.py:1742-1746) use `REVIEW_SEARCH_MAX_RESULTS` instead of
   the literal `100`/prose. Behaviour unchanged (its existing test,
   `test_review_scan_limit_validation_and_forwarding`, must stay green).
5. **Tests**: in `tests/test_auto_fix.py`, assert `RealGhClient.search_prs` raises
   `ValueError` for `limit=0`, `limit=101`, negative — with no `gh` invocation — and does
   not raise at the boundary (`1`, `100`).

## Files touched

- `src/herdr_routines/auto_fix.py` — constant + guard + defaults + comment (~4 sites).
- `src/herdr_routines/review_scan.py` — default at :122 and :258 reference the constant.
- `src/herdr_routines/cli.py` — `--limit` default and validation reference the constant.
- `tests/test_auto_fix.py` — new guard tests.

Out of scope: the separate `pr_list(..., limit=100)` calls (auto_fix.py:568, :1504) —
`gh pr list`'s limit is a local truncation choice, not GitHub's search ceiling; and the
`truncate`/cap parameters elsewhere (`history.read_job`, `target_selectors.PR_LIST_LIMIT`).

## Risks

- **Fakes bypass the guard.** Test `GhClient` fakes implement `search_prs` themselves, so
  the ValueError cannot be tested through them; it is tested directly on
  `RealGhClient.search_prs`. Acceptable — the guard's job is protecting the real API call.
- **Failure-mode change is the point, but is still a change**: an out-of-range limit now
  raises locally instead of failing at GitHub (where `search_prs` currently returns
  `None` → fail-closed `search_failed`). A raise here is a programming error, not a
  runtime condition, so a hard `ValueError` matches how the issue frames it ("fails
  locally with a clear error").
- **Low blast radius**: all existing callers pass the default or CLI-validated values in
  1–100, so no behaviour change for any current path.

## Verification

`scripts/ci-check.sh` green (issue acceptance), plus the new guard tests and the existing
`test_review_scan_limit_validation_and_forwarding` unchanged and passing.
