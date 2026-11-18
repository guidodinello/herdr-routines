# Spec v2 — issue 086: Enforce GitHub's 100-result ceiling on search_prs limit

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

- [blocking] **Fakes bypass the guard.** Test `GhClient` fakes implement `search_prs`
  themselves, so the ValueError cannot be tested through them; it is tested directly on
  `RealGhClient.search_prs`. Acceptable — the guard's job is protecting the real API call.
  — confidence: high (the guard sits in the impl every real path instantiates; fakes are
  test-local doubles that never reach `gh`)
- [blocking] **Failure-mode change is the point, but is still a change**: an out-of-range limit now
  raises locally instead of failing at GitHub (where `search_prs` currently returns
  `None` → fail-closed `search_failed`). A raise here is a programming error, not a
  runtime condition, so a hard `ValueError` matches how the issue frames it ("fails
  locally with a clear error"). — confidence: high (no current caller can hit it:
  defaults and CLI-validated values are all in 1–100)
- [non-blocking] **Low blast radius**: all existing callers pass the default or CLI-validated values in
  1–100, so no behaviour change for any current path. — confidence: high (every call
  site was read; all use the default or forward the CLI-validated `args.limit`)

## Verification

`scripts/ci-check.sh` green (issue acceptance), plus the new guard tests and the existing
`test_review_scan_limit_validation_and_forwarding` unchanged and passing.

## Acceptance criteria

1. [blocking] The ceiling is defined exactly once: `auto_fix.py` adds
   `REVIEW_SEARCH_MAX_RESULTS = 100` next to `REVIEW_SEARCH_QUALIFIERS` (~:889,
   replacing the prose-only comment), and the `limit: int = 100` defaults in
   `GhClient.search_prs` (protocol ~:115), `RealGhClient.search_prs` (:344) and
   `review_scan.scan_review_targets` (~:122) / `classify_review_candidates` (~:258)
   all become `limit: int = REVIEW_SEARCH_MAX_RESULTS` — no literal `100` limit
   default remains at any of those four signatures, and no behaviour changes (a
   default-driven call still builds `first: 100` queries identical to today's). —
   confidence: high — Test: test_review_search_max_results_is_single_source_of_truth
2. [blocking] The guard fires locally before any query is built:
   `RealGhClient.search_prs` raises `ValueError` when
   `not 1 <= limit <= REVIEW_SEARCH_MAX_RESULTS` — checked for `limit=0`,
   `limit=101`, and a negative value, with the mocked `gh`/`graphql` channel
   proving zero invocations (rejection is local, not deferred to GitHub). —
   confidence: high — Test: test_search_prs_rejects_limit_outside_github_ceiling
3. [blocking] The boundaries stay open: `limit=1` and `limit=100` (the ceiling itself)
   do not raise and proceed to build/issue the GraphQL search through the mocked
   `graphql`, matching today's happy path exactly. — confidence: high —
   Test: test_search_prs_accepts_limit_at_ceiling_boundaries
4. [non-blocking] The CLI keeps its contract while pointing at the constant:
   `review-scan --limit`'s default (cli.py:619) and the validation range/message
   (cli.py:1742-1746) read `REVIEW_SEARCH_MAX_RESULTS` instead of the literal
   `100`, with identical observable behaviour (same default, same
   `--limit must be between 1 and 100 (got N)` usage error, exit 2). —
   confidence: high — Test: test_review_scan_limit_validation_and_forwarding

## Changelog v1→v2

- Added `## Acceptance criteria` (v1 had only a prose `## Verification` section):
  four numbered items, each tagged `[blocking]`/`[non-blocking]` with a
  `confidence:` tier and pinned to an exact test name — three new guard/constant
  tests in `tests/test_auto_fix.py` plus the existing CLI test in
  `tests/test_cli.py:1417` (file pin added; v1 named the test without its file).
- Tagged the `## Risks` items with `[blocking]`/`[non-blocking]` and added
  `confidence:` tiers (v1's risks were untagged prose), matching the house
  convention used in the 20261109 run's spec.
- No change to approach, files-touched, or scope. Line references re-verified
  against src before committing: `GhClient.search_prs` protocol ~auto_fix.py:115,
  impl auto_fix.py:344, `REVIEW_SEARCH_QUALIFIERS` auto_fix.py:889, CLI
  cli.py:619 and cli.py:1742-1746 — all as v1 stated.
