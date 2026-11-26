---
id: "095"
title: "Compare real Zen quota wording and opencode export APIError"
status: open
priority: medium
area: pipeline
---

## Description

Issue 091 confirmed that opencode 2 renders provider messages verbatim, and the default
`"Free usage exceeded"` marker still matches both its retry and terminal shapes. What remains
unverified is the *real* Zen quota wording: the captured screens came from a local stub
upstream that returned HTTP 429 with the old message text, because the free models answered
every attempt on the real service.

A second open question is the pipeline path. `pipeline_run._session_api_error` keys fallback off
`info.error.name == "APIError"` from `opencode export`, which is screen-independent; whether a
real quota 429 lands as `APIError` there was never checked. This issue parks both questions so
closing 091 does not lose them.

## Acceptance

- On the first real quota failure on opencode 2, compare the first `<run_id>.tail.txt` written
  by the runner with the captured fixtures (`quota_shaped_retry.txt`,
  `quota_shaped_settled_error.txt`) and `failure_markers`.
- Check whether that failure appears as `info.error.name == "APIError"` in `opencode export`
  for the same session, and record what the pipeline path does when it does not.
- Update markers or the pipeline signal only if the evidence supports it; a 401/5xx/unavailable
  screen must not be reclassified as quota exhaustion (091's log).
- `scripts/ci-check.sh` is green.

## Log

- **2026-11-26**: filed from the residual in
  [091](091-opencode2-failure-markers.md)'s spec-v2 entry. The settle-time screen scan closes
  091's detection gap; this issue keeps the real-Zen wording and export-`APIError` comparison
  open rather than silently closing it with the stub evidence.
