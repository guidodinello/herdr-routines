---
id: "015"
title: "Auto-fix pull requests (standing job)"
status: done
priority: medium
area: pipeline
---

## Description

Claude Routines' "Behavior" toggle: watch CI and review comments on PRs a
routine opens, and let the agent push fixes. The `babysit-prs` /
`address-pr-comments` skill pattern as a standing scheduled job instead of
something invoked manually.

Scope: a job that, on each tick, enumerates open PRs authored by a named
routine (or matching `auto/*`), and for each one with failing CI or
unresolved review threads, spawns a worker to push fixes and reply to
threads. Bounded per run (don't loop forever on one PR), and confined to PRs
the routine itself opened.

## Acceptance

- Job finds open `auto/*` PRs with red CI or unresolved review threads and
  dispatches a fix worker per PR, capped per run.
- Never touches a PR not opened by a herdr-routines job.
- Fix attempts and thread replies are logged; a PR that keeps failing after N
  attempts is left alone and surfaced, not retried forever.

## Log

- **2026-08-27**: curated from `ROADMAP.md` Later §. Original trigger ("a
  couple of scheduled review-style jobs have run for real and earned trust")
  — cleared: `fitted-pr-review` and `fitted-pr-review-2/3` run daily. A
  related ad-hoc "watch open herdr-routines PRs and push fixes" routine was
  requested on the Pi 2026-08-27; this issue is the generalized, in-repo
  version.
- **2026-08-29**: merged as PR #50 (`feat: auto-fix pull requests standing
  job`, squash `f8a8eca`). The generalized trigger idea it left behind
  ("run commands, spawn agent only when they fail") is issue 025.
