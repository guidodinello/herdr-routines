---
id: "092"
title: "Confirm agent_prompt_stalled no longer follows an opencode 2 start"
status: open
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
