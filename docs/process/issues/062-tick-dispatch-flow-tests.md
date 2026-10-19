---
id: "062"
title: "Tests for tick.py's review and audit dispatch flows"
status: blocked
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
