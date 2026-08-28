---
id: "008"
title: "Retries on failure"
status: open
priority: low
area: cli
---

## Description

Transient failures (server down mid-run, startup timeout) currently end the
run with no retry. Add opt-in, per-job retry for failure classes known to be
transient.

The original gate was "real failure data showing which failures are actually
transient" — a retry can't fix a bad prompt, and blindly rerunning a
non-idempotent job is worse than not retrying. Design must therefore let a
job declare *which* `RunOutcome.reason` values are retry-eligible (e.g.
startup timeout, server-unreachable) rather than retrying unconditionally,
and must not retry a job that is not idempotent.

## Acceptance

- A job can declare a retry count and the failure `reason`s eligible for
  retry; unlisted reasons (bad prompt, gate-content failure) never retry.
- Retries are bounded and logged distinctly in `history.jsonl` (attempt
  number visible).
- Default is no retries — existing jobs are unaffected until they opt in.

## Log

- **2026-08-27**: curated from `ROADMAP.md` Next §. Gate ("real failure
  data") waived by decision 2026-08-27 — several days of real Pi runs have
  accrued; the failure-reaping work (issues 005, phase 1 PR #25) already
  classifies failure reasons, which is the input this needs.
