---
id: "096"
title: "Confirm what the opencode 2 TUI shows for Zen's real quota 429"
status: open
priority: medium
area: pipeline
---

## Description

Issue 091 showed that opencode 2 renders a provider message verbatim, in a retry line and in
a terminal `Error:` line. Its captured screens came from a local stub upstream that sent the
old text, "Free usage exceeded".

Zen's real 429 (`provider.free-limit`, captured on the Pi on 2026-11-26 from session
`ses_de9a0ad5fffemFKDFnn5F06wsI`, `opencode/deepseek-v4.1-flash-free`) says "Free usage limit
reached. Connect a provider or subscribe to OpenCode Go to keep going, or switch models."
`DEFAULT_FAILURE_MARKERS` now holds both phrasings. What is still unknown is how the TUI lays
that message out. `quota_zen_free_limit_settled_error.txt` reuses the stub screen's layout with
the real text, so it proves the marker matches the wording, not the layout.

The pipeline path is separate and already fixed: issue 095 (#304) reads the session export's
`provider.*` error, so a pipeline stage on a quota 429 reaches `fallback_model` without the
screen.

## Acceptance

- On the first real quota failure of a routine/audit/gated job on opencode 2, compare the
  runner's `<run_id>.tail.txt` with `quota_zen_free_limit_settled_error.txt`; replace the
  fixture with the real screen if the layout differs.
- A 401, 5xx or unavailable-model screen must still not match (091's Log).
- `scripts/ci-check.sh` is green.

## Log

- **2026-11-26**: filed from the residual in [091](091-opencode2-failure-markers.md).
  The wording half is done (marker added, fixture added); the on-screen layout is unverified.
  An earlier draft also asked whether a quota 429 reaches `opencode export` as an error; 095
  answered that.
- **2026-11-27**: closed the detection gap provable offline, parked the capture in
  [097](097-capture-real-zen-quota-screen.md). All six dispatch paths now resolve markers via
  `runner.effective_failure_markers`, so audit/gated jobs and the pipeline launcher see Zen's
  wording and honour an explicit `failure_markers: []`. A blocked opencode settle showing the
  marker is now `quota_exhausted` (session id captured before the owned pane closes; root-mode
  panes stay open), so `routine_flow`'s same-tick fallback fires on the state the one real Zen
  429 actually reported. `quota_zen_free_limit_retry.txt` completes the two-shape corpus, and
  the wrap bound (marker on one line ending by column 36/60) is now an explicit assertion.
  Not proven: that the real TUI shows the message at all and at that prefix width. Comparison
  protocol for the first real `<run_id>.tail.txt`: compare the message **text** with both Zen
  fixtures first. A pure layout difference (chrome, indentation, wrapping above the bound)
  needs no fixture change, because the test's claim is about the message text; replace a
  fixture only if the text or the marker's wrap bound differs. A blocking modal with no
  message text would be a new detection problem, not a fixture swap.
