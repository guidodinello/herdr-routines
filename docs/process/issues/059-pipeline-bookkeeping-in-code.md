---
id: "059"
title: "Pipeline: move stage-3 bookkeeping into code, gate on CI with a fix loop, split test authoring from implementation"
status: done
priority: high
area: pipeline
gate: PR #160 merged (issue-done check before push, PR body fixes, stage-3 prompt runs mypy)
---

## Description

Every failure in pipeline PR #159 (run `20261011T050000Z`, issue 055) came from a step
the stage-3 prompt told the agent to do. The agent either did it badly or skipped it,
and nothing checked until the PR was already open:

- the issue was never flipped to `status: done`
- all 2,644 lines of implementation were swept into one
  `chore: mark issue NN done` commit, with the literal `NN` and without the issue file
- CI was red: the prompt left mypy out, and ruff's `RUF059` slipped past even though
  the prompt listed `ruff check`
- one acceptance-test author and one implementer were the same session, so every test
  was written by the model it was meant to check

PR #160 patched the symptoms. It checks the issue flip before push, fixes the PR body,
and adds mypy to the prompt. That is still a prompt asking a model to remember things.
The principle this issue applies: **models do the work that needs judgment; code does
the bookkeeping and checks everything a model claims.** Making the prompts more
specific is not the fix. The old prompt spelled out the exact commit command, and the
agent still got it wrong.

## Design

### 1. One CI check script, shared by CI and the pipeline

`scripts/ci-check.sh` runs exactly what CI runs:
`uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest -q`.
`.github/workflows/ci.yml` calls it, or its jobs call the same commands listed in one
place, so the pipeline's check cannot drift from CI's. This makes the script the single
source of truth: "stage 3 forgot mypy" cannot happen again.

### 2. Split stage 3: test author, then implementer

- **Test-author stage (new, `independent`).** Writes every `Test: <name>` from spec v2's
  acceptance criteria, and only tests. It commits them. Code checks that every named
  test exists (the existing `gate3_test_names_present`) and that the commit touches only
  `tests/`. The tests are expected to fail at this point.
- **Implementer stage (current stage 3).** Makes the tests pass. Code checks that it did
  not weaken them. Minimum rule: the implementer's diff must not delete or rename any
  named test function. Whether it may edit their bodies at all is open (see below).
- The test author uses a different model family from the implementer, the same
  independence rule stage 5's reviewer already follows
  (`test_stage_fallbacks_stay_off_zen_and_keep_the_review_independent`).

### 3. Bookkeeping after the implementer, in code

These steps run between the implementer stage and the PR stage, with no agent:

1. **Dirty-tree sweep.** If `git status --porcelain` is non-empty, code commits the
   leftovers as `feat: issue <id> — uncommitted implementer work` and writes a line in
   the run report. The work is kept, not discarded, and the commit message says what
   happened.
2. **CI check with a fix loop.** Code runs `scripts/ci-check.sh` in the worktree. On
   failure it sends the implementer's own session (resumed, the same mechanism as
   `FALLBACK_CONTINUE_PROMPT`) a message with the failing output, truncated to the
   relevant tail, and asks it to fix and commit. Then it re-sweeps and re-checks.
   **Cap: 2 fix rounds.** If the check is still red after that, the run fails as
   `ci_check_failed` and nothing is pushed.
3. **Issue flip.** Code rewrites the issue file's frontmatter from `status: open` to
   `status: done` and commits only that file as
   `chore: mark issue <id> done (merging this PR closes it)`. This step is removed from
   every prompt.
4. **PR preflight.** Before opening the PR: the problem section must be non-empty, there
   must be at least one acceptance test, and the issue must be done (PR #160's check,
   now an invariant). Any miss fails loudly instead of opening a PR with a blank
   section.

After this, the implementer prompt shrinks to: implement spec v2, make the named tests
pass, commit your work. Everything in it of the form "don't forget X" goes away.

### 4. What stays as is

- Gate CI after the PR stage stays, as a backstop. With step 3.2 it should almost never
  find anything new.
- Stage 6 (address review) keeps reusing the implementer's session. Whether it also
  gets the CI fix loop is out of scope here.

## Decisions

- **Renumber stages to 1–7.** The test author becomes stage 3; implement, PR,
  review and address-review become 4–7. Stage numbers live in `state.json`
  (`current_stage`, `stage_sessions`), the G-17 layout check, reports and tick.
  Every one of those moves together. Runs in flight from before the change are not
  resumable across it; that is accepted. `3a`/`3b` sub-stages were rejected because
  they break `int` stage ids everywhere.
- **The implementer may edit named test bodies, but every edit is disclosed.** Deleting
  or renaming a named test fails the run. Body edits are allowed, and code lists each
  edited named test in the PR body under a "Tests changed by the implementer" heading,
  so the human reviewer sees exactly where the implementer changed its own grader.
- **The fix loop respects the deadline.** Before each fix round, code checks
  `deadline_epoch`. If a round would start past it, the run ends `partial` (the
  existing deadline outcome) with nothing pushed, rather than starting a round it
  cannot finish.

## Acceptance criteria

1. [blocking] `scripts/ci-check.sh` exists and CI runs the same commands — Test: test_ci_check_script_matches_ci_workflow
2. [blocking] A run whose implementer leaves uncommitted changes gets them committed by code with the documented message, and the report says so — Test: test_pipeline_commits_leftover_implementer_work
3. [blocking] A failing CI check resumes the implementer session with the failure output, and a fix within 2 rounds lets the run continue — Test: test_pipeline_ci_check_fix_loop_recovers
4. [blocking] A CI check still red after 2 rounds fails the run as `ci_check_failed` with nothing pushed — Test: test_pipeline_ci_check_gives_up_after_two_rounds
5. [blocking] Code, not an agent, flips the issue to `status: done` in a commit touching only that file; no stage prompt mentions the flip — Test: test_pipeline_flips_issue_in_code
6. [blocking] The test-author stage's commit touches only `tests/` and contains every named test — Test: test_test_author_stage_touches_only_tests
7. [blocking] An implementer diff that deletes or renames a named test fails the run — Test: test_implementer_cannot_remove_named_tests
8. [blocking] Every named test whose body the implementer edited is listed in the PR body — Test: test_pr_body_discloses_implementer_test_edits
9. [blocking] The test author's model family differs from the implementer's, primary and fallback — Test: test_test_author_model_is_independent_of_implementer
10. [blocking] Stages are numbered 1–7 consistently across the stage table, prompt files, G-17 layout and tick's report reading — Test: test_stages_table_matches_the_prompt_files_on_disk
11. [blocking] A fix round that would start past `deadline_epoch` ends the run `partial` with nothing pushed — Test: test_pipeline_ci_fix_loop_respects_deadline
12. [non-blocking] The PR is not opened when the spec's problem section is empty — Test: test_pr_preflight_rejects_empty_problem
