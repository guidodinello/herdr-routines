# Spec v1 — issue 085: decide whether 0 is valid for timeout and worker-limit config fields

## Problem

`config._build_job` parses four numeric fields with `_int_field(..., positive=False)`,
which accepts 0:

- `timeout_ms` (`src/herdr_routines/config.py:1407`)
- `start_timeout_ms` (`src/herdr_routines/config.py:1408`)
- `max_workers_per_tick` (`src/herdr_routines/config.py:1554`)
- `max_attempts_per_target` (`src/herdr_routines/config.py:1555`)

Sibling fields are already strict: the gated check `timeout_ms` (`config.py:1528-1532`),
the audit `timeout_ms` (`config.py:1675-1681`), the audit `max_attempts_per_target`
(`config.py:1689`), and `max_targets_per_run` (`config.py:1746`) all require a positive
value. The inconsistency has a real failure mode: a 0 `timeout_ms` is passed to
`agent prompt --wait --timeout 0` and times out instantly; a 0 `start_timeout_ms` fails
every startup before the agent can boot; a 0 `max_workers_per_tick` slices
`candidates[:0]` and dispatches nothing (tick.py:1648, tick.py:2223); a 0
`max_attempts_per_target` exhausts the retry budget before the first attempt. In every
case the job silently does nothing — the typo class the issue names.

None of these four has a documented meaning for 0. The one non-negative field that *is*
meaningful is `catch_up_minutes: 0` ("no catch-up grace", plan §4; exercised by
tests/test_tick.py:1498) — that stays non-negative and is explicitly out of scope.

## Decision

For each of the four fields, **reject 0** (`positive=True`). Rationale:

- No deployed config uses 0 — verified against `~/.config/herdr-routines/` (live
  `jobs.d/*.yaml`, `defaults.yaml`, and all backups): every `timeout_ms`,
  `start_timeout_ms`, `max_workers_per_tick`, and `max_attempts_per_target` is positive.
  So no existing jobs.d file stops loading.
- Every 0 value observed downstream is destructive or inert, never useful.
- Consistency with the sibling strict fields removes the need to document a 0
  semantics nobody asked for.

This matches acceptance bullet 1's first option for all four fields; nothing needs a
"what 0 means" comment because 0 is no longer loadable.

## Files touched

1. `src/herdr_routines/config.py` — flip the four `_int_field(..., positive=False)` calls
   to `positive=True` (lines 1407, 1408, 1554, 1555). The error message becomes
   "'<key>' must be a positive integer" (the `_int_field` helper already picks the
   article). No helper changes; `catch_up_minutes` at :1409 keeps `positive=False`.
2. `tests/test_config.py` — one test asserting all four fields reject 0
   (`ConfigError`, matching "positive integer") while `catch_up_minutes: 0` still loads
   (regression guard for the adjacent field). Neighboring coverage to keep:
   `test_max_workers_per_tick_and_max_attempts` (:690) uses positive values only; the
   audit kind's own 0-rejection tests (test_kind_audit.py:220-223) are unaffected.

Verification: `scripts/ci-check.sh` green (the issue's acceptance bullet 3).

## Risks

- **Breaking an existing jobs.d file that relies on 0** — the issue's own warning.
  Mitigated: checked the live config tree directly (see Decision); no zero values for
  any of the four fields. A post-change `herdr-routines validate` against the live
  config confirms the file still loads.
- **Tests asserting the old message text** — the wording changes from "must be a
  non-negative integer" to "must be a positive integer" for exactly these four keys.
  Grep of `tests/` shows no test matches "non-negative" against these fields; any hit
  found while editing gets updated in the same commit.
- **Scope creep** — the fields live in `_build_job`'s shared path, but the audit kind
  re-parses `max_attempts_per_target` separately (:1689, already positive) and
  `kind: audit` explicitly rejects `max_workers_per_tick` (:1645-1647). Only the four
  calls listed change; `catch_up_minutes`, `max_findings_per_dispatch`,
  `ledger_retention_days`, and `max_targets_per_run` keep their current postures.
- **Behavior change is silent-at-config-time no more** — after this change a 0 value is
  a loud load error, which is the intended tightening; no runtime code reads these
  fields differently.
