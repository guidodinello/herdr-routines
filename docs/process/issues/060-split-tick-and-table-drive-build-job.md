---
id: "060"
title: "Split dispatch flows out of tick.py and table-drive config._build_job"
status: done
priority: medium
area: infra
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
- **2026-11-16**: unblocked. The gate PRs are merged (#191, #195, and the asserts wave
  as #201). `tick.py` had grown to 5,635 lines by then.
- **2026-11-16**: dispatch flows split out. `tick.py` (1,523 lines) keeps the loop, the
  locks, the per-job decide functions and the dispatch core. The per-kind decide
  functions stay there because they and `dispatch_occurrence` call each other. The runs
  moved to `audit_flow`, `gated_pr_flow`, `gated_event_flow`, `gated_base_flow`,
  `routine_flow`, `routine_targets_flow` and `pipeline_flow`, over `dispatch_common`.
  All 109 top-level definitions are unchanged (compared by AST). Each module now has its
  own logger name. Tests that replaced a collaborator on `tick` use
  `tests/dispatch_modules.patch_dispatch`, which replaces it in every dispatch module
  that binds it. The `_build_job` half is still open.
- **2026-11-16**: `_build_job` table-driven. Its cyclomatic complexity went from 194
  to 34 (radon). Each validation block moved verbatim into a `_build_*` helper, and
  `_build_job` calls them in the original order, so the first error reported for a
  config with several mistakes is the same as before. The three steps that differ by
  kind (whether `checks` is required, the audit fields, and
  `catch_up_minutes`/`prompt_file`/`deadline_ms`) come from the `_KIND_RULES` table.
  `target`, `verify` and `pick` still compare `kind` inside their own helpers. A
  differential run of the old and new `_build_job` on 200,000 mutated job configs gave
  identical results, error messages and warnings. One unreachable branch was dropped:
  the pipeline-specific `'checks' is not applicable` error, which the general
  `'checks' requires kind: gated` error always pre-empted.
