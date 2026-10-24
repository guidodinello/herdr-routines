---
id: "078"
title: "Verify gates for event-target fix workers"
status: open
priority: low
area: scheduler
---

## Description

Issue 070 (#232) proves a PR-target fix worker's `done` with `worktree_clean` and
`pushed_or_replied`. Event-target workers (`_dispatch_event_worker` →
`_process_event_target`) got only 070's attempt-budget fix. Their `done` is still taken on
the agent's word. 070 deferred this because "done" depends on the event query:
`issues:opened` has no head branch, and a label event may not require a push. The event
path also overwrites the worker's reason with `gate_failed`.

## Design

- Per query in the closed `github_event` set, define what a worker's done means, and map
  each to existing gates where possible: `worktree_clean` always; `pushed_or_replied`
  for PR-shaped events; a new gate (e.g. a comment or linked PR on the issue) for
  `issues:opened`.
- Keep the worker's own failure reason in the record instead of replacing it with
  `gate_failed`.

## Acceptance criteria

1. [blocking] An event worker that settles `done` without doing its query's required
   action is recorded `failed` with the gate's reason. Test:
   `test_event_fix_worker_done_without_effect_recorded_failed`
2. [blocking] A failing event worker's record keeps its own reason. Test:
   `test_event_worker_failure_reason_preserved`

## Notes

- Related: 070, 071, 014. If 077 lands first, build on its event trigger path.
