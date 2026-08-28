---
id: "005"
title: "Failure reaping & quota-exhaustion handling — phase 2 (mid-run fast-fail watchdog)"
status: done
priority: medium
area: pipeline
---

## Description

A failed run whose agent never settles (e.g. an OpenCode free-quota modal;
observed twice on the Pi, 2026-08-22/23) left a live `working` agent behind,
so every later tick skipped the job (`agent_name_live`) until manual
cleanup.

Spec: [`docs/failure-reaping.md`](../../failure-reaping.md).

**Phase 1 shipped** (reap own pane on failure, post-hoc quota
classification, failure-path screen tails — PR #25). Phase 2 (mid-run
fast-fail watchdog) is designed but not scheduled — gated on phase 1
surviving a real overnight cycle and the dead-wait problem mattering once
more.

## Acceptance

- Watchdog detects a mid-run stuck/quota-exhausted agent without waiting for
  the full timeout, and reaps it before the next tick would otherwise skip
  the job.
- Does not fire on legitimately slow (not stuck) runs — false-positive reaps
  are worse than the current dead-wait.

## Log

- **2026-08-23**: phase 1 shipped (PR #25).
- Phase 2 remains gated as of curation (2026-08-25) — not yet promoted to
  active work.
- **2026-08-26**: pipeline run `20260826T031438Z` built phase 2 (Popen-based
  `agent_prompt_wait_with_watchdog`, fast-fail of quota-wedged runs before
  `timeout_ms`) — PR #47, merged as `0d1447e`.
- **2026-08-27**: marked done and gate removed (PR #47 merged).
