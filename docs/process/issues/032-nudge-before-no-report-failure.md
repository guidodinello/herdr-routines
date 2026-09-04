---
id: "032"
title: "One-shot nudge before declaring no_report failure"
status: open
priority: medium
area: runner
---

## Description

`execute_run` (`src/herdr_routines/runner.py`) deliberately treats a clean
agent settle (`idle`/`done`) with a missing or empty `$ROUTINE_REPORT` as
`reason="no_report"` failure — by design, per the existing comment: "a clean
settle with no report, or an empty one, is not done" (direct response to
unattended runs failing silently and plausibly). This classification is
correct and should not change: a settle with no report really is unproven
work.

But real Pi history (`history.jsonl`, Aug 30 – Sep 2) shows `no_report` is
now the *single most common* failure reason across the `fitted-pr-review`,
`fitted-pr-review-2`, and `fitted-implementer` jobs — 5 of 9 recent terminal
failures. All three jobs' prompts already correctly mention
`$ROUTINE_REPORT` (so `herdr-routines validate`'s existing warning for a
prompt that never mentions it doesn't fire). Spot-checking one run's
`<run_id>.tail.txt` (captured by the existing best-effort diagnostic tail at
runner.py:583-588) showed the agent had actually completed the real task
(posted a PR review, or determined no PR qualified) and then simply never
took the final "write the summary" step — a free-tier opencode model
(`muse-spark-1.2-contributor-free`, `big-pickle`) dropping the last
instruction in a multi-step prompt, not a job misconfiguration.

Issue 008 (retries on failure) explicitly can't safely cover this: retrying
the whole job risks re-doing non-idempotent side effects (a review job could
post a second review comment). What's needed instead is narrower — give the
same, already-settled agent one more chance to write the file it already
should have, before the harness gives up on work that was likely real.

## Design (proposal)

In `execute_run`, between the point `settled_status` is confirmed
`idle`/`done` (runner.py:628) and the existing pane-close +
`report_written`/`report_bytes` check (runner.py:629-645):

- If `report_written` is false or `report_bytes == 0`, send **one** bounded
  follow-up prompt to the same still-open pane/agent (before
  `_close_run_pane` runs) — e.g. "You appear to have finished without
  writing a summary to `$ROUTINE_REPORT`. Write one now describing what you
  did." — with a short, fixed timeout distinct from `job.timeout_ms` (that
  budget is already spent; this is a small top-up, not another full run).
- Re-check `report_path.exists()`/size after the nudge settles (or times
  out). Update `report_written`/`report_bytes` in `common` accordingly
  before falling through to the existing `no_report` check.
- Exactly one nudge attempt, never a loop — if the nudge itself times out,
  errors, or the agent still doesn't produce a non-empty report, fall
  through to today's `no_report` failure exactly as now. Log the nudge
  attempt distinctly in `history.jsonl` (e.g. an `extra.nudged: true` field,
  matching how `fallback_model`'s `extra.failover_attempts` is already
  recorded) so a human can distinguish "wrote the report on the first try"
  from "needed a nudge" without digging through tail logs.
- Never re-run `/code-review`-shaped or other side-effecting instructions in
  the nudge prompt — it must ask only for the missing report, not repeat the
  job's original task (which is exactly the non-idempotency issue 008
  already flagged).
- Root-mode jobs (`job.workspace == "root"`) skip `_capture_session_id` and
  pane-close today (runner.py:636) — the nudge must still work for them
  since the pane isn't closed either way; only worktree-mode jobs need the
  nudge to happen strictly before `_close_run_pane`.

## Acceptance

- An agent that settles idle/done but hasn't written `$ROUTINE_REPORT` gets
  exactly one follow-up prompt asking it to write the summary, before the
  run is declared failed.
- If the nudge succeeds (report now exists and is non-empty), the run's
  final `RunOutcome` is `done`, not `no_report` — and `history.jsonl` records
  that a nudge was needed (distinct from a first-try success).
- If the nudge fails, times out, or still produces no report, the run ends
  exactly as today: `state="failed", reason="no_report"`.
- The nudge is never sent more than once per run, and never repeats
  side-effecting instructions from the original prompt.
- `fallback_model`'s existing `quota_exhausted` retry path is unaffected —
  this only applies to a `no_report` classification that would otherwise
  fire, never to a `quota_exhausted`/`blocked`/other failure reason.

## Log

- **2026-09-03**: filed after investigating recurring `fitted-*` job
  failures on the Pi (prompted by the PR #69 investigation session).
  `history.jsonl` showed `no_report` as the dominant failure reason (5 of 9
  recent terminal failures across three jobs); `quota_exhausted` failures in
  the same window turned out to predate PR #65's fallback_model merge and
  are confirmed working now (`fitted-pr-review-fallback-20260903T090014Z` →
  `done`); `blocked` (3 occurrences) needs raw agent transcripts to
  root-cause further and isn't covered by this issue. Discussed with the
  human: a same-session nudge, not a whole-job retry (issue 008's retry
  design explicitly can't apply safely here — re-running risks duplicate
  side effects).
