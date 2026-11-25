---
id: "091"
title: "Check the pipeline's failure markers against opencode 2's screens"
status: open
priority: medium
area: pipeline
---

## Description

The pipeline's quota fast-fail and wedge detection read the visible screen
(`agent_read_visible`, `failure_markers`) for text such as a provider quota message.
Those strings were collected from opencode 1 screens. opencode 2 has a different TUI, and
herdr 0.9.5's opencode V2 integration reports agent state from the plugin
(`screen_detection_skipped: true`), so it is unknown whether the same text still appears
on screen when a model hits its quota or returns an API error.

If it does not, a stage on an exhausted free model waits out its whole timeout instead of
falling back, which is the failure mode `fallback_model` exists to prevent.

## Acceptance

- A real quota or API-error screen from opencode 2 is captured and compared with
  `failure_markers`; the markers are updated, or recorded as still matching.
- A test uses the captured screen text as its fixture.
- `scripts/ci-check.sh` is green.

## Log

- **2026-11-24**: filed as a follow-up of PR #286. Not yet observed failing: no night has
  run the pipeline on opencode 2.
