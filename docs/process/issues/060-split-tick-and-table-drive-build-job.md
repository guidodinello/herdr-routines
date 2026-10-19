---
id: "060"
title: "Split dispatch flows out of tick.py and table-drive config._build_job"
status: blocked
priority: medium
area: infra
gate: the type-gaps refactor PRs that edit tick.py (#191, #195 and its pending "asserts" wave) are merged
---

## Description

The 2026-10-19 code-health audit (radon, at c6e2d13) ranked these two as the
least maintainable code in the repo:

- `tick.py` is ~4.7k lines with a maintainability index of 0. It holds the tick
  loop plus every per-kind flow: routine runs, audit jobs, review jobs, gated
  base/PR/event targets, and the fix/event/review worker dispatchers. Most of the
  audit's risk findings sat in these flows.
- `config._build_job` has a cyclomatic complexity of 194 (radon grade F): one
  function validates and builds every job kind's fields.

This is a refactor, not a bug fix. It was kept out of the audit-fixes run
because any edit to `tick.py` conflicts with the concurrent type-gaps run.

## Acceptance

- Each dispatch flow (`_process_audit_job`, `_process_review_job`, the gated
  target flows and their `_dispatch_*` workers) lives in its own module;
  `tick.py` keeps the loop, lock and per-job dispatch.
- `_build_job` dispatches per kind (a table of per-kind builders or
  validators) instead of one branching function; error messages stay
  byte-identical.
- No behavior change: `scripts/ci-check.sh` and `uv run mypy --strict src` stay
  green with no test edits beyond import paths.

## Log

- **2026-10-19**: filed from the code-health audit follow-ups. Blocked only on
  the open type-gaps PRs touching `tick.py`.
