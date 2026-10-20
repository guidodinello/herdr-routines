---
id: "062"
title: "Tests for tick.py's review and audit dispatch flows"
status: done
priority: low
area: infra
gate: the type-gaps refactor PRs that edit tick.py and tests/test_tick.py are merged; ideally after 060 moves the flows into their own modules
---

## Description

Per the 2026-10-19 code-health audit, `tick.py` had the lowest line coverage
among the core modules (~69%). The largest untested blocks were the review and
audit dispatch flows (`_process_review_job`, `_dispatch_review_target`,
`_process_audit_job`, `_dispatch_audit_fix`). The base-target gate, which was
also at 0%, got tests in PR #186.

This is a coverage gap, not a known bug.

## Acceptance

- Behavior tests for the review and audit flows: target selection, skip
  reasons (already reviewed, attempt budget), worker dispatch, and the history
  records each path writes.
- Coverage of those functions reported in the PR.

## Log

- **2026-10-19**: filed from the code-health audit follow-ups.
- **2026-10-19**: done. `tests/test_tick_dispatch.py` covers the schedule guards of
  both flows, the review cycle and per-target dispatch, and the audit run and
  fix-dispatch failure paths. `tick.py` line coverage went from 73.5% to 81.5%;
  `_process_review_job` and `_process_audit_job` are at 100%. What stays uncovered
  is log-only `except` branches (report, output and instructions writes, pane-close
  and session-id best-effort calls), the no-skill/no-command invariant in
  `_run_audit`, and the corrupt-ledger backup failure. When an agent never becomes
  interactive the review flow records `agent_start_failed` while the audit flow
  records `agent_not_interactive`; the tests pin both.
