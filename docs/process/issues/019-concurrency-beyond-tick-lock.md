---
id: "019"
title: "Concurrency beyond the single tick lock"
status: blocked
priority: low
area: infra
gate: a job regularly starving others of their start time — not observed yet
---

## Description

The blocking tick means one long job delays other jobs' start times by up to
one run. Acceptable at a handful of nightly jobs. If it stops being
acceptable, the fix is per-job systemd units, not a daemon
(`docs/plan-v1.md` §3).

Kept `blocked`: there is no evidence yet that any job starves another, and
the fix shape (per-job units) is a real re-architecture of the deployment
that shouldn't be done speculatively.

## Acceptance

To be written if/when start-time starvation is actually observed in
`history.jsonl` (a job's actual start consistently far from its scheduled
time because another job held the tick).

## Log

- **2026-08-27**: curated from `ROADMAP.md` Later §. Gate is an
  observed-behavior trigger, not a time gate — not affected by the
  2026-08-27 decision to waive the "weeks of runs" gates.
- **2026-08-28**: checked `history.jsonl` on the Pi for starvation evidence —
  32 scheduled runs carry a `late_seconds` field. Median start delay is
  **14.0s**, identical across every job (a fixed startup cost, not
  contention). One outlier of **3628s** (fitted-pr-review, 2026-08-23) — a
  single incident, almost certainly the swap-exhaustion night; never
  recurred. Jobs run ~2 min each, spaced 1 h apart — hours of tick headroom.
  **No evidence of any job starving another.** Stays `blocked`; the gate is
  working as intended.
