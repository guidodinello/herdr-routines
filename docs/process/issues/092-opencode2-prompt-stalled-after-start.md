---
id: "092"
title: "Confirm agent_prompt_stalled no longer follows an opencode 2 start"
status: done
priority: low
area: infra
---

## Description

On 2026-11-24, with opencode 2 starting its own background service, a prompt sent right
after `herdr agent start` reported idle could return `agent_prompt_stalled` while opencode
printed "Starting background server…". Since PR #286 the service is kept up by
`opencode-service.service` and herdr-routines refuses to start it, so that wait should be
gone. Two smoke runs on the Pi after the deploy (one under the tick unit's environment)
completed without error (the prompt retry would hide a stall that it absorbed), which is too few to close the question.

## Acceptance

- After a week of scheduled runs, `history.jsonl` and the log show no
  `agent_prompt_stalled` on an opencode start; then close this issue.
- If it still occurs, record when, and extend the existing prompt retry
  (`is_retryable_prompt_error`) or wait for `interactive_ready` after the start.

## Log

- **2026-11-24**: filed as a follow-up of PR #286.
- **2026-11-25**: first evidence after the deploy of #286 to the Pi (~00:48Z). No
  `agent_prompt_stalled` in `herdr-routines.log*`, and no failed or stalled record in
  `history.jsonl` since. Runs on the new code: the scheduled 22:00 (local) `issue-refinement`
  (`done`, 34 s), two smoke runs (one under the tick unit's environment), and nine
  Telegram `/run` opencode spawns (prompt sent right after start every time). Still too few
  to close; check again after a week of scheduled runs.
- **2026-11-28**: the offline half of this issue is fixed: every pipeline stage's first
  prompt now waits for `interactive_ready` (`pipeline_run._start_and_prompt` reuses
  `runner._wait_for_agent_ready`, bounded by `start_timeout_ms`), so the just-started-agent
  race observed in run `20261008T003531Z` is gated instead of relying on prompt retry.
  The retry whitelist is unchanged. This run did **not** inspect a week of production
  evidence: the time-gated watch and its log moved to
  [`098`](098-opencode2-prompt-stall-week-watch.md), and this issue stays `status: open`
  because the pipeline owns the flip to `done`.
