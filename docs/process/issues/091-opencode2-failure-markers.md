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
- **2026-11-24**: captured real opencode 2.1.4 screens on the laptop (herdr 0.9.5, isolated
  XDG dirs, a throwaway workspace per run, fabricated keys only) and compared them with
  `failure_markers`. The default `"Free usage exceeded"` **still matches**, so the markers
  are unchanged. opencode 2 prints the provider's message verbatim: while it retries a 429
  (herdr status `working`, ~13 attempts over ~90 s) the line is
  `⚠ Retrying in 5s · attempt 5 · <message>`, and once it gives up the screen is
  `Error: <message>` with status `idle`. Fixtures: `tests/fixtures/opencode2_screens/`;
  tests: `tests/test_runner.py` (`opencode2_screen`).
  **Not verified, left open:** no real opencode Zen quota screen could be provoked (the
  free models answered). The quota screens above come from a local stub upstream that
  returned HTTP 429 with the old message text, so they show how opencode 2 renders a
  provider message, not what Zen sends today. Next step: on the first real quota failure on
  opencode 2, compare `<run_id>.tail.txt` (the runner writes it on failure) with
  `quota_shaped_retry.txt`; set `done` once it matches or the marker is updated. opencode
  1's line carried `[retrying in 3h 35m attempt #1]` (a Retry-After); the stub sent none.
  Other findings, no marker change:
  - A model missing from the catalog gets a session anyway; the TUI shows
    `Build · <model> (unavailable)`, the prompt stays unsent, the status stays `idle` and no
    error text appears. No marker could catch it; that is issue 093's catalog check.
  - A 5xx is retried the same way as a 429 (`Retrying in ... · upstream exploded`, `working`),
    and a 401 settles as `Error: Invalid API key`. Neither is a quota, so neither is matched
    (reaping them as `quota_exhausted` would send the job to its fallback for the wrong
    reason); a 5xx retry loop still waits out the timeout.
  - The opencode service is shared: a "Session failed" toast for a quota-failed session of
    one pane rendered over another pane's screen (`foreign_session_failed_toast.txt`). The
    two-consecutive-polls gate keeps one sighting from reaping a healthy run (tested); how
    long the toast stays was not measured.
