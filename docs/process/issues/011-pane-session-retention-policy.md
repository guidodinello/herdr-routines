---
id: "011"
title: "Pane/session retention policy"
status: open
priority: low
area: pipeline
---

## Description

Lock in when a finished run's pane/session is cleaned up. Direction
(conversation 2026-08-21/22): capture the full transcript to the run-history
log as soon as a run finishes, then close the pane. Actual cleanup timing
(immediate vs. keep-for-a-week vs. manual) is the open decision.

Partly implemented already: `execute_run` closes its own pane on every
settled terminal path, capturing the agent session id first
(`RunOutcome.session_id` in `history.jsonl`) so a human can resume-and-inspect
(PR #42). What is missing is the *transcript capture to the history log*
before that close, and a documented retention window.

## Acceptance

- On settle, the run's visible transcript (or a bounded tail) is persisted to
  the run-history artifact before the pane is closed.
- Retention timing for the underlying agent session is documented and
  consistent between routine jobs and pipeline workers.

## Log

- **2026-08-27**: curated from `ROADMAP.md` Next §. G-16 / PR #42 already did
  the immediate-close half; this issue is the transcript-capture + documented
  window that was punted.
