---
id: "098"
title: "Watch a week of scheduled runs for agent_prompt_stalled after an opencode 2 start"
status: open
priority: low
area: infra
---

## Description

Issue 092's time-gated acceptance could not be completed offline: the production evidence
lives on the Pi, and the pipeline flips the picked issue to `status: done` before the PR
opens. The offline-provable half — pipeline stage prompts racing opencode's TUI startup —
was fixed by gating every stage's first prompt on `interactive_ready` in
`pipeline_run._start_and_prompt`, reusing `runner._wait_for_agent_ready` with the stage's
`start_timeout_ms` bound. The retry whitelist was deliberately left unchanged: it must not
resend an ambiguous delivery.

That fix removes the known cause, but absence of a terminal failure is weak evidence because
the retry path can absorb a stall. The week-long production watch is therefore tracked here
rather than claimed by the code run.

## Acceptance

- On the Pi (not in this worktree), inspect `history.jsonl` and `herdr-routines.log*` for
  `agent_prompt_stalled`, focusing on runs whose first prompt follows an opencode start.
- Record each check in the log below with its date, the evidence source, and whether any
  `agent_prompt_stalled` occurred on an opencode start. Note whether a prompt retry absorbed
  it even when the run itself succeeded.
- After a week of scheduled runs with no such occurrence, flip this issue to `status: done`
  and record the closing evidence. Keep the ROADMAP entry for 092 next to this one.
- If a stall recurs, record timestamp, run/job, agent kind/model, the log excerpt and whether
  the retry absorbed it; then apply the smallest evidence-matching fix and add a regression
  test that proves no double-prompt on ambiguous delivery. Do not broaden retries to settle
  timeouts, wrapper timeouts or unknown delivery.

## Log

- **2026-11-28**: filed to hold issue 092's week-long confirmation. No production evidence
  has been inspected by this code run; the offline readiness gate is the only fix claimed here.
