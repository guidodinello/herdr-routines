# Spec v1 — 20261107T050000Z

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
- every `docs/process/issues/NNN-*` path added by an **open, same-repo PR**
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

## Verification

`scripts/ci-check.sh` green; the three blocking tests fail pre-implementation
(TDD: write tests first, verify red, then implement); the job prompt edits keep
`test_orchestrator_prompt_invocation.py` green (still `uv run herdr-routines`,
never bare).
