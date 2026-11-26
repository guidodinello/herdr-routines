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
