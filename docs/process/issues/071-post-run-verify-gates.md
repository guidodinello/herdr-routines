---
id: "071"
title: "Post-run verify gates as a job field, so no kind records done on the agent's word"
status: done
priority: high
area: scheduler
---

## Description

On 2026-10-24 two manual runs of the fitted reviewers ran back to back on the Pi.
`fitted-pr-review` (`fitted-pr-review-20261024T000552Z`) reviewed fitted #838. The
review is on the PR with 3 inline notes, and the label moved to `reviewed`.
`fitted-pr-review-4` (`fitted-pr-review-4-20261024T001947Z`, `opencode/big-pickle`)
was recorded `done (ok)`. Its report says it reviewed #839 "via code-review skill (PR
review with inline comments; pending-review label swapped to reviewed)". Nothing of
that is on #839: no review, no comment, label still `pending-review`. The report
itself is one line with literal `\n` escapes.

The fitted reviewers are plain `routine` jobs. A `routine` run is `done` when its
agent settles with a non-empty `$ROUTINE_REPORT` (`runner.execute_run`, the
`no_report` check). The check that would have caught this does exist, but only
inside `kind: review` (issue 055): after the agent settles, `_process_review_job`
re-reads the PR through `GhClient.pr_review_bodies` and requires the job's marker
from a trusted author (`auto_fix.has_trusted_marker`). Otherwise it records
`review_not_posted`. A `routine` job cannot opt into it.

This is one instance of a structural problem. A job here has four concerns: what
triggers it, what it acts on, what runs, and what proves it worked. Issue 049 set
out to unify them ("one gated-workflow engine"). It landed as a single `kind` enum
that picks the dispatch path, and each kind since has carried its own fixed set of
all four:

| kind | trigger | acts on | payload | post-run proof |
|---|---|---|---|---|
| `routine` | cron | (prompt decides) | one agent | report non-empty |
| `gated` | cron | `checks` (PRs, events) | fix worker | none (issue 070) |
| `pipeline` | cron | `pick-feature` | staged orchestrator | gates 1-7 + CI, pipeline only |
| `audit` | cron | findings vs ledger | audit + fix worker | ledger diff |
| `review` | cron | `@me` PR search | review agent | review marker on PR |

So "did it work" is answered per kind or not at all. Issue 070 (a fix worker
recorded `done` after leaving uncommitted changes and pushing nothing) and this
incident are the same bug in two kinds. Each new check gets built into one kind
and nothing else can use it.

## Design

**Scope: post-run verification as a job field any kind can use.** This is phase A of
splitting the four concerns. Phases B and C are sketched under Follow-ups and are
out of scope here.

### `verify:` job key

```yaml
verify:
  - review_posted      # the run's target PR carries a code review posted by this run
  - report_wellformed  # $ROUTINE_REPORT is real Markdown, not empty / escape-garbage
```

- Optional list of gate names from a closed registry, validated at load (unknown name
  is a `ConfigError`). Inheritable from `defaults.yaml` like other defaults-able keys.
- Gates run after the agent settles `idle`/`done` with a report, before history is
  written. All listed gates run, none short-circuits, and each result goes into the
  run record (`extra.verify: [{gate, passed, reason}]`). Any failure makes the run
  `failed` with reason `verify_failed:<gate>` and notifies through the job's
  `notify_policy` as any failure does.
- A gate is a small function `(VerifyContext) -> GateVerdict`, reusing
  `gates.GateVerdict`. `VerifyContext` carries what the runner already knows: job,
  run_id, run start time, report path, worktree path, and the target if one is known.
  Every external call goes through the injected `GhClient` / runner with a timeout.
  A probe failure is a failed gate, never an exception and never a pass.

### Knowing the target

`review_posted` needs to know which PR the run meant. For `kind: review`, code
already knows (the candidate). A `routine` job's agent chooses its own target, so
the report must declare it on a line the gate parses: `Target: <owner>/<repo>#<n>`.
For a `review_posted` job, a missing or unparsable `Target:` line fails the gate.
Phase B (selectors) removes this by having code pick the target.

### `review_posted`

Passes when the target PR has a review or issue comment that:
1. is authored by a trusted author (`trusted_review_authors` plus the `gh` viewer, the
   same rule `has_trusted_marker` uses);
2. was created at or after the run's start, so an older review cannot satisfy a
   run that posted nothing. `pr_review_bodies` returns no timestamps today: extend it
   to carry `createdAt`;
3. carries the review marker: the job's `review_marker` when the job sets one,
   otherwise the code-review skill's `### Code review` header.

`kind: review`'s existing post-condition is reimplemented as this gate, with the same
dispositions and records (`review_not_posted`, `review_verification_failed`). After
this issue there is one implementation, not two.

### `report_wellformed`

Fails on an empty report, and on a report containing literal `\n` escapes instead
of newlines (the #839 report). This is a cheap first signal of a model that wrote
the report without doing the work. It is never a substitute for a real gate.

### Rollout

- Ship the fitted reviewer example in `deploy/jobs.d/` with
  `verify: [review_posted, report_wellformed]` and the `Target:` line in its prompt.
  Update the Pi's `fitted-pr-review*` job files the same way after merge.
- `validate` warns when a job's prompt runs `/code-review` and `verify` lacks
  `review_posted`, the same soft posture as the `$ROUTINE_REPORT` warning.

## Acceptance criteria

1. [blocking] A `routine` job with `verify: [review_posted]` whose agent settles with a
   report but posts no review on the declared target is recorded `failed`, reason
   `verify_failed:review_posted`, whatever the report says. Test:
   `test_routine_review_posted_gate_fails_when_no_review_landed`
2. [blocking] A review from before the run's start does not satisfy `review_posted`.
   Test: `test_review_posted_ignores_reviews_older_than_the_run`
3. [blocking] A missing or malformed `Target:` line fails `review_posted`, it does
   not skip it. Test: `test_review_posted_requires_declared_target`
4. [blocking] `kind: review`'s post-condition goes through the same gate function,
   and its existing tests pass unchanged. Test:
   `test_kind_review_uses_review_posted_gate`
5. [blocking] An unknown gate name in `verify` is a load-time `ConfigError`, and
   `verify` is inheritable from `defaults.yaml`. Test:
   `test_verify_unknown_gate_rejected_and_inherits_from_defaults`
6. [blocking] A gate whose probe errors or times out fails the run, never passes it.
   Test: `test_verify_probe_failure_fails_the_run`
7. [blocking] `report_wellformed` fails an escape-garbage report like the #839 one.
   Test: `test_report_wellformed_rejects_literal_escapes`
8. [non-blocking] `validate` warns on a `/code-review` prompt without
   `review_posted`. Test: `test_validate_warns_code_review_without_review_posted`

## Follow-ups (out of scope, file when this lands)

- **Issue 070 as gates.** `worktree_clean` and `pushed_commit` (remote head moved
  since dispatch) as registry entries, enabled on the `gated` fix-worker path. 070's
  attempt-budget criterion stays in 070.
- **Phase B: selectors.** Lift "what it acts on" out of the kinds into reusable,
  code-run selectors: `oldest_unreviewed_pr(repo)`, `my_prs`, `new_findings`,
  `next_issue`, `github_event`. With a selector, code knows the target, the `Target:`
  line goes away, and `kind: review` reduces to a routine with a PR selector and
  `verify: [review_posted]`.
- **Phase C: triggers.** Cron, plus event/request triggers as first-class job
  config instead of cron-polled `github_event` checks and the separate Telegram
  `/run` path. `kind` would then remain only for the one real engine difference,
  the detached multi-stage `pipeline`.

## Notes

- The missed review on fitted #839 was re-run by hand with `fitted-pr-review` on
  2026-10-23. Check that it landed before closing this.
- Related: 049 (kind as dispatch key), 055 (`kind: review`, source of the post-check),
  070 (fix workers `done` on trust), 034/035 (gates moved from prompt prose into code).
