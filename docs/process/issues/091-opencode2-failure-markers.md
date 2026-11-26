---
id: "091"
title: "Check the pipeline's failure markers against opencode 2's screens"
status: done
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
- **2026-11-26**: first real Zen quota failure on opencode 2 (run `20261126T050000Z`, stage
  6, `opencode/big-pickle`): HTTP 429 `provider.free-limit`, "Free usage limit reached.
  Connect a provider or subscribe to OpenCode Go to keep going, or switch models.",
  `retryAfterMs` 66079000. herdr reported the stage `blocked` and the run ended
  `failed (stage_blocked)` without trying `fallback_model`. The screen was not captured, so
  the marker comparison is still open; note the text differs from the default
  `"Free usage exceeded"`. The session-record path that should have caught it was broken by
  issue 095 (fixed in #304).
- **2026-11-26**: spec v2 closed the detection gap. The retry shape was already covered by the
  in-flight watchdog, but the terminal `Error: Free usage exceeded` screen exists only after
  `agent_prompt_wait` returns; `runner.execute_run` now runs `wait_loop.settled_screen_marker`
  on the settled-success + missing/empty-report path, skips the 120 s nudge on a stable match,
  writes that screen as `<run_id>.tail.txt`, and records `quota_exhausted` so `fallback_model`
  fires. A report written during the scan wins — no `done` run is downgraded. The real-Zen
  comparison moves to [096](096-zen-quota-screen-wording.md); this supersedes
  the earlier "set `done` once it matches" instruction, which assumed the marker text was the
  only open question. The reason shift from `no_report` to `quota_exhausted` is deliberate: no
  deployed job's `retry_on` depends on `no_report` alone. Stage 5 flips this issue to
  `status: done`.
- **2026-11-26**: the real Zen wording (captured on the Pi from session
  `ses_de9a0ad5fffemFKDFnn5F06wsI`, 429 `provider.free-limit`) is "Free usage limit
  reached. Connect a provider or subscribe to OpenCode Go to keep going, or switch models.",
  which the old marker `"Free usage exceeded"` does not match, so on a real Zen 429 the
  settle-time scan above would have missed. `DEFAULT_FAILURE_MARKERS` now also holds
  `"Free usage limit reached"` (the old string stays for the stub screens and opencode 1).
  Fixture `quota_zen_free_limit_settled_error.txt` carries the real text in the stub's
  settled-screen layout. **Not verified:** how the opencode 2 TUI lays that message out on
  screen; no real screen was captured (the model was still rate limited and the run's
  screen was never saved). Tracked in [096](096-zen-quota-screen-wording.md).
- **2026-11-26** (review of #301): the settle scan reads the screen once, not twice 30 s
  apart. A settled screen is static, so the second read only added 30 s to every no-report
  run. Trade-off: the in-flight watchdog's two-poll protection against a foreign session's
  toast is gone on this path; the run has already failed with no report, so a false match
  only changes its reason to `quota_exhausted` and skips the nudge. The scan is limited to
  opencode agents and skipped when `failure_markers` is empty. The pipeline launcher's
  default markers now come from `runner.DEFAULT_FAILURE_MARKERS` too.
