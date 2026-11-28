# Spec v1 — 20261128T050000Z — Confirm opencode 2 start no longer stalls first prompt

Status: v1 draft.
Implements [issue 092](../../../process/issues/092-opencode2-prompt-stalled-after-start.md) only.
Background: `docs/plan-v1.md` (project context, not this feature).

## Problem

Issue 092 tracks a post-opencode-2-start race: on 2026-11-24, a prompt sent right after
`herdr agent start` reported idle could return `agent_prompt_stalled` while opencode printed
"Starting background server…". PR #286 moved opencode to a persistent
`opencode-service.service` and herdr-routines refuses to start its own service, so the startup
wait that produced the stall should be gone.

Evidence is still insufficient to close the issue. The 2026-11-25 log entry records no
`agent_prompt_stalled` in `herdr-routines.log*` and no failed/stalled `history.jsonl` record
since deploy, but only a few smoke/scheduled/Telegram runs. The prompt retry path can also hide
a stall it absorbs, so absence of terminal failure is weaker evidence than absence of the error.

## Approach

### 1. Collect evidence over a week of scheduled runs

- Inspect production evidence on the Pi (not in this worktree): `history.jsonl` and
  `herdr-routines.log*` for `agent_prompt_stalled`, focusing on runs whose first prompt follows
  an opencode start.
- Record each check in issue 092's log with date, evidence source, and whether any
  `agent_prompt_stalled` occurred on an opencode start.
- After a week of scheduled runs with no such occurrence, flip issue 092 to `status: done` and
  note the closing evidence. Keep the ROADMAP entry pointing at 092 unless project convention
  requires otherwise; do not silently drop the issue.

This stage produces no code change unless the stall recurs; the issue's acceptance is
observational first.

### 2. If the stall recurs, record and fix narrowly

If `agent_prompt_stalled` is observed on an opencode start after #286:

- Record when it happened (timestamp, run/job, agent kind/model, log excerpt, whether prompt
  retry absorbed it).
- Confirm the error shape is provably early and delivery did not occur. `agent_prompt_stalled`
  is currently treated as a stage-prompt start-race case in pipeline runs
  (`pipeline_run.py` passes `retry_delays_s=PROMPT_RETRY_DELAYS_S` and relies on
  `wait_loop.is_retryable_prompt_error`); routine/audit/gated jobs already go through
  `wait_loop.prompt_with_watchdog` / `runner._prompt_with_retry`.
- Apply the smallest fix that matches the evidence:
  - Extend prompt retry classification only if `agent_prompt_stalled` is provably early and not
    already covered by the existing whitelist. Do not broaden retries to settle timeouts,
    wrapper timeouts, or unknown-delivery errors.
  - Or strengthen readiness before first prompt: wait/poll for `interactive_ready` (and/or a
    stronger opencode-ready signal) after `agent start` when the observed failure indicates the
    current readiness gate is insufficient. Keep the wait bounded and fail with a clear reason.

Any code fix must add a regression test that reproduces the observed error shape and proves no
double-prompt on ambiguous delivery.

## Files touched

Expected for the observation-only outcome:

- `docs/process/issues/092-opencode2-prompt-stalled-after-start.md` — log evidence; close when
  acceptance is met.
- `docs/pipeline/runs/20261128T050000Z/spec.md` — this spec.

Only if the stall recurs and a code fix is warranted:

- `src/herdr_routines/wait_loop.py` and/or `src/herdr_routines/runner.py` — retry/readiness
  behavior, depending on where the gap is.
- `src/herdr_routines/pipeline_run.py` — only if stage-prompt handling needs a specific
  adjustment beyond the shared wait loop.
- `src/herdr_routines/herdr.py` — only if the error shape/classification needs a client-side
  distinction.
- `tests/test_wait_loop.py`, `tests/test_runner.py`, `tests/test_pipeline_run.py`, or
  `tests/test_herdr.py` — regression coverage for the chosen fix.
- `ROADMAP.md` — only if issue status/wording needs to change.

## Risks

- **Prompt retry can hide real stalls.** A clean terminal history may mask retries that absorbed
  `agent_prompt_stalled`; evidence should look for the error in logs, not only failed outcomes.
- **Production evidence lives outside the worktree.** `history.jsonl` and system logs are on the
  Pi; this run can plan and document, but cannot honestly claim week-long production evidence it
  did not inspect.
- **Over-broad retry is dangerous.** Treating non-provably-early errors as retryable risks
  double-prompting and duplicate side effects. Any change must preserve the existing delivery
  safety rules.
- **Readiness waits can hide a different failure.** Waiting longer for `interactive_ready` may
  convert a fast failure into a timeout without fixing the underlying opencode startup problem;
  the fix should be driven by observed evidence, not speculation.
