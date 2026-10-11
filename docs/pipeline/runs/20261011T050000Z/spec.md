# Spec v2 — Issue 055: Review `@me` PRs across repos (`kind: review`)

Source: `docs/process/issues/055-review-me-prs-across-repos.md` (only scope for this run).
Background only: `docs/plan-v1.md` (project context, not the feature).

## Problem

`babysit-prs` (issue 015) watches one repo and only PRs the routine opened itself
(branch prefix `auto/` scopes it). PRs a human authors by hand in any repo — or PRs
where the human is the requested reviewer — get no review. The fix is a cross-repo
`kind: review` job that posts one review per open PR.

The hard part is that "unreviewed" has no self-clearing signal: after a review
posts, the PR is still open, so a naive tick re-reviews it forever. The issue makes
two guards load-bearing, plus cross-repo checkout mechanics.

## Approach

Ship as two phases, Phase A first (deterministic selector, no agent dispatch), then
Phase B (dispatch + verify). Per the issue design:

- **Config:** new `kind: review` (4th `kind` value, 3rd dispatch branch in
  `_process_job` after `pipeline`/`gated`). Rejects `repo`/`repository`/`workspace`/
  `checks` at load time; `target` absent-or-`pr`; `select` required, non-empty,
  duplicate-free list of `{author, review_requested}`; `include_auto_branches` bool
  default `false`; `max_attempts_per_target` default **1** for this kind.
  `Job.repo` = managed-clone base dir (`repos.default_repos_dir()`); `ensure_repo`
  never called with a `kind: review` job. Ships off: example job
  `deploy/jobs.d/review-my-prs.yaml` with `enabled: false`, `cron: "30 8 * * *"`.
- **Phase A — discovery (one batched GraphQL search):** new
  `GhClient.search_prs(modes, limit)` — one `gh api graphql` call, one alias per
  selected mode. Qualifiers: `author` → `is:pr is:open archived:false draft:false
  author:@me`; `review_requested` → `... review-requested:@me`. No pagination in
  phase A; a mode hitting the 100-node ceiling records `search_truncated` in `extra`.
  Fail closed on any `gh` failure → `failed (search_failed)`.
  Skip rules with own `extra["skip_reason"]`: `author_mismatch` (stale login vs
  `gh api user`), `routine_owned_branch` (`auto/` prefix unless opt-in — avoids
  double-reviewing pipeline PRs), `fork_pr` (deferred, named follow-up).
  Order oldest-first (`createdAt`, then `updatedAt`, then `owner/repo#N`); cap
  `max_workers_per_tick` cuts newest, never backlog.
- **Guard 1 — content marker:** review agent ends posted review with
  `<!-- herdr-routines:review job=<job> key=owner/repo#N -->`. Pre-dispatch, engine
  reads PR bodies via new `GhClient.pr_review_bodies(owner, repo, number)` (one call:
  `gh pr view --json comments,reviews`, both surfaces concatenated) and skips
  `review_already_posted` when this job's marker for that target key exists.
  Other jobs' markers are not a match. `None` (read failure) = unverified.
- **Guard 2 — attempt budget:** `attempt_count_for_target(history, job, target_key)`
  over `owner/repo#N` with `_COUNTABLE_STATES` (skips don't consume); at budget →
  `max_attempts_exceeded`, no dispatch.
- **Phase B — per-repo checkout + read-only worktree:** one
  `gh api repos/{owner}/{repo}` per distinct repo per tick (cached) → refactor
  `repos.ensure_repo` into `repos._ensure_checkout(clone_url, dest, base)` with
  `ensure_repo` delegating (babysit behavior unchanged). Checkout at
  `<base>/<owner>/<repo>` on the repo's own default branch; per-PR `git fetch
  origin <headRef>` + worktree from `origin/<headRef>` at
  `<checkout>/.worktrees/review-pr<N>`. Reuse own clean worktree via existing
  `_worktree_reuse_check`; stale → force-remove + re-add; other owner's worktree
  holding head branch → `skipped (head_branch_checked_out)`; remove worktree
  `--force` after settle (success or failure). Clone/sync failure fails only that
  repo's targets.
- **Agent identity:** `rt-<job>-rv<h6>`, `h6 = sha1("job|target_key")[:6]` + run tail;
  prefix `rt-<job>-rv<h6>` shared between builder (`build_review_agent_prefix`) and
  liveness guard; job-name portion capped at 13 chars so name stays ≤ 32 with ≥ 6-char
  tail (avoids `agent_name_taken` wedge, issue 051).
- **Prompt:** empty `prompt` → engine-injected sibling of `build_fix_prompt`
  (`auto_fix.build_review_prompt`): run `code-review` skill single-session vs PR URL,
  post exactly one blocking/non-blocking-tiered review, re-read PR first and post
  nothing if marker present, never edit/commit/push/resolve, write `$ROUTINE_REPORT`
  with `## Outcome:` line. Job-level `prompt` overrides.
- **Post-condition (gate analogue):** after settle, re-query bodies: marker present
  → `done` + `verified: True` + `gate: passed`; absent → `failed
  (review_not_posted)`; read failure → `failed (review_verification_failed)`, never
  `done`.
- **History/report:** per-target `extra` (`target_key`, `repo`, `pr_number`,
  `headRefName`, `select_mode`, `attempt`, `agent`, `pane_id`, `report_path`,
  `verified`, `target: "pr"`, + reason/error/skip_reason); terminal record
  `gate: passed|failed` + counts (`enumerated`, `candidates`, `dispatched`,
  `skipped`, `repos_failed`). Aggregate `reports/review-<run_id>.md` lists every
  candidate + disposition; per-PR `reports/auto-review-<run_id>-<h6>.md`.
- **Tests (19 criteria → named tests):** Phase A: `test_review_job_config_validation`,
  `test_review_scan_selects_open_non_draft_prs`,
  `test_review_scan_skips_auto_branches_unless_opted_in`,
  `test_review_scan_skips_fork_prs`,
  `test_review_scan_orders_oldest_first_and_caps_newest`,
  `test_review_scan_marker_guard_and_search_fail_closed`,
  `test_review_attempts_are_counted_per_target_key`,
  `test_review_report_lists_candidates_and_skips`,
  `test_shipped_review_job_config_validates`; Phase B:
  `test_review_dispatch_creates_per_repo_checkout`,
  `test_review_dispatch_skips_other_worktrees_head_branch`,
  `test_review_dispatch_reuses_own_review_worktree`,
  `test_review_dispatch_removes_worktree_after_settle`,
  `test_review_verification_is_fail_closed`,
  `test_review_dispatch_isolates_per_repo_failures`,
  `test_review_live_agent_prefix_guard_and_name_cap`,
  `test_review_prompt_is_read_only_and_idempotent`,
  `test_review_is_not_repeated_across_ticks`,
  `test_ensure_checkout_refactor_preserves_babysit` + existing
  `test_auto_fix.py`/`test_tick.py` suites green. ROADMAP pointer updated.
- **Non-goals:** no fixing/committing/pushing/resolving; no fork PRs, no fan-out,
  no inspect-only CLI; no `babysit-prs` scope change; not a stage-5 replacement.

## Files touched

- `src/herdr_routines/config.py` — `kind: review` literal, validation rules, defaults.
- `src/herdr_routines/tick.py` — third dispatch branch in `_process_job`, report shape.
- `src/herdr_routines/repos.py` — extract `_ensure_checkout`, per-repo clone layout.
- `src/herdr_routines/auto_fix.py` — `build_review_agent_prefix`,
  `build_review_prompt` (+ name helpers).
- **New** `src/herdr_routines/review_scan.py` — pure selection (search → candidates →
  skip reasons → ordering → marker/attempt guards).
- **New** `tests/test_review_scan.py` (+ Phase B dispatch/verify tests).
- **New** `deploy/jobs.d/review-my-prs.yaml` — shipped example, `enabled: false`.
- `ROADMAP.md` — Parking Lot bullet pointer.

## Risks

1. **Re-review loop (highest):** both guards must hold across restarts; marker read
   capped at `gh`'s 100-item page can miss a buried marker → bounded by
   `max_attempts_per_target: 1` to one extra review, never a loop. Criteria 6/7/17.
2. **Worktree contention (issue 036 redux):** fighting another owner's branch or
   leaving a worktree behind recreates the collision; mitigated by
   `head_branch_checked_out` skip + forced post-settle removal. Criteria 10–12.
3. **Fail-open reads:** search failure or unverifiable post-condition must never read
   as "nothing to do" / `done` — both fail closed. Criteria 6/13.
4. **Per-repo failure blast radius:** one bad clone must not sink the tick — isolate
   per repo. Criterion 14.
5. **Duplicate dispatch / name wedge:** two ticks 5 min apart double-dispatching, or
   long job names collapsing the run tail (issues 036c/051) — shared prefix fn + 13-char
   cap. Criterion 15.
6. **Read-only violation:** agent pushes/edits a PR meant only to be reviewed —
   prompt clause + worktree removal; criterion 16.
7. **Shared-refactor regression:** `ensure_repo` extraction changes `babysit-prs`
   behavior — pinned by criterion 18 + existing suites unmodified.
8. **Per-run path note:** this spec lives at
   `docs/pipeline/runs/20261011T050000Z/spec.md`, not root `spec.md` (G-15: shared
   root path caused PR #29/#28 merge conflict).

## Acceptance criteria

Each item states its tier (`blocking` or `non-blocking`) and a `confidence:` tier
(`high` / `medium` / `low`), and ends with its exact verification test name.

1. [blocking, confidence: high] `kind: review` validates: rejects `repo`/`repository`/`workspace`/`checks`, requires non-empty duplicate-free `select` of `author`/`review_requested`, `target` absent-or-`pr`, `include_auto_branches` defaults `false`, `max_attempts_per_target` defaults `1`. Test: test_review_job_config_validation
2. [blocking, confidence: high] Scan selects only open non-draft non-archived PRs via one batched GraphQL call with `author:@me` / `review-requested:@me` qualifiers per selected mode. Test: test_review_scan_selects_open_non_draft_prs
3. [blocking, confidence: high] Scan skips `auto/`-prefixed branches as `routine_owned_branch` unless `include_auto_branches` is opted in, avoiding double-review of pipeline PRs. Test: test_review_scan_skips_auto_branches_unless_opted_in
4. [non-blocking, confidence: medium] Scan skips fork PRs with `fork_pr` skip reason and names the deferred follow-up. Test: test_review_scan_skips_fork_prs
5. [blocking, confidence: medium] Scan orders candidates oldest-first by `createdAt`, then `updatedAt`, then `owner/repo#N`, and `max_workers_per_tick` cap cuts newest first. Test: test_review_scan_orders_oldest_first_and_caps_newest
6. [blocking, confidence: high] Pre-dispatch marker guard skips `review_already_posted` on exact job+key marker match only, and any `gh` search failure fails closed as `failed (search_failed)`. Test: test_review_scan_marker_guard_and_search_fail_closed
7. [blocking, confidence: high] Attempt budget counts only countable states per `owner/repo#N` target key and skips `max_attempts_exceeded` at budget without dispatch. Test: test_review_attempts_are_counted_per_target_key
8. [non-blocking, confidence: medium] Aggregate `reports/review-<run_id>.md` lists every candidate with disposition plus skip reasons and counts (`enumerated`, `candidates`, `dispatched`, `skipped`, `repos_failed`). Test: test_review_report_lists_candidates_and_skips
9. [non-blocking, confidence: medium] Shipped example `deploy/jobs.d/review-my-prs.yaml` is disabled by default and passes `kind: review` validation. Test: test_shipped_review_job_config_validates
10. [blocking, confidence: high] Dispatch creates per-repo checkout at `<base>/<owner>/<repo>` on the repo's default branch via extracted `_ensure_checkout` with per-repo GraphQL default-branch lookup cached per tick. Test: test_review_dispatch_creates_per_repo_checkout
11. [blocking, confidence: medium] Dispatch skips `head_branch_checked_out` when another owner's worktree holds the PR head branch instead of fighting it. Test: test_review_dispatch_skips_other_worktrees_head_branch
12. [non-blocking, confidence: medium] Dispatch reuses the job's own clean review worktree at `<checkout>/.worktrees/review-pr<N>` and force-removes plus re-adds a stale one. Test: test_review_dispatch_reuses_own_review_worktree
13. [blocking, confidence: medium] Dispatch removes the per-PR worktree with `--force` after settle on both success and failure paths. Test: test_review_dispatch_removes_worktree_after_settle
14. [blocking, confidence: high] Post-condition re-queries review bodies: marker present yields `done` + `verified: True` + `gate: passed`; absent yields `failed (review_not_posted)`; read failure yields `failed (review_verification_failed)`, never `done`. Test: test_review_verification_is_fail_closed
15. [blocking, confidence: medium] Per-repo clone/sync failure fails only that repo's targets and the tick continues for remaining repos. Test: test_review_dispatch_isolates_per_repo_failures
16. [blocking, confidence: medium] Live agent name uses shared `build_review_agent_prefix` `rt-<job>-rv<h6>` with 13-char job-name cap keeping total length ≤ 32 with ≥ 6-char tail so the liveness guard matches. Test: test_review_live_agent_prefix_guard_and_name_cap
17. [blocking, confidence: high] Default review prompt runs the `code-review` skill single-session, posts exactly one blocking/non-blocking-tiered review, re-checks the marker first, never edits/commits/pushes/resolves, and writes `$ROUTINE_REPORT` with a `## Outcome:` line. Test: test_review_prompt_is_read_only_and_idempotent
18. [blocking, confidence: high] A reviewed target is not re-dispatched on later ticks via the marker guard plus attempt budget across restarts. Test: test_review_is_not_repeated_across_ticks
19. [blocking, confidence: high] `repos._ensure_checkout` extraction preserves existing `babysit-prs` `ensure_repo` behavior with existing `test_auto_fix.py`/`test_tick.py` suites green. Test: test_ensure_checkout_refactor_preserves_babysit

## Changelog v1→v2

- v1 title `Spec v1` → v2 title `Spec v2`; corrected test count `18 criteria` → `19 criteria` to match the 19 named tests actually listed.
- Added `## Acceptance criteria` (19 numbered items): each item carries a `blocking`/`non-blocking` tier plus a `confidence:` tier (`high`/`medium`/`low`) and ends with `Test: <exact test name>`.
- No change to Problem/Approach/Files/Risks scope; risks criterion cross-references unchanged.
