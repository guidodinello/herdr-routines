---
id: "073"
title: "Target selectors for routine jobs: code picks what to act on, and skips the run when there is nothing"
status: done
priority: high
area: scheduler
---

## Description

Phase B of issue 071's split of `kind` into trigger / target / payload / proof.
Phase A (#226) added post-run `verify:` gates. For a `routine` job, the gate learns the
target from a `Target: <owner>/<repo>#<n>` line the agent writes in its report, because
only the agent knows what it chose. That leaves two problems:

1. **"Nothing to do" counts as a failure.** The fitted reviewers run several times a day
   against "the oldest open PR not yet reviewed". Once that queue is empty, the agent
   writes no `Target:` line, and `review_posted` fails the run as `target_undeclared`.
   Deploying phase A's gates as they stand would turn every idle run into a failure
   notification. So phase A is not yet deployed on the Pi's fitted jobs.
2. **Choosing the target is still prompt prose.** The fitted prompt spells out the
   selection rule in Markdown: list PRs, drop drafts and bots, check for a
   `### Code review` marker. A model can get it wrong or skip it. Two staggered jobs
   with the same rule can also pick the same PR.

"Is there anything to do, and what?" should be answered in code before an agent starts.
When the answer is no, nothing runs.

## Design

### `pick:` job key (routine only in this phase)

```yaml
pick: oldest_unreviewed_pr
```

The key is `pick`, not `selector`: it shares no stem with `kind: review`'s `select:` list or
the gated kind's `target: pr|base`, so it cannot be misread or mistyped as either. Everywhere
below, "selector" names the concept and `pick` the job key.

- Closed registry of selector names, validated at load like `verify` gates (unknown
  name → `ConfigError`). Inheritable from `defaults.yaml` with the same rule as
  `verify`: an inherited selector on a kind that doesn't honor it gets a `validate`
  warning, never a silent drop.
- Pick a key name that cannot be confused with `kind: review`'s existing `select:` list
  (`author` / `review_requested`) or the `gated` kind's `target: pr|base`, and say why
  in the plan.
- A selector is a function `(SelectorContext) -> SelectedTarget | None`. Every external
  call goes through the injected `GhClient` with a timeout. A selector that errors fails
  the run with reason `selector_failed`, before any agent spawns. It never counts as
  "nothing to do".

### The run, with a selector

1. **Before dispatch**, in `tick._process_job` and `cli._cmd_run`, so a manual
   `herdr-routines run` behaves exactly like a scheduled one: run the selector.
2. **No target** → record the run `skipped` with reason `no_target`. No worktree, no
   agent, no notification under any `notify_policy` except `always`. A skipped run is
   the occurrence having run: it is never reported as missed, and never retried.
3. **A target** → substitute it into the prompt as `$ROUTINE_TARGET`
   (`<owner>/<repo>#<n>`), next to `$ROUTINE_REPORT` / `$ROUTINE_JOB` /
   `$ROUTINE_RUN_ID` in `runner.substitute_prompt`. Pass it to `VerifyContext.target`, so
   `review_posted` checks the PR that code chose. Record it in the run's history
   (`extra.target`).
4. A job with a `selector` no longer needs, or reads, the report's `Target:` line. A
   job without one keeps phase A's behaviour unchanged.

### `oldest_unreviewed_pr`

The fitted prompt's rule, in code, scoped to the job's own repo (resolved from the
job's checkout `origin`, like `gates.remote_owner_and_repo`):

- open, not draft;
- author is not a bot (login ends in `[bot]`, or contains `dependabot`,
  `github-actions` or `copilot`);
- not already reviewed, using **the same predicate `review_posted` uses** (a trusted
  author's body carrying the code-review marker), with no freshness bound. "Reviewed"
  is defined once, so the selector and the gate can't drift apart;
- lowest PR number first. Return one target.

A Copilot "unable to review … quota" notice is not a review: it is untrusted and has
no marker, and both selector and gate already ignore it.

## Acceptance criteria

1. [blocking] A routine job with `pick: oldest_unreviewed_pr` whose repo has no
   unreviewed PR is recorded `skipped`, reason `no_target`, and no agent or worktree is
   created. Test: `test_selector_no_target_skips_without_spawning`
2. [blocking] With a target, the prompt receives it as `$ROUTINE_TARGET`, history
   records it, and `review_posted` checks that PR without any `Target:` line in the
   report. Test: `test_selector_target_reaches_prompt_history_and_gate`
3. [blocking] `oldest_unreviewed_pr` skips drafts, bot authors and PRs that already
   carry a trusted code review, and returns the lowest remaining number. Test:
   `test_oldest_unreviewed_pr_selection_rules`
4. [blocking] The selector's "reviewed" check and `review_posted` share one predicate.
   Changing it changes both. Test: `test_selector_and_gate_share_reviewed_predicate`
5. [blocking] A selector error fails the run as `selector_failed` before dispatch. It
   is never treated as no target. Test: `test_selector_error_fails_run_before_dispatch`
6. [blocking] `herdr-routines run <job>` goes through the same selector path as a
   scheduled tick: it skips on no target and fails on a selector error. Test:
   `test_manual_run_uses_selector`
7. [blocking] Unknown selector name → `ConfigError`; selector on a non-routine kind is
   handled with the same explicit-error / inherited-warning rule as `verify`. Test:
   `test_selector_config_validation`
8. [non-blocking] `deploy/jobs.d/fitted-pr-review.yaml` uses
   `pick: oldest_unreviewed_pr` with `verify: [review_posted, report_wellformed]`,
   and its prompt shrinks to "run /code-review on $ROUTINE_TARGET". The selection prose
   and the `Target:` instruction go.

## Out of scope / follow-ups

- More selectors (`my_prs`, `new_findings`, `next_issue`), and rebuilding `kind: review`
  as routine + selector. Its selection already happens in code, so this is cleanup,
  not a fix.
- Manual `herdr-routines run` also skips the fallback model, retries and the history
  record that the tick path applies (found 2026-10-24, when a manual rerun died on
  `quota_exhausted` with no history row). Criterion 6 makes the selector path
  shared. Unifying the rest of `_cmd_run` with `_process_job` is its own issue.
- Phase C: triggers.

## Notes

- After merge, switch the Pi's `fitted-pr-review*` job files to the new selector +
  verify shape (runner self-update first, since the old runner rejects unknown keys).
- Related: 071 (phase A, verify gates), 055 (`kind: review`), 049 (kind as dispatch key).
