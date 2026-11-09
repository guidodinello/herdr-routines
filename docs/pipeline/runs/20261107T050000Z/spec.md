# Spec v2 — 20261107T050000Z

Feature: issue-refinement issue-number collisions
(`docs/process/issues/080-refinement-issue-number-collisions.md`, priority: low,
area: pipeline). Related: 029 (the job), 071/072 (the 2026-10-24 collision).

## Problem

The nightly `issue-refinement` job decides its issue number in prompt prose:
step 2 of `deploy/jobs.d/issue-refinement.yaml` tells the author model to use
"the highest existing issue number + 1", computed from the agent's snapshot of
`docs/process/issues/`. On 2026-10-24 that snapshot was stale: #225 was opened
as issue `071` while #224 (also `071`) was open and about to merge, so `main`
ended up with two `071` files (hand-fixed in #227), and the ROADMAP pointer the
author hand-wrote landed on the wrong paragraph. Three gaps, all Sharing the
same root cause — the two load-bearing decisions (what number, where the
pointer goes) are made by the model instead of by code:

1. Nothing checks `origin/main` fresh at number-computation time — only the
   worktree checkout (branched from main at job start; main can advance).
2. Nothing checks open PRs at all: a refinement PR **or any other open PR**
   (e.g. `auto/pipeline-*`) already carrying `docs/process/issues/NNN-*.md`
   owns its number until merged.
3. `refine-issue --check-issue`
   (`issue_refinement.run_check_issue`, issue_refinement.py:369) validates
   frontmatter only — it would happily pass a duplicate `id:`.

## Approach

Keep the existing exit-code contract (0 pass / 1 fail / 2 usage); add two code
steps and widen a third, all in `issue_refinement.py`, mirroring the
`refinement_open_pr_slugs` fail-safe posture (unknown answer ⇒ exit 1, never
fail-open).

**1. `refine-issue --next-id` (new mode, mutually exclusive with
`--check-issue`/`--review-pass`).** Prints the next 3-digit zero-padded issue
id on stdout. The union of numbers in use:

- `origin/main` **after a fresh fetch** (`git fetch origin main --quiet`,
  then `git ls-tree -r --name-only origin/main -- docs/process/issues/`) — not
  the worktree checkout. Any `git` failure ⇒ exit 1 with the reason.
- every `docs/process/issues/NNN-*` path changed by an **open, same-repo PR** (any change type, not only additions)
  (`gh pr list --state open --json headRefName,files,isCrossRepository`; count
  any PR with `isCrossRepository: false`, not just `--author @me` — the
  pipeline's own PRs belong to another bot identity but own their numbers all
  the same). Filename numbers matched with a strict `docs/process/issues/(\d+)-`
  regex and compared as integers, so `71`/`071`/`0071` all collide; the union
  set is the collision space, not the max.
- Any `gh` failure ⇒ exit 1 (same failure-safe rule as the slug guard).
- `--next-id` output is just the id; the job's prompt step captures it
  (`NEXT_ID=$(uv run herdr-routines refine-issue --next-id)`), replacing the
  "highest existing + 1" prose sentence.

**2. `--check-issue` widens.** After the frontmatter checks pass,
`run_check_issue` additionally rejects the draft's own `id:` when that number
is already used on fresh-fetched `origin/main` or by an open PR (same union
source as step 1; `--repo` now feeds it, default `Path.cwd()`). Duplicate ⇒
exit 1 with a named problem line like the other `check-issue` failures. The
gh/git failure still fails safe (exit 1, message distinguishes "could not
verify number uniqueness" from "duplicate id").

**3. ROADMAP pointer as a code step.** New mode
`refine-issue --roadmap-pointer docs/process/issues/NNN-<slug>.md` (same
mutually exclusive group, also inheriting `--roadmap`/`--repo`): reads the
issue file's frontmatter `id`, derives the Parking-Lot bullet slug from the
filename, locates that bullet via the existing `parse_parking_lot_bullets`,
and appends `` → [`NNN`](docs/process/issues/NNN-<slug>.md) `` to the bullet's
**last** line at the two-space continuation indent (so a bullet that already
folds over multiple lines keeps parsing). Idempotent: exit 0 without edit if
that exact pointer text is already present on that bullet. Exit 1 when no
Parking Lot bullet matches the slug — never guess a paragraph; that is the
2026-10-24 bug in code form. The prompt's step-2 ROADMAP sentence is replaced
by a call to this step **after** the review loop settles (so the pointer is
written once, to live ROADMAP.md on the merged-to-be branch, not re-edited per
pass).

**4. Job prompt edits** (`deploy/jobs.d/issue-refinement.yaml`): step 2 uses
`--next-id`; pointer writing moves out of step 2 into its own step-4 call;
step 5 (`--check-issue`) keeps its exit-code loop unchanged — the duplicate
gate arrives inside it. `tests/test_orchestrator_prompt_invocation.py` and the
prompt-shape tests in `tests/test_issue_refinement.py` follow the edits.

## Files touched

- `src/herdr_routines/issue_refinement.py` — `next_issue_id` (fresh fetch +
  ls-tree + open-PR union), `open_pr_issue_numbers` (`gh pr list --json files`,
  fail-safe), `run_check_issue(repo, path)` duplicate-id gate,
  `apply_roadmap_pointer(roadmap_path, issue_path)` + entry point; mode wiring.
- `src/herdr_routines/cli.py` — `--next-id` / `--roadmap-pointer` on the
  refine-issue parser; pass `--repo` into `run_check_issue`.
- `deploy/jobs.d/issue-refinement.yaml` — prompt steps 2/4/5 rewritten around
  the three code steps; header comment updated (what is code vs prose).
- `tests/test_issue_refinement.py` — the three blocking tests
  (`test_next_issue_id_skips_open_pr_numbers`,
  `test_check_issue_rejects_duplicate_id`,
  `test_roadmap_pointer_appended_to_its_bullet`) plus: padded-vs-unpadded id
  equality (071 vs 0071), fail-safe on gh/git failure, pointer idempotence,
  continue-line indent preserved, wrong-slug exit 1, prompt-shape updates.

## Risks

- **gh JSON-field availability:** `gh pr list --json files` relies on the gh
  CLI supporting that field (it does for the pinned gh on this host); a gh
  upgrade dropping it degrades to fail-safe exit 1, which stops refinement
  runs rather than colliding — acceptable posture, and the failure messages
  name the exact call.
- **Fetch-in-worktree assumption:** the same-repo ls-tree/fetch runs in the
  job's worktree cwd; worktrees share refs so `origin/main` is visible, but if
  runner ever moves the job to a detached clone this must be re-verified (the
  risk is a silent stale-read, so the fetch error is fail-safe, not ignored).
- **Last-mile race:** a PR carrying the same number can open between
  `--next-id` and `gh pr create`; `--check-issue` runs closest to PR open and
  closes most of that window; the residual window needs a human merge and is
  the accepted floor without a GitHub-level lock.
- **Slug ambiguity:** the pointer step keys on the filename slug; an author
  that renames the file ↔ bullet mismatch exits 1 instead of attaching to a
  wrong paragraph — a deliberate no-recovery rule.
- **Parser touch-surface:** `apply_roadmap_pointer` edits live ROADMAP.md
  text; regression risk is the Parking Lot parser, covered by existing
  `parse_parking_lot_bullets` tests plus the indent/idempotence cases above.

## Acceptance criteria

Classification: each item is **blocking** (run must not ship without it) or
**non-blocking** (valuable, ship with a follow-up note if it slips). Confidence:
`high` (deterministic, fully testable in-repo), `medium` (testable but depends
on live external state shape), `low` (can only be exercised against the real
job).

1. **blocking / confidence: high** — The union of numbers from fresh-fetched
   `origin/main` and every open same-repo PR (regardless of author) is used for
   `--next-id`; a number owned only by an open PR is skipped, so `--next-id`
   never returns an id colliding with that PR. The union is compared as
   integers via the padded-id equality rule (071 = 0071), exactly once per
   source.
   Test: `test_next_issue_id_skips_open_pr_numbers`
2. **blocking / confidence: high** — `--next-id` fails closed: any `git fetch`
   /`ls-tree` or `gh pr list` failure exits 1 with a named reason and prints no
   id (fail-safe, mirroring `refinement_open_pr_slugs`).
   Test: `test_next_issue_id_fails_safe_on_git_or_gh_failure`
3. **blocking / confidence: high** — After frontmatter checks pass,
   `--check-issue` rejects a draft whose `id:` is already used on fresh-fetched
   `origin/main` or by an open PR, exiting 1 with a named duplicate-id problem
   line; the gh/git failure path exits 1 with "could not verify number
   uniqueness", distinct from the duplicate message.
   Test: `test_check_issue_rejects_duplicate_id`
4. **blocking / confidence: high** — `--check-issue` fails safe when
   uniqueness cannot be verified (gh/git error ⇒ exit 1, never fail-open).
   Test: `test_check_issue_fails_safe_when_uniqueness_unverifiable`
5. **blocking / confidence: high** — `--roadmap-pointer` locates the Parking
   Lot bullet via `parse_parking_lot_bullets` by filename slug, reads the
   issue frontmatter `id`, and appends `` → [`NNN`](docs/process/issues/NNN-
   <slug>.md) `` to the bullet's last line at the two-space continuation
   indent, preserving multi-line folding.
   Test: `test_roadmap_pointer_appended_to_its_bullet`
6. **non-blocking / confidence: high** — `--roadmap-pointer` is idempotent:
   the exact pointer text already present on the matching bullet ⇒ exit 0 with
   no edit.
   Test: `test_roadmap_pointer_is_idempotent`
7. **non-blocking / confidence: high** — Padded and unpadded representations
   of the same number (`071`, `71`, `0071`) compare as equal ids in the union
   and in the `--check-issue` duplicate gate.
   Test: `test_next_issue_id_treats_padded_ids_as_equal`
8. **non-blocking / confidence: high** — A filename slug with no matching
   Parking Lot bullet makes `--roadmap-pointer` exit 1 without editing
   ROADMAP.md (no guessing a paragraph).
   Test: `test_roadmap_pointer_unknown_slug_exits_1`
9. **non-blocking / confidence: medium** — `apply_roadmap_pointer` never
   breaks the two-space continuation indent: an existing folded bullet stays
   parseable by `parse_parking_lot_bullets` after the edit.
   Test: `test_roadmap_pointer_preserves_continuation_indent`
10. **non-blocking / confidence: high** — Mode exclusivity and CLI wiring:
    `--next-id`, `--roadmap-pointer`, `--check-issue`, `--review-pass` are
    mutually exclusive (usage exit 2 on overlap); `--repo` feeds
    `run_check_issue` (default `Path.cwd()`); empty/absent id still exits 2.
    Test: `test_refine_issue_next_id_and_roadmap_pointer_cli_usage_errors`
11. **non-blocking / confidence: medium** — The job prompt uses `--next-id` in
    step 2 (no "highest existing + 1" prose), writes the pointer in its own
    step-4 call after review settles, and keeps step 5's `--check-issue` exit
    loop; still invoked as `uv run herdr-routines`, never bare.
    Test: `test_issue_refinement_prompt_runs_the_code_enforced_steps`
12. **non-blocking / confidence: high** — `scripts/ci-check.sh` is green and
    the three blocking tests (items 1, 3, 5) are verified red before
    implementation, per the TDD sequencing in Verification. Meta-check: run
    `scripts/ci-check.sh` after confirming red on a clean tree.
    Test: `test_ci_green_with_blocking_tests_run_first`

## Changelog v1→v2

- Added `## Acceptance criteria`: 12 numbered items, each tagged
  **blocking**/non-blocking with a `confidence:` tier and ending
  `Test: <name>` (exact test names; the three blocking tests were already
  named in Files touched in v1 and are unchanged).
- Named the previously-implicit CLI usage-error expectations as criterion 10
  with its own usage-error test name; added the fail-safe split for
  `--check-issue` (criterion 4) that v1 described only in prose.
- Added a meta-check criterion (12) binding Verification's TDD/ci-green claims
  to a testable name.
- No change to Problem, Approach, Files touched, Risks, or Verification
  content — v2 only adds observability of acceptance, per stage-2 scope.
- Title bumped to Spec v2.

## Verification

`scripts/ci-check.sh` green; the three blocking tests fail pre-implementation
(TDD: write tests first, verify red, then implement); the job prompt edits keep
`test_orchestrator_prompt_invocation.py` green (still `uv run herdr-routines`,
never bare).
